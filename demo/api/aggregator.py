"""Builds UI snapshots from Temporal.

Three sources, on purpose:
  - Queries (get_state, get_steps): the workflow's own view of the request.
    Answered by a worker replaying history, so they pause when no worker runs.
  - DescribeWorkflowExecution: server-side facts that work with zero workers:
    pending activities, attempt numbers, last failure, last worker identity,
    next retry time.
  - DescribeTaskQueue: which workers are polling, and the backlog.
When queries fail, the last good state is shown, flagged as stale, alongside
the live server-side data. That is the "kill every worker" moment in the demo.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from temporalio.client import Client, WorkflowExecutionStatus

from .. import frameworks, settings
from . import code, view
from . import lab as lab_view  # not "lab": snapshot() takes a lab flag
from .supervisor import POOLS, WorkerSupervisor

TERMINAL_TRACK = {"completed", "skipped", "rolled_back", "cancelled", "failed"}
QUERY_TIMEOUT = timedelta(seconds=2.5)


def _ts(pb) -> Optional[str]:
    try:
        if pb is None or (pb.seconds == 0 and pb.nanos == 0):
            return None
        return datetime.fromtimestamp(pb.seconds + pb.nanos / 1e9, tz=timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return None


def _status_name(status: Optional[WorkflowExecutionStatus]) -> str:
    return status.name.lower() if status is not None else "unknown"


class Aggregator:
    def __init__(self, supervisor: WorkerSupervisor):
        self.client: Optional[Client] = None
        self.connect_error: Optional[str] = None
        self.sup = supervisor
        self.links = view.default_links()
        self._state_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._final_states: Dict[str, Dict[str, Any]] = {}
        self._steps_cache: Dict[str, Dict[str, Any]] = {}
        self._final_steps: Dict[str, Dict[str, Any]] = {}
        self._run_ids: Dict[str, str] = {}
        self._list_cache: Tuple[float, List[Dict[str, Any]]] = (0.0, [])
        self._pool_cache: Tuple[float, List[Dict[str, Any]]] = (0.0, [])
        self._static = view.static_payload()

    # --- low-level helpers -------------------------------------------------------
    async def _query(self, wf_id: str, name: str) -> Optional[Dict[str, Any]]:
        try:
            return await self.client.get_workflow_handle(wf_id).query(name, rpc_timeout=QUERY_TIMEOUT)
        except Exception:  # noqa: BLE001 - no worker, not found, timeout
            return None

    async def _describe(self, wf_id: str):
        try:
            return await self.client.get_workflow_handle(wf_id).describe()
        except Exception:  # noqa: BLE001
            return None

    async def _pending(self, desc) -> List[Dict[str, Any]]:
        out = []
        if desc is None:
            return out
        try:
            from temporalio.api.enums.v1 import PendingActivityState
        except ImportError:  # pragma: no cover
            PendingActivityState = None  # type: ignore
        now = datetime.now(timezone.utc)
        for pa in desc.raw_description.pending_activities:
            hb: Dict[str, Any] = {}
            try:
                if pa.HasField("heartbeat_details") and pa.heartbeat_details.payloads:
                    decoded = await self.client.data_converter.decode(pa.heartbeat_details.payloads)
                    if decoded and isinstance(decoded[-1], dict):
                        hb = decoded[-1]
            except Exception:  # noqa: BLE001
                hb = {}
            state = "unknown"
            if PendingActivityState is not None:
                state = PendingActivityState.Name(pa.state).replace("PENDING_ACTIVITY_STATE_", "").lower()
            failure = None
            if pa.HasField("last_failure"):
                f = pa.last_failure
                ftype = f.application_failure_info.type if f.HasField("application_failure_info") else (
                    "Timeout" if f.HasField("timeout_failure_info") else "Failure")
                failure = {"message": f.message, "type": ftype}
            nxt = _ts(pa.next_attempt_schedule_time) if pa.HasField("next_attempt_schedule_time") else None
            nxt_future = False
            if nxt:
                try:
                    nxt_future = datetime.fromisoformat(nxt) > now
                except ValueError:
                    pass
            wait_from = nxt or _ts(pa.scheduled_time)
            waiting_s = None
            if state == "scheduled" and wait_from:
                try:
                    waiting_s = max(0.0, (now - datetime.fromisoformat(wait_from)).total_seconds())
                except ValueError:
                    waiting_s = None
            out.append({
                "waiting_s": waiting_s,
                "activity_id": pa.activity_id,
                "activity_type": pa.activity_type.name,
                "state": state,
                "attempt": pa.attempt,
                "max_attempts": pa.maximum_attempts,
                "last_failure": failure,
                "last_worker": (pa.last_worker_identity or "").split("@")[0] or None,
                "scheduled_at": _ts(pa.scheduled_time),
                "last_started_at": _ts(pa.last_started_time),
                "last_heartbeat_at": _ts(pa.last_heartbeat_time),
                "next_attempt_at": nxt,
                "next_attempt_at_future": nxt_future,
                "heartbeat": {k: hb.get(k) for k in ("step", "pct", "log", "worker", "checkpoint", "node", "tool", "turn") if k in hb},
                "step": hb.get("step"),
            })
        return out

    async def _state(self, wf_id: str) -> Tuple[Optional[Dict[str, Any]], bool, Optional[float]]:
        """Returns (state, stale, stale_since)."""
        if wf_id in self._final_states:
            return self._final_states[wf_id], False, None
        st = await self._query(wf_id, "get_state")
        if st is not None:
            self._state_cache[wf_id] = (time.time(), st)
            return st, False, None
        cached = self._state_cache.get(wf_id)
        return (cached[1] if cached else None), True, (cached[0] if cached else None)

    async def _child(self, track: Dict[str, Any], live_needed: bool) -> Dict[str, Any]:
        wf_id = track.get("workflow_id")
        if not wf_id:
            return {}
        if wf_id in self._final_steps:
            return self._final_steps[wf_id]
        data: Dict[str, Any] = {}
        if live_needed:
            steps_q, desc = await asyncio.gather(self._query(wf_id, "get_steps"), self._describe(wf_id))
        else:
            steps_q, desc = await self._query(wf_id, "get_steps"), None
        if steps_q:
            self._steps_cache[wf_id] = steps_q
        steps_q = steps_q or self._steps_cache.get(wf_id) or {}
        data["steps"] = steps_q.get("steps", [])
        if desc is not None:
            data["run_id"] = desc.run_id
            self._run_ids[wf_id] = desc.run_id
            pend = await self._pending(desc)
            running = [s["id"] for s in data["steps"] if s.get("status") in ("running", "compensating")]
            by_step: Dict[str, Dict[str, Any]] = {}
            for p in pend:
                if p["activity_type"] not in ("run_step", "compensate_step"):
                    continue
                sid = p.get("step") or (running[0] if len(running) == 1 else None)
                if sid:
                    by_step[sid] = p
            data["pending"] = by_step
        else:
            data["run_id"] = self._run_ids.get(wf_id)
        agents: Dict[str, Dict[str, Any]] = {}
        for st in data["steps"]:
            if st.get("agent_workflow_id"):
                agents[st["id"]] = await self._agent(st["agent_workflow_id"], st.get("agent_run_id"), live_needed)
        data["agents"] = agents
        if track.get("status") in TERMINAL_TRACK and steps_q:
            self._final_steps[wf_id] = data
        return data

    async def _agent(self, wf_id: str, run_id: Optional[str], live_needed: bool) -> Dict[str, Any]:
        """State of an agent child workflow: transcript, draft, review status, live turn."""
        if wf_id in self._final_steps:
            return self._final_steps[wf_id]
        if live_needed:
            q, desc = await asyncio.gather(self._query(wf_id, "get_agent"), self._describe(wf_id))
        else:
            q, desc = await self._query(wf_id, "get_agent"), None
        if q:
            self._steps_cache[wf_id] = q
        q = q or self._steps_cache.get(wf_id) or {}
        run = (desc.run_id if desc else None) or run_id or self._run_ids.get(wf_id)
        if run:
            self._run_ids[wf_id] = run
        out = {**q, "workflow_id": wf_id, "run_id": run, "link": self.links.workflow(wf_id, run),
               "pending": await self._pending(desc) if desc else [],
               "temporal_status": _status_name(desc.status) if desc else None}
        if out["temporal_status"] in ("failed", "terminated", "timed_out", "canceled") and q.get("status") not in ("approved", "rejected"):
            out["status"] = "failed"
        if out.get("status") in ("approved", "rejected", "failed") and (out["temporal_status"] or "running") != "running":
            self._final_steps[wf_id] = {**out, "pending": []}
        return out

    async def lab(self, st: Dict[str, Any], systems: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """The latest Agent lab run for each runtime (each started on its own, or all together)."""
        runs = lab_view.lab_runs(st)
        crashes = st.get("crashes") or []
        modes = [m for m in frameworks.MODES if m in runs]
        views = await asyncio.gather(*[self._agent(runs[m]["workflow_id"], None, True) for m in modes])
        return {
            "runs": {m: lab_view.run_view(m, runs[m], v, crashes) for m, v in zip(modes, views)},
            "links": {"runs": self.links.family("lab-"), "queue": self.links.task_queue(settings.CORE_QUEUE)},
            "change": lab_view.LAB_CHANGE,
            "cmdb_error_rate": float((systems or {}).get("servicenow", {}).get("error_rate", 0) or 0),
            "input": {"account": lab_view.LAB_SPEC["account_name"], "vpc_cidr": lab_view.LAB_CONTEXT["vpc_cidr"], "targets": lab_view.LAB_SPEC["onprem_targets"]},
        }

    # --- public -------------------------------------------------------------------
    async def requests(self) -> List[Dict[str, Any]]:
        if time.time() - self._list_cache[0] < 2.5:
            return self._list_cache[1]
        rows = []
        try:
            n = 0
            async for wf in self.client.list_workflows('WorkflowType = "AccountVendingWorkflow"'):
                n += 1
                if n > 25:
                    break
                status = _status_name(wf.status)
                if wf.id in self._final_states or n > 12:
                    state = self._final_states.get(wf.id) or (self._state_cache.get(wf.id) or (0, None))[1]
                else:
                    state, _, _ = await self._state(wf.id)
                    if state and status != "running":
                        self._final_states[wf.id] = state
                memo = {}
                try:
                    memo = await wf.memo()
                except Exception:  # noqa: BLE001
                    memo = {}
                rows.append(view.summary_row(wf.id, state, status, wf.start_time.isoformat() if wf.start_time else None, memo))
        except Exception as e:  # noqa: BLE001
            self.connect_error = f"Listing workflows failed: {e}"
            return self._list_cache[1]
        self._list_cache = (time.time(), rows)
        return rows

    async def request_view(self, wf_id: str) -> Optional[Dict[str, Any]]:
        desc = await self._describe(wf_id)
        state, stale, stale_since = await self._state(wf_id)
        if state is None:
            if desc is None:
                return None
            return {"id": wf_id, "workflow_id": wf_id, "title": "Waiting for a worker to start this request", "stale": True,
                    "status": "in_progress", "stage": "intake", "tracks": [], "approvals": [], "events": [],
                    "headline": {"text": "Queued: no core worker has picked this request up yet", "tone": "warn"},
                    "links": {"workflow": self.links.workflow(wf_id, desc.run_id), "family": self.links.family(wf_id)},
                    "parent_pending": [], "stage_times": {}}
        temporal_status = _status_name(desc.status) if desc else "unknown"
        if desc and temporal_status != "running" and not stale:
            self._final_states[wf_id] = state
        parent_pending = await self._pending(desc) if desc and temporal_status == "running" else []

        live = self.sup.current_by_step()
        tracks = state.get("tracks", [])
        results = await asyncio.gather(*[self._child(t, t.get("status") not in TERMINAL_TRACK) for t in tracks])
        children = {t["id"]: r for t, r in zip(tracks, results)}
        return view.build_request(
            state,
            wf_id=wf_id,
            run_id=desc.run_id if desc else None,
            temporal_status=temporal_status,
            children=children,
            live=live,
            parent_pending=parent_pending,
            links=self.links,
            stale=stale,
            stale_since=stale_since,
            history_length=getattr(desc, "history_length", None) if desc else None,
        )

    async def pools(self) -> List[Dict[str, Any]]:
        if time.time() - self._pool_cache[0] < 1.5:
            pollers_backlog = self._pool_cache[1]
        else:
            pollers_backlog = []
            for pid, meta in POOLS.items():
                pollers, backlog = await self._task_queue(meta["queue"], workflow=(pid == "core"))
                pollers_backlog.append({"id": pid, "pollers": pollers, "backlog": backlog})
            self._pool_cache = (time.time(), pollers_backlog)
        workers = self.sup.list()
        out = []
        for pb in pollers_backlog:
            meta = POOLS[pb["id"]]
            pool_workers = [dict(w) for w in workers if w["pool"] == pb["id"]]
            pollers = view.classify_pollers(pb["pollers"], pool_workers)
            out.append({
                "id": pb["id"], **meta, "queue_url": self.links.task_queue(meta["queue"]),
                "managed": self.sup.managed,
                "workers": pool_workers,
                "pollers": pollers, "backlog": pb["backlog"],
                # Live demo workers Temporal sees; with workers managed elsewhere, any recent poller.
                "polling": sum(1 for p in pollers if p["state"] == "alive" or (p["state"] == "recent" and not self.sup.managed)),
                "listed_gone": [p for p in pollers if p["state"] in ("dead", "quiet")],
                "listed_other": [p["identity"] for p in pollers if not p["worker"]],
            })
        return out

    async def _task_queue(self, queue: str, workflow: bool) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        if self.client is None:
            return [], None
        try:
            from temporalio.api.enums.v1 import TaskQueueType
            from temporalio.api.taskqueue.v1 import TaskQueue
            from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
        except ImportError:  # pragma: no cover
            return [], None
        pollers: Dict[str, Dict[str, Any]] = {}
        backlog: Optional[int] = None
        types = [TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY] + ([TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW] if workflow else [])
        for tq_type in types:
            req = DescribeTaskQueueRequest(namespace=settings.TEMPORAL_NAMESPACE, task_queue=TaskQueue(name=queue), task_queue_type=tq_type)
            try:
                req.report_stats = True  # newer servers return backlog stats
            except (AttributeError, ValueError):
                pass
            try:
                resp = await self.client.workflow_service.describe_task_queue(req)
            except Exception:  # noqa: BLE001
                try:
                    req = DescribeTaskQueueRequest(namespace=settings.TEMPORAL_NAMESPACE, task_queue=TaskQueue(name=queue), task_queue_type=tq_type)
                    resp = await self.client.workflow_service.describe_task_queue(req)
                except Exception:  # noqa: BLE001
                    continue
            for p in resp.pollers:
                ident = (p.identity or "").split("@")[0]
                pollers[ident] = {"identity": ident, "last_access": _ts(p.last_access_time)}
            if tq_type == TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY:
                try:
                    if resp.HasField("stats"):
                        backlog = int(resp.stats.approximate_backlog_count)
                except (ValueError, AttributeError):
                    pass
                if backlog is None:
                    try:
                        if resp.HasField("task_queue_status"):
                            backlog = int(resp.task_queue_status.backlog_count_hint)
                    except (ValueError, AttributeError):
                        pass
        return sorted(pollers.values(), key=lambda p: p["identity"]), backlog

    async def snapshot(self, selected: Optional[str], store_data: Dict[str, Any], lab: bool = False) -> Dict[str, Any]:
        snap: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            **self._static,
            "temporal": {
                "connected": self.client is not None,
                "error": self.connect_error,
                "address": settings.TEMPORAL_ADDRESS,
                "namespace": settings.TEMPORAL_NAMESPACE,
                "cloud": settings.is_temporal_cloud(),
                "ui": self.links.ui,
                "namespace_url": self.links.namespace(),
            },
            "ai": {"enabled": settings.ai_configured(), "model": settings.ANTHROPIC_MODEL},
            "settings": {k: store_data.get(k) for k in ("speed", "approvals", "retries")},
            "armed": {f["preset"]: f.get("remaining", 0) for f in store_data.get("faults", [])},
            "system_chaos": store_data.get("systems", {}),
            "requests": [],
            "selected": None,
            "code": {**self.sup.status(), "auto_deploy": bool(store_data.get("deploy", {}).get("auto", True)),
                     "bugs": code.bugs_status(store_data)},
            "frameworks": frameworks.availability(),
            "agent_mode": store_data.get("agent_mode") or "plain",
        }
        snap["pools"] = await self.pools() if self.client else [{"id": k, **v, "workers": [w for w in self.sup.list() if w["pool"] == k], "pollers": [], "backlog": None, "managed": self.sup.managed} for k, v in POOLS.items()]
        if self.client is None:
            return snap
        snap["requests"] = await self.requests()
        if selected:
            snap["selected"] = await self.request_view(selected)
        # Latest lab run per runtime, for the Temporal links on lab snippets in Under the hood.
        snap["lab_links"] = {m: self.links.workflow(r["workflow_id"]) for m, r in lab_view.lab_runs(store_data.get("lab")).items()}
        if lab:
            snap["lab"] = await self.lab(store_data.get("lab") or {}, store_data.get("systems") or {})
        return snap
