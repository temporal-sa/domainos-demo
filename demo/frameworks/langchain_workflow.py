"""LangChainAgentWorkflow: the firewall agent on LangChain, fine-grained.

Same shape as the no-framework agent: this workflow is the loop. The
difference is inside the activities, where ChatAnthropic and LangChain @tool
functions do the work (see langchain_mode.py). The conversation is kept in
LangChain's own serialized message format so the activity can hand it
straight back to LangChain.
"""

import asyncio
from datetime import timedelta
from typing import Any, Dict, List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from demo import agents as agent_registry
    from demo import compat
    from demo.frameworks import lc_messages as lcm
    from demo.frameworks.session import AgentSession
    from demo.models import AgentInput, AgentResult, ReviewDecision

AI_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0, maximum_interval=timedelta(seconds=15), maximum_attempts=4)
TOOL_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0, maximum_interval=timedelta(seconds=10), maximum_attempts=6)


@workflow.defn
class LangChainAgentWorkflow:
    @workflow.init
    def __init__(self, inp: AgentInput) -> None:
        self._inp = inp
        self._agent = agent_registry.get(inp.agent)
        self._s = AgentSession(inp, self._agent)
        self._messages: List[Dict[str, Any]] = [lcm.human(self._agent.task_prompt(self._s.ctx))]
        self._pending: List[Dict[str, Any]] = []

    # [snippet:lc-run]
    @workflow.run
    async def run(self, inp: AgentInput) -> AgentResult:
        while True:
            draft = await self._think_and_act()
            if draft is None:
                return self._s.result("failed")
            review = await self._s.wait_for_review(draft)  # durable wait for Network Security  # [mark:lc-review-wait]
            if review.decision != "changes":
                return self._s.result("approved" if review.decision == "approve" else "rejected", review)
            self._messages += [
                *self._pending,
                lcm.tool(draft["tool_use_id"], "submit_rules", "Not approved: the reviewer asked for changes."),
                lcm.human(self._agent.feedback_message(review.reviewer, review.comment)),
            ]
    # [/snippet]

    # [snippet:lc-loop]
    async def _think_and_act(self) -> Optional[Dict[str, Any]]:
        """Same loop as the no-framework agent. LangChain runs inside the activities."""
        for _ in range(self._inp.max_turns):
            turn = await workflow.execute_activity(  # [mark:lc-turn]
                "lc_llm_turn",
                {"messages": self._messages, "context": self._s.ctx, "turn": self._s.stats["llm_calls"] + 1,
                 "request_id": self._inp.request_id},
                result_type=dict,
                start_to_close_timeout=timedelta(seconds=120),
                heartbeat_timeout=timedelta(seconds=self._inp.heartbeat_seconds),
                retry_policy=AI_RETRY,
                **compat.activity_kwargs(f"LangChain: turn {self._s.stats['llm_calls'] + 1}"),
            )
            self._s.record_turn(turn)
            self._messages.append(turn["message"])  # a serialized AIMessage

            calls = turn["calls"]
            submit = next((c for c in calls if c["name"] == "submit_rules"), None)
            results = await asyncio.gather(*[self._call_tool(c) for c in calls if c is not submit])  # [mark:lc-tool]
            if submit:
                self._pending = list(results)
                return {"tool_use_id": submit["id"], **submit["input"]}
            if not calls:
                self._messages.append(lcm.human("Call submit_rules with your final rule set."))
                continue
            self._messages.extend(results)  # serialized ToolMessages
        return None
    # [/snippet]

    async def _call_tool(self, call: Dict[str, Any]) -> Dict[str, Any]:
        out = await workflow.execute_activity(
            "lc_tool",
            {"id": call["id"], "name": call["name"], "input": call["input"], "context": self._s.ctx,
             "request_id": self._inp.request_id},
            result_type=dict,
            start_to_close_timeout=timedelta(seconds=60),
            heartbeat_timeout=timedelta(seconds=self._inp.heartbeat_seconds),
            retry_policy=TOOL_RETRY,
            **compat.activity_kwargs(f"LangChain tool: {call['name']}"),
        )
        self._s.record_tool(call["name"], call["input"], out)
        return out["message"]

    @workflow.update
    def review_draft(self, d: ReviewDecision) -> str:
        self._s.review = d
        return f"{d.decision} recorded"

    @review_draft.validator
    def _validate_review(self, d: ReviewDecision) -> None:
        self._s.validate_review(d)

    @workflow.query
    def get_agent(self) -> Dict[str, Any]:
        return self._s.view()
