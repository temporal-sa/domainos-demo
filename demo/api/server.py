"""Demo API + UI server.

    python -m demo.api.server        (or: uvicorn demo.api.server:app --port 8800)

Serves the control-room UI, starts requests, relays approvals and operator
decisions to the workflows as Temporal Updates, and drives the demo controls
(worker processes, failure injection, pacing).
"""

import asyncio
import contextlib
import hashlib
import inspect
import logging
import random
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from temporalio.client import WorkflowUpdateFailedError
from temporalio.exceptions import WorkflowAlreadyStartedError

from .. import frameworks, settings
from ..bugs import BUGS
from ..catalog import find_fault
from ..chaos import RuntimeStore
from ..models import AgentInput, ApprovalDecision, DemoOptions, Resolution, ReviewDecision, VendingInput
from ..temporal_client import connect
from . import code, lab, view

log = logging.getLogger(__name__)
from .aggregator import Aggregator
from .supervisor import POOLS, WorkerSupervisor

STATIC = settings.ROOT / "demo" / "static"
UPDATE_TIMEOUT = timedelta(seconds=12)

store = RuntimeStore()
supervisor = WorkerSupervisor()
agg = Aggregator(supervisor)


async def _connect_loop() -> None:
    delay = 1.0
    while agg.client is None:
        try:
            agg.client = await connect(identity="domainos-ui")
            agg.connect_error = None
        except Exception as e:  # noqa: BLE001
            agg.connect_error = f"Cannot reach Temporal at {settings.TEMPORAL_ADDRESS}: {e}"
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10)


async def _deploy_loop() -> None:
    """Advance rolling deploys and redeploy workers when worker code changes on disk."""
    while True:
        try:
            supervisor.tick(auto=bool(store.read().get("deploy", {}).get("auto", True)))
        except Exception as e:  # noqa: BLE001 - never let the watcher die
            supervisor.last_deploy = {"at": None, "state": "failed", "error": f"Deploy watcher: {e}"}
        await asyncio.sleep(1.0)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    tasks = [asyncio.create_task(_connect_loop()), asyncio.create_task(_deploy_loop()), asyncio.create_task(_lab_loop())]
    await asyncio.to_thread(frameworks.availability)  # import checks for LangChain/LangGraph, once
    if supervisor.managed:
        for pool, n in settings.parse_start_workers().items():
            if pool in POOLS:
                supervisor.scale(pool, n)
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        supervisor.shutdown()


app = FastAPI(title="Domainos", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _client():
    if agg.client is None:
        raise HTTPException(503, agg.connect_error or "Not connected to Temporal yet")
    return agg.client


def _update_error(e: Exception) -> HTTPException:
    if isinstance(e, WorkflowUpdateFailedError):
        return HTTPException(409, str(getattr(e, "cause", None) or e))
    msg = str(e)
    if "deadline" in msg.lower() or "timeout" in msg.lower() or isinstance(e, asyncio.TimeoutError):
        return HTTPException(503, "No core worker answered. Start a core worker so the workflow can apply this.")
    return HTTPException(500, msg)


# --- UI ----------------------------------------------------------------------

def _asset_version(name: str) -> str:
    try:
        return hashlib.sha1((STATIC / name).read_bytes()).hexdigest()[:10]
    except OSError:
        return "0"


def index_html() -> str:
    """index.html with the script and stylesheet URLs tagged by content hash.

    A browser can keep a cached app.js after the demo is updated (it showed the
    previous Agent lab). A new hash means a new URL, so an update is always picked up.
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "app.css"):
        html = html.replace(f'"/static/{name}"', f'"/static/{name}?v={_asset_version(name)}"')
    return html


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse(index_html(), headers={"Cache-Control": "no-store"})


@app.get("/api/snapshot")
async def snapshot(request: Optional[str] = None, lab: bool = False) -> JSONResponse:
    snap = await agg.snapshot(request, store.read(), lab=lab)
    return JSONResponse(snap, headers={"Cache-Control": "no-store"})


@app.get("/api/presets")
async def presets() -> Any:
    return view.presets()


# --- requests ----------------------------------------------------------------

class NewRequest(BaseModel):
    text: str
    requester: str = "Demo requester"


@app.post("/api/requests")
async def create_request(body: NewRequest) -> Dict[str, str]:
    client = _client()
    if not body.text.strip():
        raise HTTPException(400, "Describe the account you need")
    rt = store.read()
    demo = DemoOptions(
        auto_approve_seconds=int(rt["approvals"].get("auto_approve_seconds", 0)),
        approval_sla_seconds=int(rt["approvals"].get("sla_seconds", 60)),
        max_attempts=int(rt["retries"].get("max_attempts", 6)),
        heartbeat_seconds=int(rt["retries"].get("heartbeat_seconds", 8)),
        # Requests placed in bug mode retry the bugged step until a fix is deployed.
        retry_forever=[BUGS[b]["step"] for b, on in rt.get("bugs", {}).items() if on and b in BUGS],
        agent_mode=_agent_mode(rt),
    )
    for _ in range(5):
        rid = f"REQ-{random.randint(1000, 9999)}"
        wf_id = f"{settings.WORKFLOW_ID_PREFIX}{rid.lower()}"
        kwargs = _summary_kwargs(client, f"{rid}: {body.text[:120]}")
        try:
            await client.start_workflow(
                "AccountVendingWorkflow",
                VendingInput(request_id=rid, request_text=body.text.strip(), requester=body.requester.strip() or "Demo requester", demo=demo),
                id=wf_id,
                task_queue=settings.CORE_QUEUE,
                memo={"request_id": rid, "requester": body.requester, "text": body.text[:160]},
                **kwargs,
            )
        except WorkflowAlreadyStartedError:
            continue
        agg._list_cache = (0.0, [])
        return {"id": rid, "workflow_id": wf_id}
    raise HTTPException(500, "Could not allocate a request id")


def _agent_mode(rt: Dict[str, Any]) -> str:
    mode = rt.get("agent_mode") or "plain"
    return mode if frameworks.check(mode) is None else "plain"


def _summary_kwargs(client: Any, summary: str) -> Dict[str, Any]:
    try:
        if "static_summary" in inspect.signature(client.start_workflow).parameters:
            return {"static_summary": summary}
    except (TypeError, ValueError):
        pass
    return {}


class Decision(BaseModel):
    decision: str
    approver: str = ""
    comment: str = ""


@app.post("/api/requests/{wf_id}/approvals/{approval_id}")
async def decide(wf_id: str, approval_id: str, body: Decision) -> Any:
    try:
        return await _client().get_workflow_handle(wf_id).execute_update(
            "decide_approval", ApprovalDecision(approval_id, body.decision, body.approver, body.comment),
            result_type=dict, rpc_timeout=UPDATE_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        raise _update_error(e) from e


class Resolve(BaseModel):
    action: str
    operator: str = "Platform on-call"
    note: str = ""


@app.post("/api/tracks/{child_wf_id}/resolve")
async def resolve(child_wf_id: str, body: Resolve) -> Any:
    try:
        return {"result": await _client().get_workflow_handle(child_wf_id).execute_update(
            "resolve", Resolution(body.action, body.operator, body.note), result_type=str, rpc_timeout=UPDATE_TIMEOUT)}
    except Exception as e:  # noqa: BLE001
        raise _update_error(e) from e


class Review(BaseModel):
    decision: str
    comment: str = ""
    reviewer: str = "Network Security on-call"


@app.post("/api/agents/{agent_wf_id}/review")
async def review(agent_wf_id: str, body: Review) -> Any:
    try:
        return {"result": await _client().get_workflow_handle(agent_wf_id).execute_update(
            "review_draft", ReviewDecision(body.decision, body.reviewer, body.comment), result_type=str, rpc_timeout=UPDATE_TIMEOUT)}
    except Exception as e:  # noqa: BLE001
        raise _update_error(e) from e


class Cancel(BaseModel):
    reason: str = "Cancelled by requester"


@app.post("/api/requests/{wf_id}/cancel")
async def cancel(wf_id: str, body: Cancel) -> Any:
    try:
        return {"result": await _client().get_workflow_handle(wf_id).execute_update("cancel_request", body.reason, result_type=str, rpc_timeout=UPDATE_TIMEOUT)}
    except Exception as e:  # noqa: BLE001
        raise _update_error(e) from e


# --- workers -----------------------------------------------------------------

class Spawn(BaseModel):
    pool: str


@app.post("/api/workers")
async def spawn(body: Spawn) -> Dict[str, str]:
    try:
        return {"id": supervisor.spawn(body.pool)}
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e)) from e


class Kill(BaseModel):
    graceful: bool = False


@app.post("/api/workers/{wid}/kill")
async def kill(wid: str, body: Kill) -> Dict[str, str]:
    try:
        supervisor.kill(wid, graceful=body.graceful)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    return {"id": wid, "action": "stop" if body.graceful else "kill"}


class Scale(BaseModel):
    count: int


@app.post("/api/pools/{pool}/scale")
async def scale(pool: str, body: Scale) -> Dict[str, Any]:
    try:
        return {"changed": supervisor.scale(pool, body.count)}
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/pools/{pool}/kill-all")
async def kill_all(pool: str) -> Dict[str, Any]:
    killed = []
    for w in supervisor.list():
        if w["pool"] == pool and w["status"] in ("running", "starting", "unresponsive"):
            with contextlib.suppress(KeyError):
                supervisor.kill(w["id"])
                killed.append(w["id"])
    return {"killed": killed}


# --- failure injection & pacing ------------------------------------------------

@app.post("/api/faults/{fault_id}")
async def arm(fault_id: str) -> Dict[str, Any]:
    preset = find_fault(fault_id)
    if not preset:
        raise HTTPException(404, f"No fault preset '{fault_id}'")
    store.arm_fault(preset)
    return {"armed": fault_id}


@app.delete("/api/faults/{fault_id}")
async def disarm(fault_id: str) -> Dict[str, Any]:
    store.disarm_fault(fault_id)
    return {"disarmed": fault_id}


class SystemChaos(BaseModel):
    error_rate: Optional[float] = None
    latency_ms: Optional[int] = None


@app.put("/api/systems/{system}")
async def system_chaos(system: str, body: SystemChaos) -> Dict[str, Any]:
    return store.set_system(system, body.error_rate, body.latency_ms)["systems"]


class SettingsBody(BaseModel):
    speed: Optional[float] = None
    auto_approve_seconds: Optional[int] = None
    sla_seconds: Optional[int] = None
    max_attempts: Optional[int] = None
    heartbeat_seconds: Optional[int] = None


@app.put("/api/settings")
async def put_settings(body: SettingsBody) -> Dict[str, Any]:
    def apply(d: Dict[str, Any]) -> None:
        if body.speed is not None:
            d["speed"] = max(0.1, min(4.0, body.speed))
        if body.auto_approve_seconds is not None:
            d["approvals"]["auto_approve_seconds"] = max(0, body.auto_approve_seconds)
        if body.sla_seconds is not None:
            d["approvals"]["sla_seconds"] = max(0, body.sla_seconds)
        if body.max_attempts is not None:
            d["retries"]["max_attempts"] = max(1, min(20, body.max_attempts))
        if body.heartbeat_seconds is not None:
            d["retries"]["heartbeat_seconds"] = max(3, min(60, body.heartbeat_seconds))

    d = store.update(apply)
    return {k: d[k] for k in ("speed", "approvals", "retries")}


# --- code: snippets, bug mode, deploys ---------------------------------------------

@app.get("/api/code")
async def code_view() -> JSONResponse:
    return JSONResponse({"snippets": code.snippets(), "bugs": code.bugs_status(store.read()), **supervisor.status()},
                        headers={"Cache-Control": "no-store"})


class Toggle(BaseModel):
    enabled: bool


@app.put("/api/bugs/{bug_id}")
async def set_bug(bug_id: str, body: Toggle) -> Dict[str, Any]:
    if bug_id not in BUGS:
        raise HTTPException(404, f"No bug '{bug_id}'")
    store.update(lambda d: d.setdefault("bugs", {}).__setitem__(bug_id, body.enabled))
    return {"bug": bug_id, "enabled": body.enabled}


@app.put("/api/deploy/auto")
async def set_auto_deploy(body: Toggle) -> Dict[str, Any]:
    store.update(lambda d: d.setdefault("deploy", {}).__setitem__("auto", body.enabled))
    return {"auto": body.enabled}


@app.post("/api/deploy")
async def deploy_now() -> Dict[str, Any]:
    if not supervisor.managed:
        raise HTTPException(400, "Workers are managed outside the demo (DEMO_MANAGE_WORKERS=false)")
    return supervisor.deploy("manual")


@app.post("/api/reset-chaos")
async def reset_chaos() -> Dict[str, Any]:
    store.update(lambda d: (d.__setitem__("faults", []), d.__setitem__("systems", {})))
    return {"ok": True}


# --- agent modes and the Agent lab ---------------------------------------------------

@app.get("/api/frameworks")
async def list_frameworks() -> Dict[str, Any]:
    return {"modes": frameworks.availability(), "agent_mode": _agent_mode(store.read())}


class ModeBody(BaseModel):
    mode: str


@app.put("/api/agent-mode")
async def set_agent_mode(body: ModeBody) -> Dict[str, Any]:
    reason = frameworks.check(body.mode)
    if reason:
        raise HTTPException(400, reason)
    store.update(lambda d: d.__setitem__("agent_mode", body.mode))
    return {"agent_mode": body.mode}


class LabStart(BaseModel):
    modes: Optional[List[str]] = None  # default: every installed runtime
    scenario: str = "happy"


@app.get("/api/lab/content")
async def lab_content() -> Dict[str, Any]:
    return lab.content()


def _lab_state() -> Dict[str, Any]:
    st = store.read().get("lab") or {}
    return {"runs": lab.lab_runs(st), "crashes": list(st.get("crashes") or [])[-20:], "cmdb_by_lab": bool(st.get("cmdb_by_lab"))}


@app.post("/api/lab/runs")
async def lab_start(body: LabStart) -> Dict[str, Any]:
    """Start the firewall agent on one runtime, or several, with a scenario."""
    client = _client()
    modes = [m for m in (body.modes or list(frameworks.MODES)) if frameworks.check(m) is None]
    if not modes:
        raise HTTPException(400, "That runtime isn't installed here: " + "; ".join(filter(None, (frameworks.check(m) for m in body.modes or []))))
    sc = lab.scenario(body.scenario)
    n = random.randint(1000, 9999)
    rid = f"LAB-{n}"
    hb = int(store.read()["retries"].get("heartbeat_seconds", 8))
    started: Dict[str, Dict[str, Any]] = {}
    for m in modes:
        # Arm this scenario's model-call failures before the first turn can run.
        if sc.get("model_faults"):
            store.arm_fault({"id": f"lab_429_{m}", "label": f"Claude 429s ({m})", "kind": "transient", "target": f"agent-{m}.llm_turn",
                             "error_type": "RateLimitError", "times": int(sc["model_faults"]),
                             "message": "429 rate_limit_error: this request would exceed the rate limit for your organization (injected by the Agent lab)"})
        wf_id = f"lab-{n}-{m}"
        await client.start_workflow(
            frameworks.MODES[m]["workflow"],
            AgentInput(request_id=rid, parent_workflow_id="", agent="firewall_rules", spec={**lab.LAB_SPEC, "request_id": rid},
                       context=dict(lab.LAB_CONTEXT), reviewer=lab.LAB_REVIEWER, mode=m, heartbeat_seconds=max(hb, 5)),
            id=wf_id,
            task_queue=settings.CORE_QUEUE,
            **_summary_kwargs(client, f"Agent lab {rid}: firewall agent on {frameworks.MODES[m]['label']} ({sc.get('label')})"),
        )
        started[m] = {"mode": m, "workflow_id": wf_id, "scenario": sc.get("id"), "started_at": time.time(), "crashed": False}

    def save(d: Dict[str, Any]) -> None:
        st = d.get("lab") if isinstance(d.get("lab"), dict) else {}
        runs = lab.lab_runs(st)
        runs.update(started)
        st["runs"] = runs
        st.setdefault("crashes", [])
        if sc.get("cmdb_error_rate"):
            d.setdefault("systems", {}).setdefault("servicenow", {})["error_rate"] = float(sc["cmdb_error_rate"])
            st["cmdb_by_lab"] = True
        d["lab"] = st

    store.update(save)
    return {"scenario": sc.get("id"), "runs": started}


async def _crash_core(respawn_after: float = 3.0) -> Dict[str, Any]:
    """Kill every core worker (SIGKILL), then start the same number again."""
    alive = [w for w in supervisor.list() if w["pool"] == "core" and w["status"] in ("running", "starting", "unresponsive")]
    killed = []
    for w in alive:
        with contextlib.suppress(KeyError):
            supervisor.kill(w["id"])
            killed.append(w["id"])
    def record(d: Dict[str, Any]) -> None:
        if not isinstance(d.get("lab"), dict):
            d["lab"] = {}
        d["lab"].setdefault("crashes", []).append({"at": time.time(), "killed": killed})

    store.update(record)
    asyncio.create_task(_respawn("core", max(1, len(alive)), max(0.0, min(30.0, respawn_after))))
    return {"killed": killed, "respawn_after": respawn_after}


class Crash(BaseModel):
    respawn_after: float = 3.0


async def _respawn(pool: str, count: int, delay: float) -> None:
    await asyncio.sleep(delay)
    with contextlib.suppress(Exception):
        supervisor.scale(pool, count)


@app.post("/api/lab/crash")
async def lab_crash(body: Crash) -> Dict[str, Any]:
    if not supervisor.managed:
        raise HTTPException(400, "Workers are managed outside the demo (DEMO_MANAGE_WORKERS=false)")
    return await _crash_core(body.respawn_after)


MODEL_TURN_ACTIVITIES = ("agent_llm_turn", "lc_llm_turn")
DONE = ("awaiting_review", "approved", "rejected", "failed")


async def _lab_loop() -> None:
    """Carry out the scenarios that need timing: crash mid-turn, and put the CMDB back afterwards."""
    while True:
        await asyncio.sleep(0.5)
        try:
            if agg.client is None:
                continue
            st = _lab_state()
            runs = st["runs"]
            crash_runs = {m: r for m, r in runs.items() if r.get("scenario") == "crash" and not r.get("crashed")}
            watch = crash_runs if crash_runs else {}
            if st["cmdb_by_lab"]:
                watch = runs
            if not watch:
                continue
            views = dict(zip(watch, await asyncio.gather(*[agg._agent(r["workflow_id"], None, True) for r in watch.values()])))

            # Crash when the scenario's model turn is in flight in any run that asked for it.
            turn = int(lab.scenario("crash").get("crash_at_turn", 3))
            for m in crash_runs:
                a = views.get(m) or {}
                if a.get("status") in DONE:
                    continue
                done_turns = int((a.get("stats") or {}).get("llm_calls") or 0)
                pending = a.get("pending") or []
                in_turn = any(p.get("activity_type") in MODEL_TURN_ACTIVITIES for p in pending) or \
                    any((p.get("heartbeat") or {}).get("node") == "agent" for p in pending)
                if done_turns >= turn - 1 and in_turn and supervisor.managed:
                    await _crash_core(3.0)

                    def mark(d: Dict[str, Any]) -> None:
                        for r in lab.lab_runs(d.get("lab")).values():
                            if r.get("scenario") == "crash":
                                r["crashed"] = True

                    store.update(mark)
                    break

            # Flaky CMDB lasts while a flaky-scenario run is still drafting.
            if st["cmdb_by_lab"]:
                drafting = [m for m, r in runs.items() if r.get("scenario") == "flaky_cmdb" and (views.get(m) or {}).get("status") not in DONE]
                if not drafting:
                    def reset(d: Dict[str, Any]) -> None:
                        d.setdefault("systems", {}).setdefault("servicenow", {})["error_rate"] = 0.0
                        if isinstance(d.get("lab"), dict):
                            d["lab"]["cmdb_by_lab"] = False
                    store.update(reset)
        except Exception:  # noqa: BLE001 - the lab loop must never take the server down
            log.exception("Agent lab loop")


class LabReview(BaseModel):
    decision: str
    comment: str = ""
    modes: Optional[List[str]] = None


@app.post("/api/lab/review")
async def lab_review(body: LabReview) -> Dict[str, Any]:
    """Send one review decision to every lab run (or the given runtimes) that's waiting for one."""
    client = _client()
    runs = [r for m, r in _lab_state()["runs"].items() if not body.modes or m in body.modes]

    async def send(r: Dict[str, Any]) -> Optional[str]:
        try:
            await client.get_workflow_handle(r["workflow_id"]).execute_update(
                "review_draft", ReviewDecision(body.decision, lab.LAB_REVIEWER, body.comment), result_type=str, rpc_timeout=UPDATE_TIMEOUT)
            return r["mode"]
        except Exception:  # noqa: BLE001 - not waiting for review yet, or already decided
            return None

    sent = [m for m in await asyncio.gather(*[send(r) for r in runs]) if m]
    if not sent:
        raise HTTPException(409, "No lab run is waiting for a review right now")
    return {"sent": sent}

def main() -> None:
    import uvicorn

    print(f"\n  Domainos UI: http://{settings.API_HOST}:{settings.API_PORT}\n  Temporal: {settings.TEMPORAL_ADDRESS} ({settings.TEMPORAL_NAMESPACE})  UI links: {settings.temporal_ui_base()}\n")
    uvicorn.run(app, host=settings.API_HOST, port=settings.API_PORT, log_level="warning")


if __name__ == "__main__":
    main()
