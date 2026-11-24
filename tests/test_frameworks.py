"""Tests for the LangChain / LangGraph agent modes and the Agent lab.

The pure pieces (message conversion, the offline brain driven through
LangChain-format messages, the lab scorecard) run anywhere. The tests that
need LangChain or LangGraph installed skip themselves when they aren't.
"""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from demo import chaos, frameworks
from demo.agents import firewall as fw
from demo.api import code, lab
from demo.frameworks import lc_messages as lcm

CTX = fw.agent_context({**lab.LAB_SPEC}, dict(lab.LAB_CONTEXT))


def run_plain(ctx, feedback=()):
    """The no-framework loop on Anthropic-format messages (what FirewallRuleAgentWorkflow does)."""
    msgs = [{"role": "user", "content": fw.task_prompt(ctx)}]
    queue, drafts = list(feedback), []
    for _ in range(20):
        content = fw.heuristic_turn(msgs, ctx)
        msgs.append({"role": "assistant", "content": content})
        uses = [b for b in content if b["type"] == "tool_use"]
        sub = next((u for u in uses if u["name"] == "submit_rules"), None)
        results = [{"type": "tool_result", "tool_use_id": u["id"], "content": json.dumps(fw.run_tool(u["name"], u["input"], ctx))} for u in uses if u is not sub]
        if sub:
            drafts.append(sub["input"])
            if not queue:
                return drafts
            msgs.append({"role": "user", "content": [*results, {"type": "tool_result", "tool_use_id": sub["id"], "content": "changes"},
                                                     {"type": "text", "text": fw.feedback_message("NetSec", queue.pop(0))}]})
            continue
        msgs.append({"role": "user", "content": results})
    raise AssertionError("agent did not submit")


def run_langchain_format(ctx, feedback=()):
    """The LangChain-mode loop on serialized LangChain messages, with the offline brain
    reading them through lcm.to_anthropic (what lc_llm_turn does without an API key)."""
    msgs = [lcm.human(fw.task_prompt(ctx))]
    queue, drafts = list(feedback), []
    for _ in range(20):
        ai = lcm.ai_from_blocks(fw.heuristic_turn(lcm.to_anthropic(msgs), ctx))
        msgs.append(ai)
        calls = lcm.calls(ai)
        sub = next((c for c in calls if c["name"] == "submit_rules"), None)
        results = [lcm.tool(c["id"], c["name"], fw.run_tool(c["name"], c["input"], ctx)) for c in calls if c is not sub]
        if sub:
            drafts.append(sub["input"])
            if not queue:
                return drafts, msgs
            msgs += [*results, lcm.tool(sub["id"], "submit_rules", "Not approved"), lcm.human(fw.feedback_message("NetSec", queue.pop(0)))]
            continue
        msgs += results
    raise AssertionError("agent did not submit")


def ports(draft):
    return sorted((r["protocol"], r["port"]) for r in draft["rules"])


# --- message format ------------------------------------------------------------------

def test_lc_message_helpers():
    ai = lcm.ai_from_blocks([{"type": "text", "text": "Looking up"}, {"type": "tool_use", "id": "t1", "name": "lookup_cmdb", "input": {"query": "oracle"}}])
    assert ai["type"] == "ai" and ai["data"]["content"] == "Looking up"
    assert lcm.calls(ai) == [{"id": "t1", "name": "lookup_cmdb", "input": {"query": "oracle"}}]
    assert lcm.calls(lcm.human("hi")) == []
    # Older serializations keep tool calls only as content blocks
    legacy = {"type": "ai", "data": {"content": [{"type": "tool_use", "id": "t2", "name": "check_policy", "input": {"rules": []}}]}}
    assert lcm.calls(legacy)[0]["name"] == "check_policy"
    tm = lcm.tool("t1", "lookup_cmdb", {"matches": []})
    assert tm["data"]["tool_call_id"] == "t1" and json.loads(tm["data"]["content"]) == {"matches": []}


def test_to_anthropic_merges_tool_results_and_feedback_into_one_user_turn():
    msgs = [lcm.human("task"),
            lcm.ai_from_blocks([{"type": "tool_use", "id": "a", "name": "check_policy", "input": {}},
                                {"type": "tool_use", "id": "b", "name": "submit_rules", "input": {}}]),
            lcm.tool("a", "check_policy", {"compliant": True}), lcm.tool("b", "submit_rules", "Not approved"), lcm.human("Reviewer X requested changes: drop NTP")]
    out = lcm.to_anthropic(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert out[0]["content"] == "task"
    assert [b["type"] for b in out[2]["content"]] == ["tool_result", "tool_result", "text"]
    assert fw.feedback(out) == ["drop NTP"]


def test_langchain_format_agent_matches_the_plain_agent():
    """Same brain, same tools: the LangChain message format changes nothing about the draft."""
    plain = run_plain(CTX)
    lc, _ = run_langchain_format(CTX)
    assert ports(plain[-1]) == ports(lc[-1]) == [("tcp", 1414), ("tcp", 1521), ("udp", 123)]


def test_langchain_format_agent_revises_on_feedback():
    drafts, msgs = run_langchain_format(CTX, feedback=["Drop the NTP rule; we use Amazon Time Sync"])
    assert ports(drafts[0]) == [("tcp", 1414), ("tcp", 1521), ("udp", 123)]
    assert ports(drafts[1]) == [("tcp", 1414), ("tcp", 1521)]
    assert json.loads(json.dumps(msgs)) == msgs  # workflow state stays plain JSON


# --- modes and the lab --------------------------------------------------------------

def test_modes_are_consistent():
    assert set(frameworks.MODES) == {"plain", "langchain", "langgraph"}
    assert frameworks.check("plain") is None
    assert "Unknown" in frameworks.check("crewai")
    for m, meta in frameworks.MODES.items():
        assert meta["workflow"] and meta["label"] and meta["boundary"]
        reason = frameworks.check(m)
        assert reason is None or meta["install"] in reason
    avail = frameworks.availability()
    assert avail["plain"]["available"] and "requires" not in avail["plain"]


def test_lab_snippets_exist_and_stay_out_of_the_main_list():
    snips = {s["id"]: s for s in code.snippets()}
    lab_ids = {"worker-registration", "plain-claude-call", "plain-tool-activity", "plain-tools", "lc-loop", "lc-run", "lc-turn",
               "lc-model", "lc-tool", "lc-tools", "lc-coarse", "lg-workflow", "lg-plugin", "lg-plugin-does", "lg-graph", "lg-routes",
               "lg-agent-node", "lg-tools-node", "lg-review-node"}
    for sid in lab_ids:
        assert not snips[sid]["missing"], sid
        assert snips[sid]["section"] == "lab", sid
        assert 0 < len(snips[sid]["lines"]) <= 35, f"{sid} should be a focused excerpt"
    assert snips["lc-coarse"]["illustration"] and snips["lg-plugin-does"]["illustration"]
    assert not snips["lc-model"]["illustration"]
    # The main Under the hood list
    assert [s["id"] for s in snips.values() if s["section"] == "hood"] == [
        "agent-loop", "human-review", "agent-activity", "retries", "demo-bug", "fan-out", "approvals"]
    marks = {m for s in snips.values() for line in s["lines"] for m in line["marks"]}
    assert {"lc-turn", "lc-tool", "lg-interrupt", "lg-run", "lg-review-wait"} <= marks


BOXES = {"service", "person", "worker", "workflow", "workflow.review", "llm", "llm.inner", "tool", "tool.inner", "claude", "systems"}
GRAPH_BOXES = {"graph", "review", "review.inner"}


def test_lab_content_is_consistent():
    """Every diagram step points at boxes that exist and code that exists; every scenario and table cell is filled."""
    content = lab.content()
    assert "error" not in content
    snips = {s["id"] for s in code.snippets()}
    assert set(content["modes"]) == set(frameworks.MODES)
    for mode, m in content["modes"].items():
        boxes = BOXES | (GRAPH_BOXES if mode == "langgraph" else set())
        assert {"workflow", "llm", "tool"} <= set(m["boxes"]), mode
        refs = []
        for b in m["boxes"].values():
            refs += b.get("code", []) + (b.get("inner") or {}).get("code", []) + (b.get("review") or {}).get("code", [])
        refs += (m.get("note") or {}).get("code", [])
        assert 7 <= len(m["steps"]) <= 10, mode
        lives = [st.get("live") for st in m["steps"] if st.get("live")]
        assert set(lives) == {"idle", "llm", "tool", "review", "recover"}, (mode, lives)
        for st in m["steps"]:
            assert st["title"] and st["text"]
            assert set(st.get("lit", [])) <= boxes, (mode, st["title"], set(st["lit"]) - boxes)
            for arrow in st.get("arrows", []):
                assert arrow[0] in boxes and arrow[1] in boxes, (mode, st["title"], arrow)
            refs += st.get("code", [])
        assert set(refs) <= snips, (mode, set(refs) - snips)
    assert set(content["shared"]["worker"]["code"]) <= snips
    assert [x["id"] for x in content["scenarios"]] == ["happy", "crash", "flaky_cmdb", "claude_429"]
    owners = set(content["owners"])
    for row in content["matrix"]:
        for m in frameworks.MODES:
            assert row[m] and all(o in owners and txt for o, txt in row[m]), (row["job"], m)


def test_lab_runs_accepts_the_old_list_format():
    old = {"runs": [{"mode": "plain", "workflow_id": "lab-1-plain"}, {"mode": "langchain", "workflow_id": "lab-1-langchain"}]}
    assert set(lab.lab_runs(old)) == {"plain", "langchain"}
    assert lab.lab_runs({"runs": {"langgraph": {"workflow_id": "lab-2-langgraph"}, "bad": {}}}) == {"langgraph": {"workflow_id": "lab-2-langgraph"}}
    assert lab.lab_runs(None) == {}
    assert lab.scenario("nope")["id"] == "happy"


def test_run_view_only_counts_crashes_after_the_run_started():
    agent = {"status": "drafting", "transcript": [{"kind": "turn", "n": 1, "at": _iso(5), "attempt": 1},
                                                 {"kind": "tool", "name": "lookup_cmdb", "input": {"query": "Oracle"}, "at": _iso(4), "attempt": 1}]}
    now = time.time()
    view = lab.run_view("langchain", {"workflow_id": "w", "scenario": "crash", "started_at": now - 8}, agent,
                        [{"at": now - 60}, {"at": now - 2}])
    assert len(view["crashes"]) == 1 and view["scenario"]["label"] == "Worker crash mid-run"
    assert view["example"] == 'lookup_cmdb(query="Oracle")'
    assert view["score"]["coarse"] == {"model_calls": 1, "tool_calls": 1}


def _iso(seconds_ago):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def test_lab_scorecard_counts_repeats_and_what_coarse_would_lose():
    agent = {"status": "awaiting_review", "round": 1, "stats": {"input_tokens": 100, "output_tokens": 20}, "transcript": [
        {"kind": "turn", "n": 1, "at": _iso(30), "attempt": 1}, {"kind": "tool", "name": "lookup_cmdb", "at": _iso(29), "attempt": 2},
        {"kind": "turn", "n": 2, "at": _iso(20), "attempt": 1},
        {"kind": "turn", "n": 3, "at": _iso(5), "attempt": 2},  # in flight when the workers died
    ], "pending": []}
    crashes = [{"at": time.time() - 10}]
    sc = lab.score(agent, crashes)
    assert (sc["turns"], sc["model_calls"], sc["repeated_model_calls"]) == (3, 4, 1)
    assert (sc["tool_calls"], sc["tool_runs"], sc["repeated_tool_runs"]) == (1, 2, 1)
    assert sc["coarse"] == {"model_calls": 2, "tool_calls": 1}  # everything finished before the crash
    assert sc["tokens"] == 120
    units = lab.units(agent)
    assert [u["kind"] for u in units] == ["turn", "tool", "turn", "turn", "wait"]


def test_lab_units_prefer_graph_nodes_and_show_work_in_flight():
    agent = {"status": "drafting", "transcript": [], "units": [{"kind": "node", "node": "agent", "label": "agent", "attempt": 1, "at": _iso(3)}],
             "pending": [{"activity_type": "agent_llm_turn", "attempt": 2, "next_attempt_at_future": True, "last_failure": {"type": "Timeout"}}]}
    units = lab.units(agent)
    assert [u["kind"] for u in units] == ["node", "pending"]
    assert units[1]["state"] == "retrying" and units[1]["label"] == "Model turn"
    assert lab.score(agent, [])["coarse"] is None


# --- with the optional packages installed ----------------------------------------------

def test_langchain_tools_mirror_the_plain_tools():
    pytest.importorskip("langchain_core")
    pytest.importorskip("langchain_anthropic")
    pytest.importorskip("temporalio")
    from demo.frameworks import langchain_mode

    tools = {t.name: t for t in langchain_mode.make_tools(CTX["vpc_cidr"])}
    assert set(tools) == {t["name"] for t in fw.TOOLS}
    rules = [{"name": "aws-x-to-oracle", "source": CTX["vpc_cidr"], "destination": "10.20.14.25", "port": 1521, "protocol": "tcp", "purpose": "db"}]
    got = asyncio.run(tools["check_policy"].ainvoke({"rules": rules}))
    assert got == fw.check_policy(rules, CTX["vpc_cidr"])
    assert asyncio.run(tools["lookup_cmdb"].ainvoke({"query": "oracle"}))["matches"][0]["ip"] == "10.20.14.25"


def test_langgraph_graph_runs_through_review_without_temporal(tmp_path, monkeypatch):
    """The graph is plain LangGraph: run it locally (offline brain), interrupt for review, resume."""
    pytest.importorskip("langgraph")
    pytest.importorskip("langchain_anthropic")
    pytest.importorskip("temporalio")
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from demo.frameworks import common, langgraph_mode

    store = chaos.RuntimeStore(tmp_path)
    store.update(lambda d: d.__setitem__("speed", 0.01))
    monkeypatch.setattr(common, "STORE", store)

    app = langgraph_mode.build_graph().compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t1"}}
    start = {"messages": [lcm.human(fw.task_prompt(CTX))], "ctx": CTX, "request_id": "LAB-1", "max_turns": 10,
             "turns": 0, "round": 1, "decision": None, "events": []}

    async def run(inp):
        paused, nodes = None, []
        async for update in app.astream(inp, config, stream_mode="updates"):
            for node, data in update.items():
                if node == "__interrupt__":
                    first = data[0] if isinstance(data, (list, tuple)) else data
                    paused = getattr(first, "value", first)
                else:
                    nodes.append(node)
        return paused, nodes

    paused, nodes = asyncio.run(run(start))
    assert nodes[:2] == ["agent", "tools"] and paused is not None
    assert ports(paused["draft"]) == [("tcp", 1414), ("tcp", 1521), ("udp", 123)]

    paused, nodes = asyncio.run(run(Command(resume={"decision": "changes", "reviewer": "NetSec", "comment": "Drop the NTP rule"})))
    assert nodes[0] == "review" and paused is not None
    assert ports(paused["draft"]) == [("tcp", 1414), ("tcp", 1521)]

    paused, nodes = asyncio.run(run(Command(resume={"decision": "approve", "reviewer": "NetSec", "comment": ""})))
    assert paused is None and nodes == ["review"]


def test_injected_429s_fail_model_calls_then_stop(tmp_path, monkeypatch):
    """The Claude rate-limited scenario: armed per runtime, used up after N calls."""
    pytest.importorskip("temporalio")
    from demo.frameworks import common

    store = chaos.RuntimeStore(tmp_path)
    monkeypatch.setattr(common, "STORE", store)
    store.arm_fault({"id": "lab_429_langchain", "kind": "transient", "target": "agent-langchain.llm_turn", "error_type": "RateLimitError", "times": 2, "message": "429"})
    asyncio.run(common.inject_model_fault("plain"))  # other runtimes are untouched
    for _ in range(2):
        with pytest.raises(common.ModelCallFailed):
            asyncio.run(common.inject_model_fault("langchain"))
    asyncio.run(common.inject_model_fault("langchain"))  # used up
