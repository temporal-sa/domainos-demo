"""Tests for the pure-Python pieces (no Temporal server needed)."""

import asyncio
import random

import pytest

from demo import chaos
from demo.ai import tasks as ai
from demo.api import view
from demo.catalog import build_plan, depth_map, load_catalog, matches, validate, CatalogError
from demo.connectors import get as get_connector
from demo.connectors.ansible import playbook_tasks, AnsibleMockConnector
from demo.connectors.base import StepContext
from demo import settings

PROD_PII_ONPREM = {"environment": "prod", "data_classification": "Confidential", "onprem_connectivity": True, "cidr_size": "/22", "account_name": "mortgage-analytics-prod"}
SANDBOX = {"environment": "sandbox", "data_classification": "Internal", "onprem_connectivity": False, "cidr_size": "/24", "account_name": "fraud-ds-sandbox"}


# --- catalog -------------------------------------------------------------------

def test_catalog_is_valid():
    validate(load_catalog())


def test_conditions():
    assert matches("onprem_connectivity", {"onprem_connectivity": True})
    assert not matches("onprem_connectivity", {"onprem_connectivity": False})
    assert matches("!onprem_connectivity", {})
    assert matches({"environment": ["prod", "test"]}, {"environment": "PROD"})
    assert not matches({"environment": ["prod"], "data_classification": ["Restricted"]}, {"environment": "prod", "data_classification": "Internal"})
    assert matches([{"environment": "dev"}, "onprem_connectivity"], {"environment": "prod", "onprem_connectivity": True})


def test_prod_plan_has_every_team_and_gated_firewall():
    plan = build_plan(PROD_PII_ONPREM)
    ids = [t.id for t in plan.tracks]
    assert ids == ["account", "identity", "access", "network", "firewall", "dns", "security", "logging", "finops", "cmdb"]
    assert {a.id for a in plan.approvals} == {"cloud_coe", "infosec", "finance"}
    fw = next(t for t in plan.tracks if t.id == "firewall")
    assert [s.kind for s in fw.steps] == ["agent", "action", "action"]
    assert fw.steps[0].agent == "firewall_rules" and fw.steps[0].reviewer
    assert all(s.task_queue == settings.ONPREM_QUEUE for s in fw.steps[1:])
    assert not any(a.auto for a in plan.approvals)


def test_sandbox_plan_drops_onprem_work_and_auto_approves():
    plan = build_plan(SANDBOX)
    ids = [t.id for t in plan.tracks]
    assert "firewall" not in ids
    assert {s.id for s in next(t for t in plan.tracks if t.id == "dns").steps} == {"private_zone"}
    assert {s.id for s in next(t for t in plan.tracks if t.id == "network").steps} == {"allocate_cidr", "create_vpc"}
    assert [a.id for a in plan.approvals] == ["cloud_coe"]
    assert plan.approvals[0].auto
    assert any(s.id == "firewall" for s in plan.skipped)


def test_depths_form_a_fan_out():
    d = depth_map(build_plan(PROD_PII_ONPREM).tracks)
    assert d["account"] == d["identity"] == 0
    assert d["network"] == d["access"] == d["security"] == d["finops"] == 1
    assert d["firewall"] == d["dns"] == d["logging"] == d["cmdb"] == 2


def test_cycle_detection():
    cat = {"systems": {"x": {}}, "tracks": [
        {"id": "a", "name": "A", "depends_on": ["b"], "steps": [{"id": "s", "name": "s", "system": "x", "action": "y"}]},
        {"id": "b", "name": "B", "depends_on": ["a"], "steps": [{"id": "s", "name": "s", "system": "x", "action": "y"}]},
    ]}
    with pytest.raises(CatalogError):
        validate(cat)


# --- chaos ----------------------------------------------------------------------

def test_hard_fault_fires_once(tmp_path):
    store = chaos.RuntimeStore(tmp_path)
    store.arm_fault({"id": "ipam_exhausted", "kind": "hard", "target": "network.allocate_cidr", "error_type": "IpamPoolExhausted", "message": "no space", "times": 1})
    assert chaos.apply(store, "account", "create_account", "aws_orgs") == 0
    with pytest.raises(chaos.InjectedFault) as e:
        chaos.apply(store, "network", "allocate_cidr", "ipam")
    assert e.value.error_type == "IpamPoolExhausted" and not e.value.retryable
    chaos.apply(store, "network", "allocate_cidr", "ipam")  # charge used up: passes on retry
    assert store.read()["faults"] == []


def test_transient_system_fault_counts_down(tmp_path):
    store = chaos.RuntimeStore(tmp_path)
    store.arm_fault({"id": "okta", "kind": "transient", "target": "system:okta", "error_type": "RateLimited", "message": "429", "times": 3})
    fails = 0
    for _ in range(5):
        try:
            chaos.apply(store, "identity", "okta_group", "okta")
        except chaos.InjectedFault as e:
            assert e.retryable
            fails += 1
    assert fails == 3


def test_error_rate_and_latency(tmp_path):
    store = chaos.RuntimeStore(tmp_path)
    store.set_system("servicenow", error_rate=1.0, latency_ms=250)
    with pytest.raises(chaos.InjectedFault):
        chaos.apply(store, "cmdb", "register_ci", "servicenow", rng=random.Random(1))
    store.set_system("servicenow", error_rate=0.0)
    assert chaos.apply(store, "cmdb", "register_ci", "servicenow") == pytest.approx(0.25)


# --- connectors -------------------------------------------------------------------

def _ctx(action, system="aws_orgs", **kw):
    base = dict(request_id="REQ-1234", track_id="t", step_id="s", step_name=action, system=system, system_label=system,
                action=action, params={}, spec=PROD_PII_ONPREM, context={}, seconds=0.05, speed=1.0)
    base.update(kw)
    return StepContext(**base)


def test_mock_ids_are_stable_across_retries():
    conn = get_connector("aws_orgs")
    a = asyncio.run(conn.execute(_ctx("create_account")))
    b = asyncio.run(conn.execute(_ctx("create_account")))
    assert a["account_id"] == b["account_id"] and len(a["account_id"]) == 12
    c = asyncio.run(conn.execute(_ctx("create_account", request_id="REQ-9999")))
    assert c["account_id"] != a["account_id"]


def test_ansible_mock_replays_the_real_playbook():
    tasks = playbook_tasks(settings.ANSIBLE_DIR / "playbooks" / "ipam_allocate.yml")
    assert [t["task"] for t in tasks][:2] == ["Look up the next available network in the workload container", "Reserve the network for this account"]
    conn = get_connector("ipam")
    assert isinstance(conn, AnsibleMockConnector)
    ctx = _ctx("run_playbook", system="ipam", params={"playbook": "ipam_allocate.yml"})
    out = asyncio.run(conn.execute(ctx))
    assert out["vpc_cidr"].startswith("10.48.") and out["vpc_cidr"].endswith("/22")
    assert any(line.startswith("TASK [Reserve the network") for line in ctx.log)


def test_every_catalog_step_has_a_mock_output():
    cat = load_catalog()
    for t in cat["tracks"]:
        for s in t["steps"]:
            conn = get_connector(s["system"])
            ctx = _ctx(s["action"], system=s["system"], params=s.get("params") or {})
            assert isinstance(conn.build_output(ctx), dict), f"{t['id']}.{s['id']}"


# --- AI fallbacks -----------------------------------------------------------------

@pytest.mark.parametrize("preset,env,cls,onprem", [
    (0, "prod", "Confidential", True),
    (1, "sandbox", "Internal", False),
    (2, "dev", "Internal", False),
    (3, "prod", "Restricted", True),
])
def test_heuristic_parse_presets(preset, env, cls, onprem):
    p = view.presets()[preset]
    spec = ai.heuristic_parse(p["text"], p["requester"])
    assert (spec["environment"], spec["data_classification"], spec["onprem_connectivity"]) == (env, cls, onprem)
    assert set(ai.INTAKE_SCHEMA["required"]) <= set(spec)


def test_parse_request_falls_back_without_a_key():
    spec, meta = asyncio.run(ai.parse_request(view.presets()[0]["text"], "Jordan Lee"))
    assert meta["source"] == "heuristic"
    assert spec["account_name"] == "mortgage-analytics-prod"


def test_schemas_are_structured_output_safe():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
    for schema in (ai.INTAKE_SCHEMA, ai.RISK_SCHEMA, ai.DIAGNOSIS_SCHEMA, ai.SUMMARY_SCHEMA):
        walk(schema)


# --- view -------------------------------------------------------------------------

def test_headline_for_blocked_track_has_plain_version():
    v = {"status": "in_progress", "stage": "provisioning", "approvals": [], "stage_times": {},
         "tracks": [{"id": "network", "team": "Network Engineering", "status": "blocked", "blocked_step": "Reserve a CIDR block in IPAM",
                     "diagnosis": {"requester_note": "Network Engineering is freeing up IP space."}}]}
    h = view.headline(v)
    assert h["tone"] == "bad" and "Reserve a CIDR block" in h["text"]
    assert h["plain"] == "Network Engineering is freeing up IP space."


def test_temporal_links():
    links = view.Links("https://cloud.temporal.io", "my-namespace.a1b2c")
    assert links.workflow("vend-req-1.network", "run1") == "https://cloud.temporal.io/namespaces/my-namespace.a1b2c/workflows/vend-req-1.network/run1/history"
    assert "STARTS_WITH" in links.family("vend-req-1").replace("%20", " ")


def test_temporal_keeps_listing_killed_workers_for_five_minutes():
    """Temporal lists a worker until 5 minutes after its last poll; the panel tells live from listed."""
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
    ago = lambda s: (now - timedelta(seconds=s)).isoformat()  # noqa: E731
    workers = [{"id": "core-1", "status": "killed"}, {"id": "core-2", "status": "running"}, {"id": "core-3", "status": "running"}]
    pollers = [{"identity": "core-1", "last_access": ago(40)}, {"identity": "core-2", "last_access": ago(2)},
               {"identity": "core-3", "last_access": ago(120)},  # busy at capacity: not polling, still alive
               {"identity": "temporal-system", "last_access": ago(5)}, {"identity": "laptop-worker", "last_access": ago(200)}]
    out = {p["identity"]: p["state"] for p in view.classify_pollers(pollers, workers, now)}
    assert out == {"core-1": "dead", "core-2": "alive", "core-3": "alive", "temporal-system": "recent", "laptop-worker": "quiet"}
    assert workers[0]["last_poll_s"] == 40 and workers[0]["listed_for_s"] == 260
