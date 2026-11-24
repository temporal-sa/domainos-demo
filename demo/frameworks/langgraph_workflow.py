"""LangGraphAgentWorkflow: runs the firewall-agent graph with Temporal's LangGraph plugin.

The graph does the agent work; this workflow runs it, shows progress, and
turns the graph's interrupt() into a human review: query the draft, take the
decision as a Workflow Update, resume the graph with Command(resume=...).
Same handlers and query shape as the other two agent workflows, so the UI,
the parent track and the Agent lab treat all three alike.
"""

from typing import Any, Dict, Optional

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from demo import agents as agent_registry
    from demo import frameworks
    from demo.frameworks import lc_messages as lcm
    from demo.frameworks.session import AgentSession
    from demo.models import AgentInput, AgentResult, ReviewDecision

    temporal_graph = frameworks.plugin_module().graph


@workflow.defn
class LangGraphAgentWorkflow:
    @workflow.init
    def __init__(self, inp: AgentInput) -> None:
        self._inp = inp
        self._agent = agent_registry.get(inp.agent)
        self._s = AgentSession(inp, self._agent)
        self._node: Optional[str] = None  # last node that finished

    # [snippet:lg-workflow]
    @workflow.run
    async def run(self, inp: AgentInput) -> AgentResult:
        app = temporal_graph(frameworks.GRAPH_NAME).compile(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": workflow.info().workflow_id}}
        graph_input: Any = {
            "messages": [lcm.human(self._agent.task_prompt(self._s.ctx))], "ctx": self._s.ctx,
            "request_id": inp.request_id, "max_turns": inp.max_turns, "turns": 0, "round": 1,
            "decision": None, "events": [],
        }
        review: Optional[ReviewDecision] = None
        while True:
            paused = await self._run_graph(app, graph_input, config)  # [mark:lg-run]
            if paused is None:  # the graph reached END
                break
            # interrupt() in the review node: wait for Network Security, durably.
            review = await self._s.wait_for_review(paused["draft"])  # [mark:lg-review-wait]
            graph_input = Command(resume={"decision": review.decision, "reviewer": review.reviewer, "comment": review.comment})
        if review and review.decision in ("approve", "reject"):
            return self._s.result("approved" if review.decision == "approve" else "rejected", review)
        return self._s.result("failed")

    async def _run_graph(self, app: Any, graph_input: Any, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Run the graph until it ends or pauses. Returns the interrupt payload if it paused."""
        paused = None
        async for update in app.astream(graph_input, config, stream_mode="updates"):
            for node, data in (update or {}).items():
                if node == "__interrupt__":
                    first = data[0] if isinstance(data, (list, tuple)) else data
                    paused = getattr(first, "value", first)
                else:
                    self._absorb(node, data or {})
        return paused
    # [/snippet]

    def _absorb(self, node: str, data: Dict[str, Any]) -> None:
        """Turn a node's state update into transcript entries and a durability unit."""
        self._node = node
        events = data.get("events") or []
        for e in events:
            if e.get("kind") == "turn":
                self._s.record_turn(e)
            elif e.get("kind") == "tool":
                self._s.record_tool(e["name"], e.get("input") or {}, {"output": e.get("output"), "attempt": e.get("attempt"), "worker": e.get("worker")})
        first = events[0] if events else {}
        label = {"agent": "agent", "tools": f"tools ({len(events)} call{'' if len(events) == 1 else 's'})", "review": "review"}.get(node, node)
        self._s.units.append({"kind": "node", "node": node, "label": label, "attempt": first.get("attempt"),
                              "worker": first.get("worker"), "at": workflow.now().isoformat(), "calls": len(events) if node == "tools" else None})

    @workflow.update
    def review_draft(self, d: ReviewDecision) -> str:
        self._s.review = d
        return f"{d.decision} recorded"

    @review_draft.validator
    def _validate_review(self, d: ReviewDecision) -> None:
        self._s.validate_review(d)

    @workflow.query
    def get_agent(self) -> Dict[str, Any]:
        return self._s.view(node=self._node)
