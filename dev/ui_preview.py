"""UI preview harness: the real UI on canned data, no Temporal or workers needed.

    python dev/ui_preview.py            -> http://127.0.0.1:8801

Builds simulated workflow state for five requests (waiting on approvals,
provisioning with a retry, blocked with a diagnosis, delivered, workers down)
and runs it through the same view code the real server uses, so the snapshot
shape is identical. Handy for UI work and screenshots. Buttons answer with a
toast but don't change anything.
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from demo import settings  # noqa: E402
from demo.ai.tasks import heuristic_parse, heuristic_risk, normalize_spec  # noqa: E402
from demo.api import view  # noqa: E402
from demo.catalog import build_plan  # noqa: E402
from demo.connectors.base import StepContext  # noqa: E402
from demo.connectors.mock import MockConnector, _script  # noqa: E402
from demo.agents import firewall as fw  # noqa: E402
from demo.api import code as code_view  # noqa: E402
from demo.api import lab as lab_view  # noqa: E402
from demo import frameworks  # noqa: E402

STATIC = ROOT / "demo" / "static"
PREVIEW_STORE = {"bugs": {"vpc_subnets": True}}
FRAMEWORKS_INSTALLED = "--no-frameworks" not in sys.argv
T0 = time.time()
LINKS = view.Links("http://localhost:8233", "default")


def iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def mock_out(rid, spec, step, ctx):
    c = StepContext(request_id=rid, track_id="", step_id=step.id, step_name=step.name, system=step.system,
                    system_label=step.system_label, action=step.action, params=step.params, spec=spec, context=ctx)
    m = MockConnector(step.system, {})
    out = m.build_output(c)
    return out, _script(c, out)


def simulate_agent(rid, spec, outputs, wf, feedback=None):
    """Run the offline agent brain + tools to produce a realistic transcript."""
    ctx = fw.agent_context(spec, outputs)
    msgs = [{"role": "user", "content": fw.task_prompt(ctx)}]
    transcript, stats, rnd, draft, policy = [], {"llm_calls": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0}, 1, None, None
    workers = ["core-1", "core-2"]
    for _ in range(12):
        content = fw.heuristic_turn(msgs, ctx)
        stats["llm_calls"] += 1
        stats["input_tokens"] += 1800 + 400 * stats["llm_calls"]
        stats["output_tokens"] += 220
        transcript.append({"kind": "turn", "n": stats["llm_calls"], "round": rnd, "text": " ".join(b["text"] for b in content if b["type"] == "text"),
                           "tools": [b["name"] for b in content if b["type"] == "tool_use"], "source": "claude", "model": settings.ANTHROPIC_MODEL,
                           "latency_ms": 1400 + 300 * stats["llm_calls"], "attempt": 2 if stats["llm_calls"] == 3 else 1, "worker": workers[stats["llm_calls"] % 2]})
        msgs.append({"role": "assistant", "content": content})
        uses = [b for b in content if b["type"] == "tool_use"]
        sub = next((u for u in uses if u["name"] == "submit_rules"), None)
        if sub:
            draft = {"tool_use_id": sub["id"], **sub["input"]}
            if feedback and rnd == 1:
                transcript.append({"kind": "review", "decision": "changes", "reviewer": "Network Security on-call", "comment": feedback, "round": rnd})
                msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": sub["id"], "content": "changes"}, {"type": "text", "text": fw.feedback_message("Network Security on-call", feedback)}]})
                rnd += 1
                continue
            break
        res = []
        for u in uses:
            out = fw.run_tool(u["name"], u["input"], ctx)
            if u["name"] == "check_policy":
                policy = out
            stats["tool_calls"] += 1
            transcript.append({"kind": "tool", "round": rnd, "name": u["name"], "input": u["input"], "summary": fw.describe_call(u["name"], u["input"], out), "attempt": 1, "worker": "core-1"})
            res.append({"type": "tool_result", "tool_use_id": u["id"], "content": json.dumps(out)})
        msgs.append({"role": "user", "content": res})
    aid = f"{wf}.firewall.draft_rules-1"
    return {"status": "awaiting_review", "round": rnd, "mode": "plain", "reviewer": "Network Security on-call", "context": {k: ctx[k] for k in ("account_name", "vpc_cidr", "targets")},
            "draft": {**draft, "policy": policy}, "transcript": transcript, "stats": stats, "workflow_id": aid, "run_id": "a9e17-run",
            "link": LINKS.workflow(aid, "a9e17-run"), "pending": []}


def make(rid, preset, stage, statuses, approvals, *, age=300, blocked=None, retry=None, failure=None, queued=False, summary=False, status="in_progress", feedback=None):
    spec = normalize_spec(heuristic_parse(preset["text"], preset["requester"]), preset["text"], preset["requester"])
    spec["assumptions"] = ["Region defaulted to us-east-1", "CIDR sized /22 for production"] if spec["environment"] == "prod" else ["Region defaulted to us-east-1"]
    spec["missing_info"] = []
    plan = build_plan(spec)
    outputs = {"change_number": "CHG0031" + rid[-3:], "change_url": "#"}
    tracks, children, events = [], {}, []
    wf = f"vend-{rid.lower()}"
    events.append({"t": iso(age), "kind": "intake", "message": f"Request received from {preset['requester']}"})
    events.append({"t": iso(age - 3), "kind": "intake", "message": f"Claude read the request: {spec['environment']} account, {spec['data_classification']} data"})
    events.append({"t": iso(age - 5), "kind": "plan", "message": f"Plan: {len(plan.tracks)} teams, {sum(len(t.steps) for t in plan.tracks)} steps, {len(plan.approvals)} approvals"})
    events.append({"t": iso(age - 7), "kind": "ticket", "message": f"Opened {outputs['change_number']} (one ticket instead of one per team)"})
    downstream = {t.id: [] for t in plan.tracks}
    for t in plan.tracks:
        for d in t.depends_on:
            downstream[d].append(t.name)
    offset = age - 40
    for t in plan.tracks:
        st = statuses.get(t.id, "pending")
        steps_child, ctx = [], dict(outputs)
        done_n = {"completed": len(t.steps)}.get(st, 0)
        if st == "running":
            done_n = 1 if len(t.steps) > 1 else 0
        pend, agents = {}, {}
        if st == "awaiting_review":
            st_steps = "running"
        else:
            st_steps = st
        for i, s in enumerate(t.steps):
            out, script = mock_out(rid, spec, s, ctx)
            sst = "completed" if i < done_n else ("pending" if st_steps not in ("running", "blocked") or i > done_n else ("blocked" if st == "blocked" else "running"))
            if st == "cancelled":
                sst = "cancelled"
            row = {"id": s.id, "name": s.name, "system": s.system, "system_label": s.system_label, "location": s.location,
                   "queue": s.task_queue, "status": sst, "attempt": 1 if sst == "completed" else None,
                   "worker": ("onprem-1" if s.location == "onprem" else "core-2") if sst in ("completed", "running", "blocked") else None,
                   "started_at": iso(offset - i * 6) if sst != "pending" else None,
                   "completed_at": iso(offset - i * 6 - 5) if sst == "completed" else None,
                   "duration_ms": int(s.seconds * 1000 + 230) if sst == "completed" else None,
                   "error": None, "diagnosis": None, "log": script if sst == "completed" else [], "output": out if sst == "completed" else {}, "history": []}
            if sst == "completed":
                ctx.update(out)
                outputs.update(out)
            if sst == "running" and s.kind == "agent":
                row.update(kind="agent", agent_workflow_id=f"{wf}.firewall.draft_rules-1", agent_status="awaiting_review", worker="agent")
                agents[s.id] = simulate_agent(rid, spec, outputs, wf, feedback)
            elif sst == "running":
                frac = ((time.time() - T0) % 12) / 12
                f = (failure or {}).get(t.id)
                pend[s.id] = {"activity_type": "run_step", "state": "scheduled" if f else "started", "attempt": f["attempt"] if f else (retry or {}).get(t.id, 1), "max_attempts": 0 if f else 6,
                              "last_failure": {"type": f["type"], "message": f["message"]} if f else ({"type": "RateLimited", "message": "HTTP 429 Too Many Requests from /api/v1/groups"} if (retry or {}).get(t.id) else None),
                              "last_worker": row["worker"], "last_started_at": iso(4), "last_heartbeat_at": iso(1), "next_attempt_at": iso(-4) if f else None,
                              "next_attempt_at_future": bool(f), "heartbeat": {"log": script[: max(1, int(frac * len(script)) + 1)]}, "step": s.id}
                if queued:
                    pend[s.id].update(state="scheduled", last_heartbeat_at=None, heartbeat={})
                    row["worker"] = None
            if sst == "blocked" and blocked:
                row["error"] = blocked["error"]
                row["diagnosis"] = blocked["diagnosis"]
                row["history"] = [{"at": iso(30), "event": "failed", "error": blocked["error"]["message"]}]
            steps_child.append(row)
        children[t.id] = {"steps": steps_child, "pending": pend, "agents": agents, "run_id": "c0ffee00-" + t.id}
        cur = next((x["name"] for x in steps_child if x["status"] in ("running", "blocked")), None)
        waiting = [x.name for x in plan.tracks if x.id in t.depends_on and statuses.get(x.id) != "completed"]
        appr_wait = []
        tracks.append({
            "id": t.id, "name": t.name, "team": t.team, "depends_on": t.depends_on, "approval": t.approval, "reason": t.reason,
            "downstream": downstream[t.id], "status": st, "waiting_on": appr_wait or waiting,
            "steps_total": len(t.steps), "steps_done": done_n, "current_step": cur, "worker": None,
            "error": blocked["error"] if st == "blocked" and blocked else None,
            "diagnosis": blocked["diagnosis"] if st == "blocked" and blocked else None,
            "blocked_step": cur if st == "blocked" else None,
            "workflow_id": f"{wf}.{t.id}" if st not in ("pending", "waiting", "awaiting_approval") else None,
            "run_id": "c0ffee00-" + t.id if st not in ("pending", "waiting", "awaiting_approval") else None,
            "started_at": iso(offset) if st not in ("pending", "waiting", "awaiting_approval") else None,
            "completed_at": iso(offset - 12) if st == "completed" else None,
            "step_status": {x["id"]: x["status"] for x in steps_child},
            "steps": [{"id": s.id, "name": s.name, "system": s.system, "system_label": s.system_label, "location": s.location, "queue": s.task_queue, "undo": s.undo} for s in t.steps],
        })
        if st == "completed":
            events.append({"t": iso(offset - 12), "kind": "track_completed", "message": f"{t.team} finished {t.name}", "tone": "good"})
        elif st in ("running", "blocked"):
            events.append({"t": iso(offset), "kind": "track_started", "message": f"{t.team} started {t.name}"})
    appr = []
    for a in plan.approvals:
        s = approvals.get(a.id, "approved")
        appr.append({**a.__dict__, "status": s, "requested_at": iso(age - 20), "decided_at": iso(age - 50) if s != "pending" else None,
                     "decided_by": ("Policy" if a.auto else a.approver) if s != "pending" else None, "comment": None,
                     "escalated": a.id == "finance" and s == "pending", "escalated_at": None})
        if s == "pending":
            events.append({"t": iso(age - 20), "kind": "approval_requested", "message": f"Waiting on {a.name} ({a.approver})", "tone": "wait"})
        else:
            events.append({"t": iso(age - 50), "kind": "approval_approved", "message": f"{a.name} approved by {a.approver}", "tone": "good"})
    if blocked:
        events.append({"t": iso(30), "kind": "track_blocked", "message": f"Network & IP space is blocked on 'Reserve a CIDR block in IPAM': {blocked['error']['message'][:80]}", "tone": "bad"})
    events.sort(key=lambda e: e["t"])
    stage_order = ["intake", "approvals", "provisioning", "verification", "delivered"]
    stage_times = {s: iso(age - 10 * i) for i, s in enumerate(stage_order[: stage_order.index(stage) + 1])}
    state = {
        "request_id": rid, "requester": preset["requester"], "request_text": preset["text"], "title": spec["title"],
        "submitted_at": iso(age), "status": status, "status_reason": None, "stage": stage, "stage_times": stage_times,
        "closed_at": iso(5) if status == "completed" else None, "spec": spec,
        "intake": {"source": "claude", "model": settings.ANTHROPIC_MODEL, "latency_ms": 2140, "assumptions": spec["assumptions"], "missing_info": []},
        "ticket": {"change_number": outputs["change_number"], "change_state": "Implement" if stage == "provisioning" else ("Closed" if status == "completed" else "Assess")},
        "risk": {**heuristic_risk(spec), "meta": {"source": "claude", "model": settings.ANTHROPIC_MODEL, "latency_ms": 1810}},
        "approvals": appr, "tracks": tracks, "skipped": [s.__dict__ for s in plan.skipped],
        "verification": None, "outputs": outputs, "events": events, "demo": {},
        "summary": {"headline": f"{spec['title']} is ready", "message": f"Account {outputs.get('account_id')} is live in us-east-1. "
                    f"{len({t['team'] for t in tracks})} teams finished their work from one request, and the change ticket is closed.",
                    "next_steps": ["Sign in through Okta and pick the account in the AWS access portal", "Budget alerts go to the owners at 80% and 100%", "Ask for more roles through the same form"],
                    "meta": {"source": "claude", "model": settings.ANTHROPIC_MODEL, "latency_ms": 1650}} if summary else None,
    }
    return wf, state, children


BLOCK = {
    "error": {"type": "IpamPoolExhausted", "message": "No free /22 available in network container 10.48.0.0/14 (utilization 100%, 0 of 256 blocks free)", "retryable": False},
    "diagnosis": {
        "root_cause": "The 10.48.0.0/14 workload container in Infoblox is fully allocated, so the playbook could not reserve a /22 for this VPC.",
        "impact": "Blocks Firewall rules, DNS and CMDB registration; the other five teams are unaffected.",
        "recommended_action": "retry", "confidence": "high",
        "operator_steps": ["Reclaim the two /22s still reserved for decommissioned sandbox accounts", "Or extend the container with 10.52.0.0/16 as an overflow pool", "Retry the step; the playbook takes the next free block"],
        "requester_note": "Network Engineering is freeing up IP space for your account; nothing is needed from you.",
        "meta": {"source": "claude", "model": settings.ANTHROPIC_MODEL, "latency_ms": 2380},
    },
}


def scenarios():
    p = view.presets()
    done = {k: "completed" for k in ("account", "identity", "access", "network", "firewall", "dns", "security", "logging", "finops", "cmdb")}
    return [
        make("REQ-4821", p[0], "provisioning", {"account": "completed", "identity": "completed", "network": "blocked", "security": "completed",
                                                  "finops": "completed", "access": "running", "logging": "running", "firewall": "waiting", "dns": "waiting", "cmdb": "waiting"},
             {"network_security": "approved"}, age=420, blocked=BLOCK),
        make("REQ-4817", p[3], "provisioning", {"account": "completed", "identity": "running", "network": "completed", "security": "running", "finops": "completed",
                                                  "firewall": "awaiting_review", "dns": "running", "cmdb": "running", "access": "waiting"},
             {}, age=260, retry={"identity": 3}),
        make("REQ-4833", p[0], "provisioning", {"account": "completed", "identity": "completed", "network": "running", "security": "completed", "finops": "completed",
                                                  "access": "running", "logging": "completed"},
             {}, age=180, failure={"network": {"type": "IndexError", "attempt": 7,
                                              "message": "list index out of range"}}),
        make("REQ-4809", p[1], "approvals", {}, {}, age=95),
        make("REQ-4796", p[2], "delivered", done, {}, age=1500, summary=True, status="completed"),
        make("REQ-4790", p[0], "approvals", {}, {"cloud_coe": "approved", "infosec": "pending", "finance": "pending"}, age=140),
    ]


# --- Agent lab ---------------------------------------------------------------------

def _lab_steps():
    """Drive the offline brain on the lab input: [(turn_content, [(tool_use, output)])...] until submit."""
    spec, ctx_in = dict(lab_view.LAB_SPEC), dict(lab_view.LAB_CONTEXT)
    ctx = fw.agent_context(spec, ctx_in)
    msgs = [{"role": "user", "content": fw.task_prompt(ctx)}]
    steps = []
    for _ in range(12):
        content = fw.heuristic_turn(msgs, ctx)
        msgs.append({"role": "assistant", "content": content})
        uses = [b for b in content if b["type"] == "tool_use"]
        sub = next((u for u in uses if u["name"] == "submit_rules"), None)
        calls = [(u, fw.run_tool(u["name"], u["input"], ctx)) for u in uses if u is not sub]
        steps.append((content, calls, sub))
        if sub:
            return steps, ctx
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": u["id"], "content": json.dumps(o)} for u, o in calls]})
    return steps, ctx


def simulate_lab_run(mode, start_ago, crash_ago, *, tools_retry=None, stop_after=None, pending_now=None):
    steps, ctx = _lab_steps()
    t = start_ago
    transcript, units, policy, draft = [], [], None, None
    stats = {"llm_calls": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0}
    pending = []
    for i, (content, calls, sub) in enumerate(steps):
        if stop_after is not None and i >= stop_after:
            pending.append(pending_now or {"activity_type": "langgraph_node", "attempt": 1, "last_worker": "core-4", "next_attempt_at_future": False})
            break
        t -= 1.9
        crashed = crash_ago is not None and t < crash_ago < t + 1.9 and mode != "langgraph"
        stats["llm_calls"] += 1
        stats["input_tokens"] += 1700 + 380 * i
        stats["output_tokens"] += 210
        turn = {"kind": "turn", "at": iso(t), "n": i + 1, "round": 1, "text": " ".join(b["text"] for b in content if b["type"] == "text"),
                "tools": [b["name"] for b in content if b["type"] == "tool_use"], "source": "claude", "model": settings.ANTHROPIC_MODEL,
                "latency_ms": 1500 + 120 * i, "attempt": 2 if crashed else 1, "worker": "core-4" if crashed else ("core-1" if i % 2 else "core-2")}
        transcript.append(turn)
        if mode == "langgraph":
            units.append({"kind": "node", "node": "agent", "label": "agent", "attempt": 1, "worker": turn["worker"], "at": turn["at"]})
        node_attempt = 1
        if stop_after is not None and i == stop_after - 1 and ((pending_now or {}).get("heartbeat") or {}).get("node") == "tools":
            pending.append(pending_now)  # this turn's tools node is still running
            break
        if calls:
            t -= 0.7
            if mode == "langgraph":
                node_attempt = (tools_retry or {}).get(i, 1)
                if crash_ago is not None and t < crash_ago < t + 2.6:
                    node_attempt = max(node_attempt, 2)
            for j, (u, out) in enumerate(calls):
                stats["tool_calls"] += 1
                if u["name"] == "check_policy":
                    policy = out
                a = node_attempt if mode == "langgraph" else (2 if (tools_retry or {}).get(i) and j == 1 else 1)
                transcript.append({"kind": "tool", "at": iso(t), "round": 1, "name": u["name"], "input": u["input"],
                                   "summary": fw.describe_call(u["name"], u["input"], out), "attempt": a, "worker": "core-1"})
            if mode == "langgraph":
                units.append({"kind": "node", "node": "tools", "label": f"tools ({len(calls)} calls)", "attempt": node_attempt, "worker": "core-2", "at": iso(t), "calls": len(calls)})
        if sub:
            draft = {"tool_use_id": sub["id"], **sub["input"]}
    status = "awaiting_review" if draft and stop_after is None else "drafting"
    wf_id = f"lab-4821-{mode}"
    return {"status": status, "round": 1, "mode": mode, "reviewer": lab_view.LAB_REVIEWER,
            "context": {k: ctx[k] for k in ("account_name", "vpc_cidr", "targets")},
            "draft": {**draft, "policy": policy} if draft and status == "awaiting_review" else None,
            "transcript": transcript, "units": units, "stats": stats, "workflow_id": wf_id, "run_id": f"{mode}-run",
            "link": LINKS.workflow(wf_id, f"{mode}-run"), "pending": pending, "temporal_status": "running"}


def lab_snapshot(fw_info):
    """Three runs in different states: plain crashed and waiting for review, LangChain
    calling Claude, LangGraph's tools node retrying against a flaky CMDB."""
    crash_ago = 13.0
    crashes = [{"at": time.time() - crash_ago, "killed": ["core-1", "core-2"]}]
    plans = {
        "plain": ("crash", 24.0, crash_ago, {"tools_retry": {0: 1}}),
        "langchain": ("happy", 9.0, None, {"stop_after": 2, "pending_now": {"activity_type": "lc_llm_turn", "attempt": 1, "last_worker": "core-4", "heartbeat": {"turn": 3}}}),
        "langgraph": ("flaky_cmdb", 8.0, None, {"stop_after": 1, "pending_now": {
            "activity_type": "langgraph_node", "attempt": 3, "last_worker": "core-2", "heartbeat": {"node": "tools"},
            "last_failure": {"type": "TransientUpstreamError", "message": "Transient error calling servicenow"}, "next_attempt_at_future": False}}),
    }
    runs = {}
    for mode, (scen, ago, crash, kwargs) in plans.items():
        if not fw_info[mode]["available"]:
            continue
        agent = simulate_lab_run(mode, ago, crash, **kwargs)
        if mode == "langgraph":
            agent["node"] = "agent"
        run = {"workflow_id": agent["workflow_id"], "scenario": scen, "started_at": time.time() - ago - 1}
        runs[mode] = lab_view.run_view(mode, run, agent, crashes if crash else [])
    return {"runs": runs, "links": {"runs": LINKS.family("lab-"), "queue": LINKS.task_queue(settings.CORE_QUEUE)},
            "change": lab_view.LAB_CHANGE, "cmdb_error_rate": 0.3,
            "input": {"account": lab_view.LAB_SPEC["account_name"], "vpc_cidr": lab_view.LAB_CONTEXT["vpc_cidr"], "targets": lab_view.LAB_SPEC["onprem_targets"]}}


def frameworks_info():
    out = {}
    for m, meta in frameworks.MODES.items():
        ok = m == "plain" or FRAMEWORKS_INSTALLED
        out[m] = {**{k: v for k, v in meta.items() if k != "requires"}, "id": m, "available": ok,
                  "reason": None if ok else f"{meta['requires'][-1]} is not installed (ModuleNotFoundError). {meta['install']}"}
    return out


def pools():
    """Pools as the API returns them: Temporal's poller list, classified against the demo's own workers."""
    now = datetime.now(timezone.utc)
    ago = lambda sec: (now - timedelta(seconds=sec)).isoformat()  # noqa: E731
    raw = {"core": [{"identity": "core-1", "last_access": ago(3)}, {"identity": "core-2", "last_access": ago(8)},
                    {"identity": "core-3", "last_access": ago(42)}, {"identity": "temporal-system", "last_access": ago(4)}],
           "onprem": [{"identity": "onprem-1", "last_access": ago(5)}]}
    out = []
    for p in _pools_raw():
        p["pollers"] = view.classify_pollers(raw[p["id"]], p["workers"], now)
        p["polling"] = sum(1 for x in p["pollers"] if x["state"] == "alive")
        p["listed_gone"] = [x for x in p["pollers"] if x["state"] in ("dead", "quiet")]
        p["listed_other"] = [x["identity"] for x in p["pollers"] if not x["worker"]]
        out.append(p)
    return out


def _pools_raw():
    return [
        {"id": "core", "label": "Core workers", "description": "Workflows, Claude, AWS, Okta, ServiceNow", "where": "Cloud or Kubernetes",
         "queue": "vending-core", "queue_url": LINKS.task_queue("vending-core"), "managed": True, "backlog": 0,
         "pollers": [{"identity": "core-1"}, {"identity": "core-2"}],
         "workers": [
             {"id": "core-1", "pool": "core", "pid": 41211, "status": "running", "exit_code": None, "version": "8f85350", "completed": 38, "failed": 1, "current": [{"label": "Provision permission sets"}]},
             {"id": "core-2", "pool": "core", "pid": 41212, "status": "running", "exit_code": None, "version": "3c1d0aa", "replacing": "core-3", "completed": 41, "failed": 0, "current": [{"label": "Route CloudTrail to the log archive"}, {"label": "Claude: diagnosing a failure"}]},
             {"id": "core-3", "pool": "core", "pid": 41390, "status": "killed", "exit_code": -9, "version": "8f85350", "completed": 0, "failed": 0, "current": []},
         ]},
        {"id": "onprem", "label": "On-prem workers", "description": "Ansible: AD, Infoblox, Panorama, DNS; Splunk", "where": "Inside the data center, outbound only",
         "queue": "vending-onprem", "queue_url": LINKS.task_queue("vending-onprem"), "managed": True, "backlog": 2,
         "pollers": [{"identity": "onprem-1"}],
         "workers": [{"id": "onprem-1", "pool": "onprem", "pid": 41213, "status": "running", "exit_code": None, "version": "8f85350", "completed": 12, "failed": 1, "current": [{"label": "Create AD security groups"}]}]},
    ]


def snapshot(selected, lab=False):
    sc = scenarios()
    fw_info = frameworks_info()
    rows = [view.summary_row(wf, st, "completed" if st["status"] == "completed" else "running", st["submitted_at"], {}) for wf, st, _ in sc]
    sel = None
    for wf, st, ch in sc:
        if wf == selected:
            sel = view.build_request(st, wf_id=wf, run_id="0a1b2c3d-run", temporal_status="running" if st["status"] != "completed" else "completed",
                                     children=ch, live={}, parent_pending=[] if st["stage"] != "approvals" else [],
                                     links=LINKS, history_length=180)
    return {
        "ts": datetime.now(timezone.utc).isoformat(), **view.static_payload(),
        "temporal": {"connected": True, "address": "localhost:7233", "namespace": "default", "cloud": False, "ui": LINKS.ui, "namespace_url": LINKS.namespace()},
        "ai": {"enabled": True, "model": settings.ANTHROPIC_MODEL},
        "settings": {"speed": 1.0, "approvals": {"auto_approve_seconds": 0, "sla_seconds": 60}, "retries": {"max_attempts": 6, "heartbeat_seconds": 8}},
        "armed": {"okta_rate_limit": 2}, "system_chaos": {"servicenow": {"error_rate": 0.2}},
        "pools": pools(), "requests": rows, "selected": sel,
        "code": {"disk_version": "3c1d0aa", "deployed_version": "3c1d0aa", "auto_deploy": True, "rollouts": [],
                 "last_deploy": {"at": time.time() - 20, "version": "3c1d0aa", "state": "rolling", "replaced": 2, "error": None},
                 "bugs": code_view.bugs_status(PREVIEW_STORE)},
        "frameworks": fw_info,
        "agent_mode": "plain",
        **({"lab": lab_snapshot(fw_info)} if lab else {}),
        "lab_links": {m: LINKS.workflow(f"lab-4821-{m}") for m in ("plain", "langchain", "langgraph")},
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/":
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path.startswith("/static/"):
            f = STATIC / path.removeprefix("/static/")
            ctype = {"js": "text/javascript", "css": "text/css", "html": "text/html"}.get(f.suffix.lstrip("."), "application/octet-stream")
            return self._send(200, f.read_bytes(), ctype) if f.exists() else self._send(404, {"detail": "not found"})
        if path == "/api/code":
            return self._send(200, {"snippets": code_view.snippets(), "bugs": code_view.bugs_status(PREVIEW_STORE),
                                    "disk_version": "3c1d0aa", "deployed_version": "3c1d0aa", "last_deploy": None, "rollouts": []})
        if path == "/api/lab/content":
            return self._send(200, lab_view.content())
        if path == "/api/presets":
            return self._send(200, view.presets())
        if path == "/api/snapshot":
            params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            return self._send(200, snapshot(params.get("request"), params.get("lab") == "1"))
        self._send(404, {"detail": "not found"})

    def do_POST(self):
        self._send(200, {"ok": True, "id": "REQ-PREVIEW", "workflow_id": "vend-req-4821"})

    do_PUT = do_POST
    do_DELETE = do_POST


if __name__ == "__main__":
    port = int(next((a for a in sys.argv[1:] if a.isdigit()), 8801))
    print(f"UI preview on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
