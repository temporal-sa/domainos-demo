"""LangChain mode, fine-grained: LangChain inside activities, the loop in the workflow.

Temporal has no official LangChain plugin, so this is the wrapping a team
writes itself. The rule that makes it durable: one model call per activity,
one tool call per activity. The workflow (LangChainAgentWorkflow) keeps the
conversation and decides what runs next, exactly like the no-framework agent.

Why not one activity around the whole AgentExecutor ("coarse")? Then a crash,
timeout or bad tool call on turn 6 means running turns 1 to 5 again: the same
model calls (and tokens), the same CMDB lookups, a different draft, and no
place to pause for a human review in between.

The LangGraph agent's nodes reuse `model_turn` and `run_tool` from here, so
both framework modes call Claude the same way.
"""

import asyncio
import json
import time
from typing import Any, Dict, List, Literal, Tuple

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, message_to_dict, messages_from_dict
from langchain_core.tools import tool
from pydantic import BaseModel, Field, ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .. import settings
from ..activities import _as_temporal_error
from ..agents import firewall
from ..ai import claude
from ..ai.claude import heuristic_meta
from . import lc_messages as lcm
from .common import (AI_MAX_ATTEMPTS, OFFLINE_TURN_SECONDS, WORKER_ID, ModelCallFailed, attempt, call_tool, heartbeating,
                     inject_model_fault, speed, tracked)


class Rule(BaseModel):
    """One firewall rule."""

    name: str = Field(description="aws-<account>-to-<service>")
    source: str = Field(description="The account's VPC CIDR")
    destination: str = Field(description="Single host IP from the CMDB")
    destination_name: str = Field(default="", description="What the host is")
    port: int
    protocol: Literal["tcp", "udp"]
    purpose: str


def _plain(rule: Any) -> Dict[str, Any]:
    return rule.model_dump() if hasattr(rule, "model_dump") else dict(rule)


# [snippet:lc-tools]
def make_tools(vpc_cidr: str) -> List[Any]:
    """The firewall agent's tools as LangChain tools: the same functions the plain agent calls."""

    @tool
    def lookup_cmdb(query: str) -> dict:
        """Find on-prem systems in the CMDB by name or description. Returns hosts, IPs and the ports each service listens on."""
        return firewall.lookup_cmdb(query)

    @tool
    def check_existing_rules(source_cidr: str, destination: str) -> dict:
        """List Panorama rules that already allow traffic from a source CIDR to a destination host."""
        return firewall.check_existing_rules(source_cidr, destination)

    @tool
    def check_policy(rules: List[Rule]) -> dict:
        """Validate a draft rule set against the bank's network security policy. Returns violations to fix."""
        return firewall.check_policy([_plain(r) for r in rules], vpc_cidr)

    @tool
    def submit_rules(rules: List[Rule], rationale: str) -> str:
        """Submit the final, policy-compliant rule set for human review by Network Security.
        The rationale is 2-4 sentences: what you opened, what you left out and why."""
        return "Submitted for review."  # never runs: the workflow takes the draft to a reviewer

    return [lookup_cmdb, check_existing_rules, check_policy, submit_rules]
# [/snippet]


def _jsonable(d: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(d, default=str))


# [snippet:lc-turn]
@activity.defn
async def lc_llm_turn(payload: Dict[str, Any]) -> Dict[str, Any]:
    """One model turn, LangChain style. The workflow owns the loop; this owns one call."""
    with tracked(f"LangChain: agent turn {payload.get('turn')}", request_id=payload.get("request_id")):
        async with heartbeating(turn=payload.get("turn")):  # a killed worker is noticed in seconds
            return await model_turn(payload["messages"], payload["context"])
# [/snippet]


# [snippet:lc-model]
async def _langchain_call(messages: List[Dict[str, Any]], ctx: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    llm = ChatAnthropic(model=settings.ANTHROPIC_MODEL, max_tokens=4000, max_retries=0)  # Temporal owns retries
    model = llm.bind_tools(make_tools(ctx["vpc_cidr"]))
    history = messages_from_dict(messages)  # the conversation lives in workflow history, as JSON
    ai = await model.ainvoke([SystemMessage(firewall.system_prompt(settings.ORG_NAME)), *history])
    return _jsonable(message_to_dict(ai)), dict(ai.usage_metadata or {})
# [/snippet]


# [snippet:lc-tool]
@activity.defn
async def lc_tool(payload: Dict[str, Any]) -> Dict[str, Any]:
    """One LangChain tool call: retried on its own, never repeated once it succeeds."""
    with tracked(f"LangChain tool: {payload['name']}", request_id=payload.get("request_id")):
        output = await run_tool(payload["name"], payload.get("input") or {}, payload["context"])
    return {"output": output, "message": lcm.tool(payload["id"], payload["name"], output),
            "worker": WORKER_ID, "attempt": attempt()}
# [/snippet]


async def model_turn(messages: List[Dict[str, Any]], ctx: Dict[str, Any], mode: str = "langchain") -> Dict[str, Any]:
    """One model turn: Claude through LangChain, or the offline brain without a key.

    Errors are raised for Temporal to retry; on the last attempt the offline
    brain answers instead, so the agent run doesn't fail outright.
    """
    started = time.monotonic()
    try:
        await inject_model_fault(mode)  # the Agent lab's "Claude rate-limited" scenario
        if claude.enabled():
            msg, usage = await _langchain_call(messages, ctx)
            meta: Dict[str, Any] = {"source": "claude", "model": settings.ANTHROPIC_MODEL,
                                    "latency_ms": int((time.monotonic() - started) * 1000),
                                    "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")}
        else:
            await asyncio.sleep(OFFLINE_TURN_SECONDS * speed())
            msg, meta = _offline(messages, ctx), heuristic_meta("ANTHROPIC_API_KEY not set")
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        if attempt() < AI_MAX_ATTEMPTS:
            retryable = isinstance(e, ModelCallFailed) or any(w in str(e).lower() for w in ("rate", "overload", "429", "529"))
            raise ApplicationError(str(e)[:500], type="AIRetryable" if retryable else type(e).__name__) from e
        msg, meta = _offline(messages, ctx), heuristic_meta("Claude unavailable", str(e)[:300])
    return {"message": msg, "calls": lcm.calls(msg), "text": lcm.text_of((msg.get("data") or {}).get("content"))[:700],
            **meta, "worker": WORKER_ID, "attempt": attempt()}


def _offline(messages: List[Dict[str, Any]], ctx: Dict[str, Any]) -> Dict[str, Any]:
    return lcm.ai_from_blocks(firewall.heuristic_turn(lcm.to_anthropic(messages), ctx))


async def run_tool(name: str, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke one LangChain tool, with the demo's failure injection in front of it."""
    tools = {t.name: t for t in make_tools(ctx.get("vpc_cidr", ""))}
    if name not in tools:
        return {"error": f"Unknown tool '{name}'"}

    async def invoke() -> Any:
        try:
            return await tools[name].ainvoke(args)
        except ValidationError as e:  # bad arguments: tell the model, don't retry the same call
            return {"error": f"Invalid arguments for {name}: {str(e)[:400]}"}

    try:
        return await call_tool(firewall, name, args, ctx, run=invoke)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        raise _as_temporal_error(e) from e


LANGCHAIN_ACTIVITIES = [lc_llm_turn, lc_tool]
