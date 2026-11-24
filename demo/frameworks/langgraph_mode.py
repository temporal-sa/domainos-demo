"""LangGraph mode: the firewall agent as a LangGraph graph, run by Temporal's LangGraph plugin.

    START -> agent -> tools  -> agent ...       (look things up, draft, check policy)
                   -> review -> agent | END     (Network Security decides)

Each node runs as a Temporal activity (metadata execute_in="activity") with
its own timeout, heartbeat and retry policy. The routing functions are async
and run in the workflow, as the plugin requires, so they only read state.

The review node calls interrupt(). The graph pauses there; the workflow shows
the draft, waits for a Workflow Update, and resumes the graph with
Command(resume=<decision>) (see langgraph_workflow.py).

State is plain JSON (LangChain message dicts) because it crosses activity
boundaries. The model call and the tools are the same ones LangChain mode
uses, so the comparison is about orchestration only.

The plugin is in Public Preview: check the Temporal docs for API changes.
"""

import asyncio
import operator
from datetime import timedelta
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from temporalio.common import RetryPolicy

from ..agents import firewall
from . import lc_messages as lcm
from .common import WORKER_ID, attempt, heartbeating, tracked
from .langchain_mode import model_turn, run_tool


class AgentState(TypedDict):
    messages: Annotated[List[Dict[str, Any]], operator.add]  # LangChain message dicts
    ctx: Dict[str, Any]  # account, VPC CIDR, on-prem targets
    request_id: str
    max_turns: int
    turns: int  # model turns this review round
    round: int
    decision: Optional[Dict[str, Any]]
    events: Annotated[List[Dict[str, Any]], operator.add]  # what each node did, for the UI


NUDGE = "Call submit_rules with your final rule set."


# [snippet:lg-agent-node]
async def agent(state: AgentState) -> Dict[str, Any]:
    """One model turn: ChatAnthropic with the tools bound (same call as LangChain mode)."""
    last = state["messages"][-1] if state["messages"] else None
    nudge = [lcm.human(NUDGE)] if last and last.get("type") == "ai" and not lcm.calls(last) else []
    with tracked(f"LangGraph: agent node, turn {state['turns'] + 1}", request_id=state.get("request_id")):
        async with heartbeating(node="agent"):
            turn = await model_turn(state["messages"] + nudge, state["ctx"], mode="langgraph")
    event = {"node": "agent", "kind": "turn", **{k: turn.get(k) for k in (
        "text", "calls", "source", "model", "latency_ms", "input_tokens", "output_tokens", "worker", "attempt")}}
    return {"messages": nudge + [turn["message"]], "turns": state["turns"] + 1, "events": [event]}
# [/snippet]


# [snippet:lg-tools-node]
async def tools(state: AgentState) -> Dict[str, Any]:
    """Every tool call from the last model turn, in parallel, in this one node."""
    calls = [c for c in lcm.calls(lcm.last_ai(state["messages"])) if c["name"] != "submit_rules"]
    with tracked(f"LangGraph: tools node ({len(calls)} calls)", request_id=state.get("request_id")):
        async with heartbeating(node="tools"):
            outputs = await asyncio.gather(*[run_tool(c["name"], c["input"], state["ctx"]) for c in calls])
    return {
        "messages": [lcm.tool(c["id"], c["name"], out) for c, out in zip(calls, outputs)],
        "events": [{"node": "tools", "kind": "tool", "name": c["name"], "input": c["input"], "output": out,
                    "worker": WORKER_ID, "attempt": attempt(), "batch": len(calls)} for c, out in zip(calls, outputs)],
    }
# [/snippet]


# [snippet:lg-review-node]
async def review(state: AgentState) -> Dict[str, Any]:
    """Pause the graph until Network Security decides."""
    submit = next(c for c in lcm.calls(lcm.last_ai(state["messages"])) if c["name"] == "submit_rules")
    decision = interrupt({"draft": {"tool_use_id": submit["id"], **submit["input"]}, "round": state["round"]})  # [mark:lg-interrupt]
    event = {"node": "review", "kind": "review", "decision": decision.get("decision"), "worker": WORKER_ID, "attempt": attempt()}
    if decision.get("decision") != "changes":
        return {"decision": decision, "events": [event]}
    return {  # back to the agent with the reviewer's notes
        "decision": decision, "round": state["round"] + 1, "turns": 0, "events": [event],
        "messages": [lcm.tool(submit["id"], "submit_rules", "Not approved: the reviewer asked for changes."),
                     lcm.human(firewall.feedback_message(decision.get("reviewer", ""), decision.get("comment", "")))],
    }
# [/snippet]


# [snippet:lg-routes]
async def after_agent(state: AgentState) -> str:  # routing runs in the workflow: no I/O
    calls = lcm.calls(lcm.last_ai(state["messages"]))
    if any(c["name"] != "submit_rules" for c in calls):
        return "tools"
    if calls:
        return "review"
    return END if state["turns"] >= state["max_turns"] else "agent"


async def after_tools(state: AgentState) -> str:
    if any(c["name"] == "submit_rules" for c in lcm.calls(lcm.last_ai(state["messages"]))):
        return "review"
    return END if state["turns"] >= state["max_turns"] else "agent"


async def after_review(state: AgentState) -> str:
    return "agent" if (state.get("decision") or {}).get("decision") == "changes" else END
# [/snippet]


# [snippet:lg-graph]
def build_graph() -> StateGraph:
    g = StateGraph(AgentState)
    g.add_node("agent", agent, metadata={  # each node is an activity, with its own policy
        "execute_in": "activity",
        "start_to_close_timeout": timedelta(seconds=120),
        "heartbeat_timeout": timedelta(seconds=10),
        "retry_policy": RetryPolicy(initial_interval=timedelta(seconds=2), maximum_interval=timedelta(seconds=15), maximum_attempts=4),
    })
    g.add_node("tools", tools, metadata={
        "execute_in": "activity",
        "start_to_close_timeout": timedelta(seconds=90),
        "heartbeat_timeout": timedelta(seconds=10),
        "retry_policy": RetryPolicy(initial_interval=timedelta(seconds=1), maximum_interval=timedelta(seconds=5), maximum_attempts=10),
    })
    g.add_node("review", review, metadata={"execute_in": "activity", "start_to_close_timeout": timedelta(seconds=30)})
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", after_agent, ["tools", "review", "agent", END])
    g.add_conditional_edges("tools", after_tools, ["review", "agent", END])
    g.add_conditional_edges("review", after_review, ["agent", END])
    return g
# [/snippet]
