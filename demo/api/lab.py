"""Agent lab: where Temporal fits with LangChain and LangGraph.

The lab runs the firewall agent on one runtime at a time (or all three) with
a chosen scenario, with no parent request, so a run can be crashed, reviewed
and inspected in isolation. The words on the page (diagrams, walkthrough,
scenarios, the who-does-what table) come from config/agent_lab.yaml.

This module loads that file and holds the pure functions that turn an agent's
query result into what the lab shows: durable steps (one per activity) and a
scorecard of model calls and tool runs, including repeats caused by failures.
"""

from datetime import datetime
from functools import lru_cache
from typing import Any, Dict, List, Optional

import yaml

from .. import frameworks, settings

LAB_SPEC: Dict[str, Any] = {
    "account_name": "mortgage-analytics-prod",
    "environment": "prod",
    "data_classification": "Confidential",
    "onprem_connectivity": True,
    "onprem_targets": ["Oracle loan origination database", "mainframe MQ", "data center DNS", "NTP"],
    "request_text": ("Production AWS account for the Mortgage Analytics team. It needs the Oracle loan database "
                     "and the mainframe MQ in the data center, plus DNS and NTP."),
}
LAB_CONTEXT: Dict[str, Any] = {"vpc_cidr": "10.48.36.0/22"}
LAB_REVIEWER = "Network Security on-call"
LAB_CHANGE = "Drop the NTP rule; we use Amazon Time Sync"

CONTENT_FILE = "agent_lab.yaml"


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def content() -> Dict[str, Any]:
    """Diagrams, walkthrough steps, scenarios and the who-does-what table (config/agent_lab.yaml)."""
    path = settings.CONFIG_DIR / CONTENT_FILE
    try:
        return _load(str(path), path.stat().st_mtime)
    except (OSError, yaml.YAMLError) as e:
        return {"error": f"Couldn't read {CONTENT_FILE}: {e}"}


def lab_runs(st: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """The latest run per runtime from the stored lab state (older versions kept a list)."""
    runs = (st or {}).get("runs") or {}
    if isinstance(runs, list):
        runs = {r.get("mode"): r for r in runs if isinstance(r, dict) and r.get("mode")}
    return {m: r for m, r in runs.items() if isinstance(r, dict) and r.get("workflow_id")}


def scenario(sid: Optional[str]) -> Dict[str, Any]:
    scenarios = content().get("scenarios") or []
    return next((x for x in scenarios if x.get("id") == sid), scenarios[0] if scenarios else {"id": "happy", "label": "Happy path"})


def _ts(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


PENDING_LABEL = {
    "agent_llm_turn": "Model turn", "lc_llm_turn": "Model turn", "agent_tool": "Tool call", "lc_tool": "Tool call",
}


def units(agent: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One entry per activity the agent ran, in order, then whatever is in flight now."""
    if agent.get("units"):
        done = [dict(u, state="done") for u in agent["units"]]
    else:
        done = []
        for e in agent.get("transcript") or []:
            if e.get("kind") == "turn":
                done.append({"kind": "turn", "label": f"Turn {e.get('n')}", "attempt": e.get("attempt"), "worker": e.get("worker"), "at": e.get("at"), "state": "done"})
            elif e.get("kind") == "tool":
                done.append({"kind": "tool", "label": e.get("name"), "attempt": e.get("attempt"), "worker": e.get("worker"), "at": e.get("at"), "state": "done"})
    for p in agent.get("pending") or []:
        name = p.get("activity_type") or ""
        done.append({"kind": "pending", "label": PENDING_LABEL.get(name, name), "attempt": p.get("attempt"),
                     "worker": p.get("last_worker"), "state": "retrying" if p.get("next_attempt_at_future") or (p.get("attempt") or 1) > 1 else "running",
                     "failure": p.get("last_failure"), "retry_at": p.get("next_attempt_at") if p.get("next_attempt_at_future") else None})
    if agent.get("status") == "awaiting_review":
        done.append({"kind": "wait", "label": "Waiting for review", "state": "waiting"})
    return done


def score(agent: Dict[str, Any], crashes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Model calls and tool runs made, how many were repeats, and what a coarse run would have lost."""
    turns = [e for e in agent.get("transcript") or [] if e.get("kind") == "turn"]
    tools = [e for e in agent.get("transcript") or [] if e.get("kind") == "tool"]
    llm_in_flight = [p for p in agent.get("pending") or [] if p.get("activity_type") in ("agent_llm_turn", "lc_llm_turn")]
    model_calls = sum(int(e.get("attempt") or 1) for e in turns) + sum(max(0, int(p.get("attempt") or 1) - 1) for p in llm_in_flight)
    tool_runs = sum(int(e.get("attempt") or 1) for e in tools)
    out: Dict[str, Any] = {
        "turns": len(turns),
        "tool_calls": len(tools),
        "model_calls": model_calls,
        "tool_runs": tool_runs,
        "repeated_model_calls": model_calls - len(turns),
        "repeated_tool_runs": tool_runs - len(tools),
        "rounds": agent.get("round") or 1,
        "tokens": int((agent.get("stats") or {}).get("input_tokens") or 0) + int((agent.get("stats") or {}).get("output_tokens") or 0),
        "coarse": None,
    }
    # If the whole agent were one activity, a crash would throw away everything done so far.
    start = _ts(turns[0]["at"]) if turns else None
    lost_turns = lost_tools = 0
    for c in crashes:
        at = c.get("at")
        if at is None or start is None or at < start - 5:
            continue
        lost_turns += sum(1 for e in turns if (_ts(e.get("at")) or 0) <= at)
        lost_tools += sum(1 for e in tools if (_ts(e.get("at")) or 0) <= at)
    if lost_turns or lost_tools:
        out["coarse"] = {"model_calls": lost_turns, "tool_calls": lost_tools}
    return out


def run_view(mode: str, run: Dict[str, Any], agent: Dict[str, Any], crashes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One lab run as the UI shows it. Only crashes after the run started count against it."""
    meta = frameworks.MODES.get(mode, {})
    mine = [c for c in crashes if c.get("at", 0) >= (run.get("started_at") or 0) - 1]
    sc = scenario(run.get("scenario"))
    return {
        "mode": mode,
        "label": meta.get("label", mode),
        "workflow_id": run.get("workflow_id"),
        "started_at": run.get("started_at"),
        "scenario": {"id": sc.get("id"), "label": sc.get("label")},
        "agent": agent,
        "crashes": mine,
        "units": units(agent),
        "score": score(agent, mine),
        "example": example_call(agent),
    }


def example_call(agent: Dict[str, Any]) -> Optional[str]:
    """The run's first tool request, written as a call: lookup_cmdb(query="Oracle ...")."""
    for e in agent.get("transcript") or []:
        if e.get("kind") == "tool":
            args = ", ".join(f"{k}={v!r}" if not isinstance(v, str) else f'{k}="{v}"' for k, v in (e.get("input") or {}).items())
            return f"{e.get('name')}({args[:80]}{'…' if len(args) > 80 else ''})"
    return None
