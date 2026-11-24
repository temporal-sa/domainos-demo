"""Temporal activities: everything that touches the outside world.

Workflows stay deterministic; anything that can fail, time out or return a
different answer twice (Claude, AWS, Ansible, ServiceNow) runs here, where
Temporal retries it according to the policy the workflow set.
"""

import asyncio
import json
import os
import socket
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from temporalio import activity
from temporalio.exceptions import ApplicationError

from . import agents, chaos, connectors, settings, worker_stats
from .ai import claude
from .ai import tasks as ai
from .ai.claude import AIRetryable, heuristic_meta
from .catalog import build_plan
from .connectors.base import StepContext, StepFailed
from .frameworks.common import AI_MAX_ATTEMPTS, ModelCallFailed, call_tool, heartbeating, offline_pause
from .models import IntakeInput, Plan, PlanInput, StepInput, StepResult

STORE = chaos.RuntimeStore()
WORKER_ID = os.getenv("DEMO_WORKER_ID") or f"worker@{socket.gethostname()}"


def _key() -> str:
    info = activity.info()
    return f"{info.workflow_id}:{info.activity_id}:{info.attempt}"


def _track_activity(label: str, **meta: Any) -> str:
    info = activity.info()
    return worker_stats.begin(_key(), {"label": label, "attempt": info.attempt, "workflow_id": info.workflow_id, "activity_type": info.activity_type, **meta})


def _resume_checkpoint() -> Dict[str, Any]:
    details = activity.info().heartbeat_details
    if details:
        last = details[-1]
        if isinstance(last, dict):
            return dict(last.get("checkpoint") or {})
    return {}


def _make_ctx(inp: StepInput, action: str, key: str) -> StepContext:
    info = activity.info()
    rt = STORE.read()
    checkpoint = _resume_checkpoint()
    ctx = StepContext(
        request_id=inp.request_id,
        track_id=inp.track_id,
        step_id=inp.step.id,
        step_name=inp.step.name,
        system=inp.step.system,
        system_label=inp.step.system_label,
        action=action,
        params=dict(inp.step.params),
        spec=inp.spec,
        context=inp.context,
        seconds=inp.step.seconds,
        speed=float(rt.get("speed", 1.0) or 1.0),
        original_output=inp.original_output,
        resume=checkpoint,
    )

    def progress(line: Optional[str], pct: Optional[float], cp: Optional[Dict[str, Any]]) -> None:
        if cp:
            checkpoint.update(cp)
        worker_stats.progress(key, line, pct)
        activity.heartbeat({
            "step": inp.step.id,
            "track": inp.track_id,
            "worker": WORKER_ID,
            "attempt": info.attempt,
            "pct": pct,
            "log": ctx.log[-6:],
            "checkpoint": checkpoint,
        })

    ctx._progress = progress
    return ctx


def _as_temporal_error(e: Exception) -> ApplicationError:
    if isinstance(e, chaos.InjectedFault):
        return ApplicationError(str(e), {"worker": WORKER_ID, "injected": e.fault_id}, type=e.error_type, non_retryable=not e.retryable)
    if isinstance(e, StepFailed):
        return ApplicationError(str(e), {"worker": WORKER_ID, **e.details}, type=e.error_type, non_retryable=not e.retryable)
    return ApplicationError(f"{type(e).__name__}: {e}", {"worker": WORKER_ID}, type=type(e).__name__)


async def _execute_step(inp: StepInput, compensating: bool) -> StepResult:
    info = activity.info()
    action = (inp.step.undo or "") if compensating else inp.step.action
    label = f"{'Undo: ' if compensating else ''}{inp.step.name}"
    key = _track_activity(label, request_id=inp.request_id, track=inp.track_id, step=inp.step.id, system=inp.step.system_label)
    started = time.monotonic()
    ctx = _make_ctx(inp, action, key)
    try:
        if info.attempt > 1:
            ctx.progress(f"Attempt {info.attempt}, picked up by {WORKER_ID}")
        if ctx.resume:
            ctx.progress(f"Resuming from checkpoint {json.dumps(ctx.resume)}")
        if not compensating:
            extra = await asyncio.to_thread(chaos.apply, STORE, inp.track_id, inp.step.id, inp.step.system)
            if extra:
                ctx.progress(f"{inp.step.system_label} is slow today (+{extra:.1f}s)")
                await ctx.wait(extra)
        conn = connectors.get(inp.step.system)
        output = await (conn.compensate(ctx) if compensating else conn.execute(ctx))
    except asyncio.CancelledError:
        worker_stats.end(key, ok=False, error="cancelled")
        raise
    except Exception as e:  # noqa: BLE001 - translated for Temporal
        worker_stats.end(key, ok=False, error=str(e)[:200])
        raise _as_temporal_error(e) from e
    worker_stats.end(key, ok=True)
    return StepResult(
        step_id=inp.step.id,
        status="rolled_back" if compensating else "completed",
        output=output or {},
        worker=WORKER_ID,
        attempt=info.attempt,
        duration_ms=int((time.monotonic() - started) * 1000),
        log=ctx.log[-40:],
    )


@activity.defn
async def run_step(inp: StepInput) -> StepResult:
    return await _execute_step(inp, compensating=False)


@activity.defn
async def compensate_step(inp: StepInput) -> StepResult:
    return await _execute_step(inp, compensating=True)


# ---------------------------------------------------------------------------
# Claude activities
# ---------------------------------------------------------------------------

async def _ai_call(label: str, call: Callable[[], Awaitable[Tuple[Dict[str, Any], Dict[str, Any]]]], fallback: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    info = activity.info()
    key = _track_activity(f"Claude: {label}")
    try:
        data, meta = await call()
    except asyncio.CancelledError:
        worker_stats.end(key, ok=False, error="cancelled")
        raise
    except Exception as e:  # noqa: BLE001
        if info.attempt >= AI_MAX_ATTEMPTS:
            # Out of retries: keep the request moving with the deterministic fallback.
            worker_stats.end(key, ok=True)
            return {"result": fallback(), "meta": heuristic_meta(f"Claude unavailable after {info.attempt} attempts", str(e)[:300])}
        worker_stats.end(key, ok=False, error=str(e)[:200])
        raise ApplicationError(str(e), type="AIRetryable" if isinstance(e, AIRetryable) else type(e).__name__) from e
    worker_stats.end(key, ok=True)
    meta["attempt"] = info.attempt
    return {"result": data, "meta": meta}


@activity.defn
async def ai_parse_request(inp: IntakeInput) -> Dict[str, Any]:
    out = await _ai_call(
        "reading the request",
        lambda: ai.parse_request(inp.request_text, inp.requester),
        lambda: ai.normalize_spec(ai.heuristic_parse(inp.request_text, inp.requester), inp.request_text, inp.requester),
    )
    return {"spec": out["result"], "meta": out["meta"]}


@activity.defn
async def ai_risk_review(payload: Dict[str, Any]) -> Dict[str, Any]:
    return await _ai_call("briefing approvers", lambda: ai.risk_review(payload["spec"], payload["plan"]), lambda: ai.heuristic_risk(payload["spec"]))


@activity.defn
async def ai_diagnose_failure(payload: Dict[str, Any]) -> Dict[str, Any]:
    return await _ai_call("diagnosing a failure", lambda: ai.diagnose(payload), lambda: ai.heuristic_diagnose(payload))


@activity.defn
async def ai_completion_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    return await _ai_call(
        "writing the hand-off note",
        lambda: ai.summarize(payload["spec"], payload["outputs"], payload["tracks"]),
        lambda: ai.heuristic_summary(payload["spec"], payload["outputs"], payload["tracks"]),
    )


# ---------------------------------------------------------------------------
# Agent activities: one model turn, or one tool call, per activity
# ---------------------------------------------------------------------------

# [snippet:agent-activity]
@activity.defn
async def agent_llm_turn(payload: Dict[str, Any]) -> Dict[str, Any]:
    """One model turn of an agent. Client retries are off: Temporal retries."""
    info = activity.info()
    agent = agents.get(payload["agent"])
    key = _track_activity(f"Claude: agent turn {payload.get('turn')}", request_id=payload.get("request_id"))
    try:
        async with heartbeating(turn=payload.get("turn")):  # a killed worker is noticed in seconds
            if claude.enabled():
                out = await claude.tool_turn(agent.system_prompt(settings.ORG_NAME), payload["messages"], agent.TOOLS)
            else:
                await offline_pause()
                out = {"content": agent.heuristic_turn(payload["messages"], payload["context"]), **heuristic_meta("ANTHROPIC_API_KEY not set")}
    except asyncio.CancelledError:
        worker_stats.end(key, ok=False, error="cancelled")
        raise
    except Exception as e:  # noqa: BLE001
        if info.attempt < AI_MAX_ATTEMPTS:
            worker_stats.end(key, ok=False, error=str(e)[:200])
            raise ApplicationError(str(e), type="AIRetryable" if isinstance(e, (AIRetryable, ModelCallFailed)) else type(e).__name__) from e
        out = {"content": agent.heuristic_turn(payload["messages"], payload["context"]), **heuristic_meta("Claude unavailable", str(e)[:300])}
    worker_stats.end(key, ok=True)
    return {**out, "worker": WORKER_ID, "attempt": info.attempt}
# [/snippet]


# [snippet:plain-tool-activity]
@activity.defn
async def agent_tool(payload: Dict[str, Any]) -> Dict[str, Any]:
    """One tool call. Tools hit real systems (CMDB, Panorama), so each one is retried on its own."""
    info = activity.info()
    agent = agents.get(payload["agent"])
    name = payload["name"]
    key = _track_activity(f"Agent tool: {name}", request_id=payload.get("request_id"))
    try:
        output = await call_tool(agent, name, payload.get("input") or {}, payload.get("context") or {})
    except asyncio.CancelledError:
        worker_stats.end(key, ok=False, error="cancelled")
        raise
    except Exception as e:  # noqa: BLE001
        worker_stats.end(key, ok=False, error=str(e)[:200])
        raise _as_temporal_error(e) from e
    worker_stats.end(key, ok=True)
    return {"output": output, "worker": WORKER_ID, "attempt": info.attempt}
# [/snippet]


# ---------------------------------------------------------------------------
# Planning, ITSM, notifications, verification
# ---------------------------------------------------------------------------

@activity.defn
async def plan_request(inp: PlanInput) -> Plan:
    key = _track_activity("Building the provisioning plan", request_id=inp.request_id)
    plan = build_plan(inp.spec)
    worker_stats.end(key, ok=True)
    return plan


async def _servicenow(request_id: str, action: str, label: str, spec: Dict[str, Any], context: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    info = activity.info()
    key = _track_activity(label, request_id=request_id, system="ServiceNow")
    ctx = StepContext(request_id=request_id, track_id="itsm", step_id=action, step_name=label, system="servicenow",
                      system_label="ServiceNow", action=action, params=params, spec=spec, context=context,
                      seconds=1.5, speed=float(STORE.read().get("speed", 1.0) or 1.0))
    ctx._progress = lambda line, pct, cp: (worker_stats.progress(key, line, pct), activity.heartbeat({"log": ctx.log[-4:], "attempt": info.attempt}))
    try:
        await asyncio.to_thread(chaos.apply, STORE, "itsm", action, "servicenow")
        out = await connectors.get("servicenow").execute(ctx)
    except Exception as e:  # noqa: BLE001
        worker_stats.end(key, ok=False, error=str(e)[:200])
        raise _as_temporal_error(e) from e
    worker_stats.end(key, ok=True)
    return out


@activity.defn
async def itsm_create_change(payload: Dict[str, Any]) -> Dict[str, Any]:
    return await _servicenow(payload["request_id"], "create_change", "Open change request", payload["spec"], {}, {})


@activity.defn
async def itsm_update_change(payload: Dict[str, Any]) -> Dict[str, Any]:
    return await _servicenow(payload["request_id"], "update_change", f"Update change: {payload.get('state', '')}",
                             payload.get("spec", {}), payload.get("context", {}), {"state": payload.get("state", ""), "note": payload.get("note", "")})


@activity.defn
async def notify(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Mock notifier: appends to runtime/notifications.jsonl.

    Swap for Slack, Teams or email; the workflow only cares that it returns.
    """
    key = _track_activity(f"Notify {payload.get('to', '')}", request_id=payload.get("request_id"))
    record = {"at": time.time(), "worker": WORKER_ID, **payload}
    path = settings.RUNTIME_DIR / "notifications.jsonl"

    def append() -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    await asyncio.to_thread(append)
    worker_stats.end(key, ok=True)
    return {"channel": payload.get("channel", "email (mock)"), "delivered": True}


@activity.defn
async def verify_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    key = _track_activity("Verifying the account baseline", request_id=payload.get("request_id"))
    outputs, spec = payload.get("outputs", {}), payload.get("spec", {})
    speed = float(STORE.read().get("speed", 1.0) or 1.0)
    checks = [
        ("Account exists and is in the workload OU", bool(outputs.get("account_id")), outputs.get("ou_path") or outputs.get("account_id")),
        ("Guardrail SCPs attached", bool(outputs.get("scps")), f"{len(outputs.get('scps') or [])} policies"),
        ("Network allocated and routable", bool(outputs.get("vpc_id")), outputs.get("vpc_cidr")),
        ("Threat detection enabled", bool(outputs.get("guardduty_detector_id")), "GuardDuty + Security Hub"),
        ("Budget alerts configured", bool(outputs.get("budget_name")), outputs.get("budget_name")),
        ("Registered in CMDB", bool(outputs.get("ci_number")), outputs.get("ci_number")),
    ]
    results = []
    for name, ok, detail in checks:
        await asyncio.sleep(0.4 * speed)
        activity.heartbeat(name)
        worker_stats.progress(key, name, None)
        results.append({"name": name, "ok": bool(ok), "detail": str(detail or "")})
    for sysid in ("aws_orgs",):
        try:
            results.extend(await connectors.get(sysid).verify(spec, outputs))
        except Exception as e:  # noqa: BLE001
            results.append({"name": f"{sysid} live check", "ok": False, "detail": str(e)[:160]})
    worker_stats.end(key, ok=True)
    return {"checks": results, "passed": all(r["ok"] for r in results)}


CORE_ACTIVITIES = [
    run_step, compensate_step, ai_parse_request, ai_risk_review, ai_diagnose_failure, ai_completion_summary,
    agent_llm_turn, agent_tool,
    plan_request, itsm_create_change, itsm_update_change, notify, verify_request,
]
ONPREM_ACTIVITIES = [run_step, compensate_step]
