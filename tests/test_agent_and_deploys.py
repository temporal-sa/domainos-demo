"""Tests for the bug/deploy demo, the firewall agent and the Under the hood tab."""

import json
import signal

import pytest

from demo import bugs, chaos, settings
from demo.agents import firewall as fw
from demo.api import code, supervisor

CTX = {"account_name": "mortgage-analytics-prod", "vpc_cidr": "10.48.12.0/22", "targets": ["Oracle database", "Data center services"],
       "environment": "prod", "data_classification": "Confidential", "request_text": "x"}


def run_offline_agent(ctx, feedback=()):
    """Drive the offline brain the way FirewallRuleAgentWorkflow does."""
    msgs = [{"role": "user", "content": fw.task_prompt(ctx)}]
    queue, drafts = list(feedback), []
    for _ in range(20):
        content = fw.heuristic_turn(msgs, ctx)
        msgs.append({"role": "assistant", "content": content})
        uses = [b for b in content if b["type"] == "tool_use"]
        sub = next((u for u in uses if u["name"] == "submit_rules"), None)
        if sub:
            drafts.append(sub["input"])
            if not queue:
                return drafts, msgs
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": sub["id"], "content": "changes requested"},
                {"type": "text", "text": fw.feedback_message("Network Security on-call", queue.pop(0))}]})
            continue
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": u["id"], "content": json.dumps(fw.run_tool(u["name"], u["input"], ctx))} for u in uses]})
    raise AssertionError("agent did not submit")


# --- agent -----------------------------------------------------------------------

def test_agent_drafts_least_privilege_rules_and_skips_existing():
    drafts, _ = run_offline_agent(CTX)
    names = [r["name"] for r in drafts[-1]["rules"]]
    assert names == ["aws-mortgage-analytics-prod-to-oracle-loans", "aws-mortgage-analytics-prod-to-ntp"]
    assert "aws-shared-to-dns" in drafts[-1]["rationale"]
    assert fw.check_policy(drafts[-1]["rules"], CTX["vpc_cidr"])["compliant"]


def test_agent_drops_what_policy_forbids():
    drafts, msgs = run_offline_agent({**CTX, "account_name": "card-services-prod", "targets": ["Mainframe"]})
    assert [r["port"] for r in drafts[-1]["rules"]] == [1414]
    assert "CISO exception" in drafts[-1]["rationale"]
    assert any(c["name"] == "check_policy" and not c["output"]["compliant"] for c in fw.tool_calls(msgs))


def test_agent_revises_after_reviewer_feedback():
    drafts, _ = run_offline_agent(CTX, feedback=["Drop the NTP rule; we use Amazon Time Sync"])
    assert len(drafts) == 2
    assert [r["port"] for r in drafts[0]["rules"]] == [1521, 123]
    assert [r["port"] for r in drafts[1]["rules"]] == [1521]
    assert "Removed per reviewer: NTP" in drafts[1]["rationale"]


@pytest.mark.parametrize("rule,problem", [
    ({"destination": "any"}, "any"),
    ({"destination": "10.20.0.0/24"}, "single host"),
    ({"destination": "192.168.1.5"}, "outside the data center"),
    ({"port": 23}, "Telnet"),
    ({"port": 8080}, "approved service"),
    ({"source": "0.0.0.0/0"}, "VPC CIDR"),
    ({"name": "allow-everything"}, "aws-<account>-to-<service>"),
])
def test_policy_catches_bad_rules(rule, problem):
    base = {"name": "aws-x-to-oracle", "source": CTX["vpc_cidr"], "destination": "10.20.14.25", "port": 1521, "protocol": "tcp"}
    out = fw.check_policy([{**base, **rule}], CTX["vpc_cidr"])
    assert not out["compliant"]
    assert any(problem in v["problem"] for v in out["violations"])


def test_tool_schemas_cover_every_tool():
    names = {t["name"] for t in fw.TOOLS}
    assert names == {"lookup_cmdb", "check_existing_rules", "check_policy", "submit_rules"}
    assert all(t["input_schema"]["type"] == "object" for t in fw.TOOLS)


# --- bug mode ------------------------------------------------------------------------

def test_bug_line_found_and_live():
    st = bugs.line_status("vpc_subnets")
    assert st["active"] and st["line"] and "raise" in st["text"]


def test_bug_only_fires_in_bug_mode(tmp_path, monkeypatch):
    from demo import subnets

    store = chaos.RuntimeStore(tmp_path)
    monkeypatch.setattr(bugs, "_store", store)
    assert subnets.plan_subnets("10.48.12.0/22") == ["10.48.12.0/24", "10.48.13.0/24", "10.48.14.0/24"]
    store.update(lambda d: d["bugs"].__setitem__("vpc_subnets", True))
    with pytest.raises(IndexError):
        subnets.plan_subnets("10.48.12.0/22")


def test_commented_out_bug_line_is_detected(tmp_path, monkeypatch):
    src = (settings.ROOT / "demo" / "subnets.py").read_text()
    fixed = src.replace('    if bug_mode("vpc_subnets")', '    # if bug_mode("vpc_subnets")')
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "subnets.py").write_text(fixed)
    monkeypatch.setattr(settings, "ROOT", tmp_path)
    st = bugs.line_status("vpc_subnets")
    assert st["line"] and not st["active"]


# --- deploys ----------------------------------------------------------------------------

def test_code_version_changes_with_source_and_compile_check_catches_syntax(tmp_path, monkeypatch):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    monkeypatch.setattr(supervisor, "_worker_sources", lambda: [f])
    monkeypatch.setattr(settings, "ROOT", tmp_path)
    v1 = supervisor.code_version()
    f.write_text("x = 2\n")
    assert supervisor.code_version() != v1
    assert supervisor.compile_errors() == []
    f.write_text("def broken(:\n")
    assert supervisor.compile_errors() and "line 1" in supervisor.compile_errors()[0]


class FakeProc:
    _pid = 1000

    def __init__(self, *a, **kw):
        FakeProc._pid += 1
        self.pid, self.code, self.signals = FakeProc._pid, None, []

    def poll(self):
        return self.code

    def send_signal(self, sig):
        self.signals.append(sig)
        self.code = 0 if sig == signal.SIGTERM else -9

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


def _sup(monkeypatch, version="v1"):
    monkeypatch.setattr(supervisor.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(supervisor, "code_version", lambda: state["v"])
    monkeypatch.setattr(supervisor, "compile_errors", lambda: state["errors"])
    state = {"v": version, "errors": []}
    sup = supervisor.WorkerSupervisor(managed=True)
    return sup, state


def test_rolling_deploy_replaces_old_workers_only_after_new_ones_are_up(monkeypatch):
    sup, state = _sup(monkeypatch)
    a, b = sup.spawn("core"), sup.spawn("core")
    for w in (a, b):
        sup.procs[w]["status"] = "running"
    state["v"] = "v2"
    d = sup.deploy()
    assert d["state"] == "rolling" and d["replaced"] == 2
    new = [r["new"] for r in sup.rollouts]
    sup.tick(auto=False)
    assert sup.procs[a]["proc"].signals == []  # replacements not up yet: old ones keep working
    for w in new:
        sup.procs[w]["status"] = "running"
    sup.tick(auto=False)
    assert sup.procs[a]["proc"].signals == [signal.SIGTERM] and sup.procs[b]["proc"].signals == [signal.SIGTERM]
    assert sup.last_deploy["state"] == "done" and not sup.rollouts
    assert {sup.procs[w]["version"] for w in new} == {"v2"}


def test_deploy_that_will_not_start_keeps_old_workers(monkeypatch):
    sup, state = _sup(monkeypatch)
    a = sup.spawn("onprem")
    sup.procs[a]["status"] = "running"
    state["v"] = "v2"
    sup.deploy()
    new = sup.rollouts[0]["new"]
    sup.procs[new]["proc"].code = 1  # new code crashes on start
    sup.tick(auto=False)
    assert sup.last_deploy["state"] == "failed" and new in sup.last_deploy["error"]
    assert sup.procs[a]["proc"].poll() is None


def test_deploy_refuses_code_that_does_not_compile(monkeypatch):
    sup, state = _sup(monkeypatch)
    sup.procs[sup.spawn("core")]["status"] = "running"
    state.update(v="v2", errors=["demo/subnets.py line 18: invalid syntax"])
    d = sup.deploy()
    assert d["state"] == "failed" and "line 18" in d["error"] and not sup.rollouts


def test_auto_deploy_waits_for_the_file_to_settle(monkeypatch):
    sup, state = _sup(monkeypatch)
    sup.procs[sup.spawn("core")]["status"] = "running"
    state["v"] = "v2"
    sup.tick(auto=True)
    assert not sup.rollouts  # first sighting: wait for the editor to finish saving
    sup._seen_version = ("v2", sup._seen_version[1] - 5)
    sup.tick(auto=True)
    assert sup.rollouts


def test_code_notes_do_not_trigger_redeploys():
    assert not any(p.name == "code_notes.yaml" for p in supervisor._worker_sources())
    assert any(p.name == "subnets.py" for p in supervisor._worker_sources())
    assert not any("api" in p.parts for p in supervisor._worker_sources())


# --- under the hood ------------------------------------------------------------------------

def test_every_snippet_is_found_with_its_live_marks():
    snips = {s["id"]: s for s in code.snippets()}
    assert not [sid for sid, s in snips.items() if s["missing"]]
    marks = {m for s in snips.values() for line in s["lines"] for m in line["marks"]}
    assert {"llm-turn", "tool-call", "review-wait", "step-run", "blocked-wait", "fanout-wait", "approval-wait", "demo-bug"} <= marks
    assert all("[snippet" not in line["text"] and "[mark:" not in line["text"] for s in snips.values() for line in s["lines"])
