"""End-to-end workflow tests against a real (local, in-process) Temporal server.

Needs `temporalio` installed. The first run downloads the Temporal dev server
binary, so it needs network access once.

    pytest tests/test_workflows.py -v
"""

import asyncio
import uuid

import pytest

temporalio = pytest.importorskip("temporalio")

from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from demo import chaos, frameworks, settings  # noqa: E402
from demo.activities import CORE_ACTIVITIES, ONPREM_ACTIVITIES  # noqa: E402
from demo.api import lab  # noqa: E402
from demo.models import AgentInput, AgentResult, ApprovalDecision, DemoOptions, Resolution, ReviewDecision, VendingInput  # noqa: E402
from demo.workflows import AccountVendingWorkflow, FirewallRuleAgentWorkflow, ProvisioningTrackWorkflow  # noqa: E402

EXTRAS = frameworks.worker_extras()  # LangChain / LangGraph modes, when installed

PROD = "Production AWS account for the Mortgage Analytics team. Holds borrower PII and needs the Oracle database in the data center. Cost center 4410. Owner jlee@example.com."
SANDBOX = "Sandbox account for the Fraud Data Science team. No customer data, no on-prem access. Cost center 7720."


@pytest.fixture
def store():
    s = chaos.RuntimeStore()
    s.update(lambda d: (d.__setitem__("speed", 0.02), d.__setitem__("faults", []), d.__setitem__("systems", {})))
    return s


def _core_worker(env):
    return Worker(env.client, task_queue=settings.CORE_QUEUE,
                  workflows=[AccountVendingWorkflow, ProvisioningTrackWorkflow, FirewallRuleAgentWorkflow, *EXTRAS["workflows"]],
                  activities=[*CORE_ACTIVITIES, *EXTRAS["activities"]],
                  **({"plugins": EXTRAS["plugins"]} if EXTRAS["plugins"] else {}))


async def _run(env, inp, during=None):
    async with _core_worker(env), Worker(env.client, task_queue=settings.ONPREM_QUEUE, activities=ONPREM_ACTIVITIES):
        handle = await env.client.start_workflow(AccountVendingWorkflow.run, inp, id=f"vend-test-{uuid.uuid4().hex[:8]}", task_queue=settings.CORE_QUEUE)
        if during:
            await during(env, handle)
        return handle, await asyncio.wait_for(handle.result(), timeout=120)


async def _wait_for(handle, pred, timeout=60):
    for _ in range(int(timeout / 0.25)):
        state = await handle.query(AccountVendingWorkflow.get_state)
        if pred(state):
            return state
        await asyncio.sleep(0.25)
    raise AssertionError("condition not reached")


async def _approve_all(handle):
    st = await _wait_for(handle, lambda s: s["stage"] == "approvals" and s["approvals"])
    for a in st["approvals"]:
        if a["status"] == "pending":
            await handle.execute_update(AccountVendingWorkflow.decide_approval, ApprovalDecision(a["id"], "approved", "test"))


async def _agent_handle(env, handle):
    """Wait for the firewall agent to ask for review; return its workflow handle."""
    st = await _wait_for(handle, lambda s: any(t["status"] == "awaiting_review" for t in s["tracks"]))
    fw = next(t for t in st["tracks"] if t["status"] == "awaiting_review")
    steps = await env.client.get_workflow_handle(fw["workflow_id"]).query(ProvisioningTrackWorkflow.get_steps)
    agent_id = next(x["agent_workflow_id"] for x in steps["steps"] if x.get("agent_workflow_id"))
    return env.client.get_workflow_handle(agent_id)


@pytest.mark.asyncio
async def test_sandbox_runs_end_to_end(store):
    async with await WorkflowEnvironment.start_local() as env:
        _, result = await _run(env, VendingInput("REQ-T1", SANDBOX, "Riya Kim", DemoOptions(heartbeat_seconds=5)))
    assert result["status"] == "completed"
    assert result["outputs"]["account_id"]


@pytest.mark.asyncio
async def test_prod_waits_for_people_then_recovers_from_ipam_failure(store):
    store.arm_fault({"id": "ipam_exhausted", "kind": "hard", "target": "network.allocate_cidr", "error_type": "IpamPoolExhausted", "message": "pool full", "times": 1})

    async def during(env, handle):
        await _approve_all(handle)
        st = await _wait_for(handle, lambda s: any(t["status"] == "blocked" for t in s["tracks"]))
        net = next(t for t in st["tracks"] if t["id"] == "network")
        child = env.client.get_workflow_handle(net["workflow_id"])
        await child.execute_update(ProvisioningTrackWorkflow.resolve, Resolution("retry", "test"))
        # Network Security asks for a change once, then approves the revised draft.
        agent = await _agent_handle(env, handle)
        await agent.execute_update(FirewallRuleAgentWorkflow.review_draft, ReviewDecision("changes", "test", "Drop the NTP rule"))
        for _ in range(200):
            a = await agent.query(FirewallRuleAgentWorkflow.get_agent)
            if a["status"] == "awaiting_review" and a["round"] == 2:
                break
            await asyncio.sleep(0.25)
        assert [r["port"] for r in a["draft"]["rules"]] == [1521]
        await agent.execute_update(FirewallRuleAgentWorkflow.review_draft, ReviewDecision("approve", "test"))

    async with await WorkflowEnvironment.start_local() as env:
        handle, result = await _run(env, VendingInput("REQ-T2", PROD, "Jordan Lee", DemoOptions(heartbeat_seconds=5)), during)
        state = await handle.query(AccountVendingWorkflow.get_state)
    assert result["status"] == "completed"
    assert all(t["status"] == "completed" for t in state["tracks"])


@pytest.mark.asyncio
async def test_rejection_closes_the_request(store):
    async def during(env, handle):
        st = await _wait_for(handle, lambda s: s["stage"] == "approvals" and s["approvals"])
        await handle.execute_update(AccountVendingWorkflow.decide_approval, ApprovalDecision(st["approvals"][0]["id"], "rejected", "test", "no"))

    async with await WorkflowEnvironment.start_local() as env:
        _, result = await _run(env, VendingInput("REQ-T3", PROD, "Jordan Lee"), during)
    assert result["status"] == "rejected"


@pytest.mark.asyncio
async def test_operator_rollback_undoes_completed_tracks(store):
    store.arm_fault({"id": "panorama_lock", "kind": "hard", "target": "firewall.fw_commit", "error_type": "PanoramaCommitLocked", "message": "locked", "times": 1})

    async def during(env, handle):
        await _approve_all(handle)
        agent = await _agent_handle(env, handle)
        await agent.execute_update(FirewallRuleAgentWorkflow.review_draft, ReviewDecision("approve", "test"))
        st = await _wait_for(handle, lambda s: any(t["status"] == "blocked" for t in s["tracks"]))
        fw = next(t for t in st["tracks"] if t["status"] == "blocked")
        await env.client.get_workflow_handle(fw["workflow_id"]).execute_update(ProvisioningTrackWorkflow.resolve, Resolution("rollback", "test"))

    async with await WorkflowEnvironment.start_local() as env:
        handle, result = await _run(env, VendingInput("REQ-T4", PROD, "Jordan Lee", DemoOptions(heartbeat_seconds=5)), during)
        state = await handle.query(AccountVendingWorkflow.get_state)
    assert result["status"] == "rolled_back"
    assert any(t["status"] == "rolled_back" for t in state["tracks"])


@pytest.mark.asyncio
async def test_bug_mode_step_waits_for_a_fix(store, monkeypatch):
    """With bug mode on, the VPC step keeps retrying; turning the bug off lets it finish."""
    store.update(lambda d: d["bugs"].__setitem__("vpc_subnets", True))

    async def during(env, handle):
        await _wait_for(handle, lambda s: any(t["id"] == "network" and t["status"] == "running" for t in s["tracks"]))
        await asyncio.sleep(8)  # several failed attempts
        st = await handle.query(AccountVendingWorkflow.get_state)
        assert next(t for t in st["tracks"] if t["id"] == "network")["status"] == "running"  # not blocked: still retrying
        store.update(lambda d: d["bugs"].__setitem__("vpc_subnets", False))  # stands in for "comment out the line"

    async with await WorkflowEnvironment.start_local() as env:
        _, result = await _run(env, VendingInput("REQ-T5", SANDBOX, "Riya Kim", DemoOptions(retry_forever=["network.create_vpc"])), during)
    assert result["status"] == "completed"


# --- agent modes ------------------------------------------------------------------------

async def _agent_state(handle, pred, timeout=60):
    for _ in range(int(timeout / 0.25)):
        a = await handle.query("get_agent")
        if pred(a):
            return a
        await asyncio.sleep(0.25)
    raise AssertionError("agent condition not reached")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["plain", "langchain", "langgraph"])
async def test_agent_mode_standalone_with_change_request(store, mode):
    """The Agent lab path: one agent workflow per mode, no parent, reviewed twice."""
    if frameworks.check(mode):
        pytest.skip(frameworks.check(mode))
    async with await WorkflowEnvironment.start_local() as env:
        async with _core_worker(env):
            h = await env.client.start_workflow(
                frameworks.MODES[mode]["workflow"],
                AgentInput(request_id="LAB-T", parent_workflow_id="", agent="firewall_rules", spec=dict(lab.LAB_SPEC),
                           context=dict(lab.LAB_CONTEXT), reviewer=lab.LAB_REVIEWER, mode=mode),
                id=f"lab-test-{mode}-{uuid.uuid4().hex[:6]}", task_queue=settings.CORE_QUEUE, result_type=AgentResult)
            a = await _agent_state(h, lambda a: a["status"] == "awaiting_review")
            assert sorted(r["port"] for r in a["draft"]["rules"]) == [123, 1414, 1521]
            await h.execute_update("review_draft", ReviewDecision("changes", "test", "Drop the NTP rule"), result_type=str)
            a = await _agent_state(h, lambda a: a["status"] == "awaiting_review" and a["round"] == 2)
            assert sorted(r["port"] for r in a["draft"]["rules"]) == [1414, 1521]
            await h.execute_update("review_draft", ReviewDecision("approve", "test"), result_type=str)
            result = await asyncio.wait_for(h.result(), timeout=60)
    assert result.status == "approved" and result.rounds == 2 and len(result.rules) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["langchain", "langgraph"])
async def test_request_uses_the_chosen_agent_mode(store, mode):
    """A request placed with a framework mode runs that agent workflow under the firewall track."""
    if frameworks.check(mode):
        pytest.skip(frameworks.check(mode))

    async def during(env, handle):
        await _approve_all(handle)
        agent = await _agent_handle(env, handle)
        desc = await agent.describe()
        assert desc.workflow_type == frameworks.MODES[mode]["workflow"]
        await agent.execute_update("review_draft", ReviewDecision("approve", "test"), result_type=str)

    async with await WorkflowEnvironment.start_local() as env:
        _, result = await _run(env, VendingInput("REQ-T6", PROD, "Jordan Lee", DemoOptions(heartbeat_seconds=5, agent_mode=mode)), during)
    assert result["status"] == "completed"
