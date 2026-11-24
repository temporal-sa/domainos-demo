"""Turns raw workflow state into what the UI renders.

Pure functions, no Temporal imports: the aggregator feeds them live data, the
UI preview harness (dev/ui_preview.py) feeds them simulated data, and both get
the same snapshot shape.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from .. import settings
from ..catalog import depth_map, fault_presets, load_catalog
from ..models import TrackSpec

ACTIVE_TRACK = {"starting", "running", "blocked", "compensating", "finishing"}


# ---------------------------------------------------------------------------
# Temporal UI links
# ---------------------------------------------------------------------------

class Links:
    def __init__(self, ui_base: str, namespace: str):
        self.ui = ui_base.rstrip("/")
        self.ns = namespace

    def namespace(self) -> str:
        return f"{self.ui}/namespaces/{quote(self.ns)}/workflows"

    def workflow(self, wf_id: Optional[str], run_id: Optional[str] = None, tab: str = "history") -> Optional[str]:
        if not wf_id:
            return None
        base = f"{self.ui}/namespaces/{quote(self.ns)}/workflows/{quote(wf_id, safe='')}"
        return f"{base}/{run_id}/{tab}" if run_id else base

    def search(self, query: str) -> str:
        return f"{self.ui}/namespaces/{quote(self.ns)}/workflows?query={quote(query)}"

    def family(self, wf_id: str) -> str:
        return self.search(f'WorkflowId STARTS_WITH "{wf_id}"')

    def task_queue(self, queue: str) -> str:
        return f"{self.ui}/namespaces/{quote(self.ns)}/task-queues/{quote(queue)}"


def default_links() -> Links:
    return Links(settings.temporal_ui_base(), settings.TEMPORAL_NAMESPACE)


# ---------------------------------------------------------------------------
# Static payload (rarely changes)
# ---------------------------------------------------------------------------

def static_payload() -> Dict[str, Any]:
    cat = load_catalog()
    return {
        "brand": {"name": settings.BRAND_NAME, "product": settings.BRAND_PRODUCT, "org": settings.ORG_NAME},
        "stages": cat["stages"],
        "fault_presets": [{k: f.get(k) for k in ("id", "label", "description", "kind", "target", "times")} for f in fault_presets(cat)],
        "systems": [{"id": k, "label": v.get("label", k), "location": v.get("location", "cloud")} for k, v in cat["systems"].items()],
    }


def presets() -> List[Dict[str, str]]:
    return [
        {
            "label": "Prod with borrower data",
            "requester": "Jordan Lee",
            "text": "We need a production AWS account for the Mortgage Analytics team. It will hold borrower PII and has to "
                    "reach the Oracle loan database in the data center. Cost center 4410, budget around $12k a month. "
                    "Owners are jlee@example.com and mpatel@example.com.",
        },
        {
            "label": "Data science sandbox",
            "requester": "Riya Kim",
            "text": "Sandbox account for the Fraud Data Science team to try out SageMaker. No customer data and no "
                    "on-prem access needed. Cost center 7720. Owner is rkim@example.com.",
        },
        {
            "label": "Cloud-only dev service",
            "requester": "Alex Taylor",
            "text": "Dev account for the Digital Banking team's new notification service. Internal data only, fully "
                    "cloud-native, no data center connectivity. Cost center 3105, owner ataylor@example.com.",
        },
        {
            "label": "Card data, mainframe link",
            "requester": "Morgan Diaz",
            "text": "Production account for the Card Services team's tokenization service. It processes PCI cardholder "
                    "data and needs a private link to the mainframe in the data center. Cost center 2290, $20k a month. "
                    "Owner mdiaz@example.com.",
        },
    ]


# ---------------------------------------------------------------------------
# Request view
# ---------------------------------------------------------------------------

def build_steps(track: Dict[str, Any], child: Dict[str, Any], live: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    child_steps = {s["id"]: s for s in (child.get("steps") or [])}
    pending = child.get("pending") or {}
    signal_status = track.get("step_status") or {}
    wf = track.get("workflow_id")
    out = []
    for base in track.get("steps", []):
        sid = base["id"]
        cs = child_steps.get(sid, {})
        status = cs.get("status") or signal_status.get(sid)
        if not status:
            status = "completed" if track.get("status") == "completed" else ("cancelled" if track.get("status") == "cancelled" else "pending")
        step = {
            **base,
            "status": status,
            "attempt": cs.get("attempt"),
            "worker": cs.get("worker"),
            "started_at": cs.get("started_at"),
            "completed_at": cs.get("completed_at"),
            "duration_ms": cs.get("duration_ms"),
            "error": cs.get("error"),
            "diagnosis": cs.get("diagnosis"),
            "log": cs.get("log") or [],
            "output": cs.get("output") or {},
            "history": cs.get("history") or [],
            "kind": base.get("kind") or cs.get("kind") or "action",
            "agent": (child.get("agents") or {}).get(sid),
            "live": None,
        }
        p = pending.get(sid)
        lv = live.get(f"{wf}|{sid}")
        if p or lv:
            step["live"] = {**(p or {})}
            if lv:
                step["live"].update(worker_now=lv.get("worker"), pct=lv.get("pct"), log=lv.get("log") or [], since=lv.get("since"))
            if step["live"].get("attempt"):
                step["attempt"] = step["live"]["attempt"]
            step["worker"] = (lv or {}).get("worker") or (p or {}).get("last_worker") or step["worker"]
        out.append(step)
    return out


def build_request(state: Dict[str, Any], *, wf_id: str, run_id: Optional[str], temporal_status: str,
                  children: Dict[str, Dict[str, Any]], live: Dict[str, Dict[str, Any]], parent_pending: List[Dict[str, Any]],
                  links: Links, stale: bool = False, stale_since: Optional[float] = None, history_length: Optional[int] = None) -> Dict[str, Any]:
    tracks_out = []
    specs = [TrackSpec(id=t["id"], name=t["name"], team=t.get("team", ""), depends_on=t.get("depends_on", [])) for t in state.get("tracks", [])]
    depths = depth_map(specs) if specs else {}
    for t in state.get("tracks", []):
        child = children.get(t["id"], {})
        steps = build_steps(t, child, live)
        running = next((s for s in steps if s["status"] in ("running", "blocked", "compensating")), None)
        live_info = (running or {}).get("live") or {}
        run_id_child = t.get("run_id") or child.get("run_id")
        tracks_out.append({
            **{k: v for k, v in t.items() if k not in ("steps", "step_status")},
            "depth": depths.get(t["id"], 0),
            "steps": steps,
            "run_id": run_id_child,
            "link": links.workflow(t.get("workflow_id"), run_id_child),
            "pending_link": links.workflow(t.get("workflow_id"), run_id_child, "pending-activities") if run_id_child else None,
            "current_step": (running or {}).get("name") or t.get("current_step"),
            "worker": live_info.get("worker_now") or live_info.get("last_worker") or t.get("worker"),
            "attempt": live_info.get("attempt"),
            "retrying": (live_info.get("attempt") or 1) > 1 and t.get("status") == "running",
            "queued": live_info.get("state") == "scheduled" and not live_info.get("next_attempt_at_future") and (live_info.get("waiting_s") or 0) >= 3,
            "backoff_until": live_info.get("next_attempt_at") if live_info.get("next_attempt_at_future") else None,
            "last_failure": live_info.get("last_failure"),
        })

    view = {
        "id": state.get("request_id"),
        "workflow_id": wf_id,
        "run_id": run_id,
        "temporal_status": temporal_status,
        "history_length": history_length,
        "links": {
            "workflow": links.workflow(wf_id, run_id),
            "pending": links.workflow(wf_id, run_id, "pending-activities") if run_id else None,
            "family": links.family(wf_id),
        },
        "title": state.get("title") or (state.get("request_text") or "")[:60],
        "requester": state.get("requester"),
        "request_text": state.get("request_text"),
        "submitted_at": state.get("submitted_at"),
        "closed_at": state.get("closed_at"),
        "status": state.get("status"),
        "status_reason": state.get("status_reason"),
        "stage": state.get("stage"),
        "stage_times": state.get("stage_times", {}),
        "spec": state.get("spec"),
        "intake": state.get("intake"),
        "ticket": state.get("ticket"),
        "risk": state.get("risk"),
        "approvals": state.get("approvals", []),
        "tracks": tracks_out,
        "skipped": state.get("skipped", []),
        "verification": state.get("verification"),
        "summary": state.get("summary"),
        "outputs": {k: v for k, v in (state.get("outputs") or {}).items() if not isinstance(v, (dict,)) or k == "tags"},
        "events": list(reversed(state.get("events", [])))[:80],
        "parent_pending": parent_pending,
        "demo": state.get("demo"),
        "stale": stale,
        "stale_since": stale_since,
    }
    view["reviews"] = [
        {"track_id": t["id"], "track": t["name"], "team": t["team"], "step_id": st["id"], "step": st["name"], "agent": st["agent"]}
        for t in tracks_out for st in t["steps"]
        if st.get("agent") and st["agent"].get("status") == "awaiting_review"
    ]
    view["headline"] = headline(view)
    return view


def headline(v: Dict[str, Any]) -> Dict[str, Any]:
    """Operator headline plus a `plain` version for the requester view (no jargon)."""
    h = _headline(v)
    h.setdefault("plain", h["text"])
    return h


def _headline(v: Dict[str, Any]) -> Dict[str, Any]:
    status, stage = v.get("status"), v.get("stage")
    if v.get("stale"):
        return {"text": "No workers are answering. Temporal is holding every step until a worker comes back.", "tone": "warn", "since": None,
                "plain": "Your request is safe and will pick up again in a moment", "plain_tone": "info"}
    if status == "completed":
        s = v.get("summary") or {}
        return {"text": s.get("headline") or "Delivered", "tone": "good", "since": v.get("closed_at")}
    if status == "rejected":
        return {"text": v.get("status_reason") or "Rejected", "tone": "bad", "since": v.get("closed_at")}
    if status in ("rolled_back", "cancelled"):
        return {"text": f"{'Rolled back' if status == 'rolled_back' else 'Cancelled'}. {v.get('status_reason') or ''}".strip(), "tone": "bad", "since": v.get("closed_at")}
    if status == "rolling_back":
        return {"text": f"Undoing completed work. {v.get('status_reason') or ''}".strip(), "tone": "warn", "since": None}

    blocked = [t for t in v["tracks"] if t.get("status") == "blocked"]
    if blocked:
        t = blocked[0]
        note = (t.get("diagnosis") or {}).get("requester_note") or f"{t['team']} hit a snag and is sorting it out"
        return {"text": f"{t['team']} is blocked on “{t.get('blocked_step') or t.get('current_step')}” and needs a decision", "tone": "bad", "since": None,
                "plain": note, "plain_tone": "warn"}

    if stage == "intake":
        pend = (v.get("parent_pending") or [{}])[0]
        what = {"ai_parse_request": "Reading your request", "plan_request": "Planning the work", "itsm_create_change": "Opening the change request"}.get(pend.get("activity_type"), "Reading your request")
        if pend.get("attempt", 1) > 1:
            what += f" (retry {pend['attempt']})"
        return {"text": what, "tone": "info", "since": v.get("submitted_at")}

    pending_appr = [a for a in v.get("approvals", []) if a.get("status") == "pending"]
    if stage == "approvals":
        if pending_appr:
            a = pending_appr[0]
            extra = f" (+{len(pending_appr) - 1} more)" if len(pending_appr) > 1 else ""
            esc = f", escalated to {a['escalation']}" if a.get("escalated") else ""
            return {"text": f"Waiting on {a['name']} approval from {a['approver']}{extra}{esc}", "tone": "wait", "since": a.get("requested_at")}
        return {"text": "Briefing the approvers", "tone": "info", "since": v.get("stage_times", {}).get("approvals")}

    if stage == "provisioning" and v.get("reviews"):
        r = v["reviews"][0]
        n = len(((r.get("agent") or {}).get("draft") or {}).get("rules") or [])
        others = sum(1 for t in v["tracks"] if t.get("status") in ACTIVE_TRACK)
        tail = f"; {others} other team{'s' if others != 1 else ''} still working" if others else ""
        return {"text": f"Waiting on {r['team']} to review the {n} firewall rule{'s' if n != 1 else ''} Claude drafted{tail}", "tone": "wait", "since": None,
                "plain": f"{r['team']} is reviewing the firewall rules for your account"}

    if stage == "provisioning":
        gated = [t for t in v["tracks"] if t.get("status") == "awaiting_approval"]
        running = [t for t in v["tracks"] if t.get("status") in ACTIVE_TRACK]
        retrying = [t for t in running if t.get("retrying")]
        queued = [t for t in running if t.get("queued")]
        done = sum(1 for t in v["tracks"] if t.get("status") in ("completed", "skipped"))
        working = f"{len(running)} team{'s' if len(running) != 1 else ''} working on your request ({done} of {len(v['tracks'])} done)"
        if queued and len(queued) == len(running):
            return {"text": f"{len(queued)} step{'s are' if len(queued) > 1 else ' is'} queued: no worker has picked {'them' if len(queued) > 1 else 'it'} up yet", "tone": "warn", "since": None,
                    "plain": "Your request is in line and will pick up again in a moment", "plain_tone": "info"}
        if retrying:
            t = retrying[0]
            return {"text": f"{t['team']}: retrying “{t.get('current_step')}” (attempt {t.get('attempt')})", "tone": "warn", "since": None,
                    "plain": working, "plain_tone": "info"}
        if running:
            names = ", ".join(t["team"] for t in running[:3]) + (f" +{len(running) - 3}" if len(running) > 3 else "")
            tail = f"; firewall rules wait on {gated[0]['waiting_on'][0]}" if gated else ""
            return {"text": f"{len(running)} teams working now ({done}/{len(v['tracks'])} done): {names}{tail}", "tone": "info", "since": v.get("stage_times", {}).get("provisioning"),
                    "plain": working}
        if gated:
            return {"text": f"Waiting on {gated[0]['waiting_on'][0]}", "tone": "wait", "since": None}
        return {"text": "Provisioning", "tone": "info", "since": v.get("stage_times", {}).get("provisioning")}

    if stage == "verification":
        return {"text": "Running final checks on the new account", "tone": "info", "since": v.get("stage_times", {}).get("verification")}
    if stage == "delivered":
        return {"text": "Handing off to the requester", "tone": "info", "since": None}
    return {"text": "Working on it", "tone": "info", "since": None}


def summary_row(wf_id: str, state: Optional[Dict[str, Any]], temporal_status: str, started: Optional[str], memo: Dict[str, Any]) -> Dict[str, Any]:
    st = state or {}
    tracks = st.get("tracks") or []
    status = st.get("status") or ("in_progress" if temporal_status == "running" else temporal_status)
    if status == "in_progress" and any(t.get("status") == "blocked" for t in tracks):
        status = "blocked"
    return {
        "id": st.get("request_id") or memo.get("request_id") or wf_id.removeprefix(settings.WORKFLOW_ID_PREFIX).upper(),
        "workflow_id": wf_id,
        "title": st.get("title") or memo.get("title") or (memo.get("text") or "")[:60],
        "requester": st.get("requester") or memo.get("requester"),
        "status": status,
        "stage": st.get("stage"),
        "submitted_at": st.get("submitted_at") or started,
        "temporal_status": temporal_status,
        "tracks_done": sum(1 for t in tracks if t.get("status") in ("completed", "skipped")),
        "tracks_total": len(tracks),
    }


# --- workers as Temporal sees them ---------------------------------------------------

ALIVE = ("running", "starting", "unresponsive", "stopping")
QUIET_AFTER_S = 75  # an idle live worker re-polls at least once a minute
LISTED_FOR_S = 300  # Temporal drops a worker from the list 5 minutes after its last poll


def classify_pollers(pollers: List[Dict[str, Any]], workers: List[Dict[str, Any]],
                     now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """What Temporal's poller list means, worker by worker.

    Temporal doesn't track processes. DescribeTaskQueue (and the Temporal UI's
    Workers tab) lists every worker that polled in the last 5 minutes, with the
    time of its last poll. A killed worker never says goodbye, so it stays on
    the list until that window passes. For the demo's own workers we know the
    process state; for anything else, a last poll over a minute old means it has
    stopped (or is too busy to poll).

    Also annotates each worker dict with last_poll_s and listed_for_s.
    """
    now = now or datetime.now(timezone.utc)
    by_id = {w["id"]: w for w in workers}
    out = []
    for p in pollers:
        age = None
        try:
            if p.get("last_access"):
                age = max(0.0, (now - datetime.fromisoformat(p["last_access"])).total_seconds())
        except ValueError:
            age = None
        w = by_id.get(p.get("identity"))
        if w:
            state = "alive" if w.get("status") in ALIVE else "dead"
            w["last_poll_s"] = age
            w["listed_for_s"] = None if age is None else max(0.0, LISTED_FOR_S - age)
        else:
            state = "recent" if age is not None and age <= QUIET_AFTER_S else "quiet"
        out.append({**p, "age_s": age, "worker": bool(w), "state": state})
    return out
