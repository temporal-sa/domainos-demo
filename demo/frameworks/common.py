"""Activity-side helpers shared by every agent runtime (no framework, LangChain, LangGraph).

Keeping these in one place is what keeps the comparison fair: the same tools,
the same failure injection, the same heartbeats and the same offline brain
run underneath all three modes. Only the orchestration on top differs.

No LangChain or LangGraph imports here: the plain worker uses this module too.
"""

import asyncio
import contextlib
import os
import socket
from types import ModuleType
from typing import Any, Dict, Iterator, Optional

from temporalio import activity

from .. import chaos, worker_stats

STORE = chaos.RuntimeStore()
WORKER_ID = os.getenv("DEMO_WORKER_ID") or f"worker@{socket.gethostname()}"

# Must match maximum_attempts in the workflows' AI retry policy: on the last
# attempt the agent falls back to the offline brain instead of failing.
AI_MAX_ATTEMPTS = 4

# With no API key the offline brain answers instantly. A short pause makes the
# turns visible, and gives a "kill the worker mid-turn" click something to hit.
OFFLINE_TURN_SECONDS = 1.6


def in_activity() -> bool:
    try:
        activity.info()
        return True
    except RuntimeError:
        return False


def attempt() -> int:
    return activity.info().attempt if in_activity() else 1


def speed() -> float:
    return float(STORE.read().get("speed", 1.0) or 1.0)


@contextlib.asynccontextmanager
async def heartbeating(every: float = 2.0, **details: Any):
    """Heartbeat while a slow call (a model turn) is in flight.

    With heartbeat_timeout set on the activity, a worker killed mid-call is
    noticed within seconds and the turn moves to another worker, instead of
    waiting out the whole start_to_close timeout.
    """
    if not in_activity():
        yield
        return

    async def beat() -> None:
        while True:
            activity.heartbeat({"worker": WORKER_ID, **details})
            await asyncio.sleep(every)

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@contextlib.contextmanager
def tracked(label: str, **meta: Any) -> Iterator[str]:
    """Show this call on the worker card in the demo controls while it runs."""
    if not in_activity():
        yield ""
        return
    info = activity.info()
    key = worker_stats.begin(f"{info.workflow_id}:{info.activity_id}:{info.attempt}",
                             {"label": label, "attempt": info.attempt, "workflow_id": info.workflow_id,
                              "activity_type": info.activity_type, **meta})
    try:
        yield key
    except BaseException as e:  # noqa: BLE001 - recorded, then re-raised
        worker_stats.end(key, ok=False, error="cancelled" if isinstance(e, asyncio.CancelledError) else str(e)[:200])
        raise
    worker_stats.end(key, ok=True)


class ModelCallFailed(Exception):
    """An injected model-call failure (the Agent lab's "Claude rate-limited" scenario)."""


async def inject_model_fault(mode: str = "plain") -> None:
    """Fail this model call if the Agent lab armed a fault for this runtime."""
    try:
        await asyncio.to_thread(chaos.apply, STORE, f"agent-{mode}", "llm_turn", "claude")
    except chaos.InjectedFault as e:
        raise ModelCallFailed(f"{e.error_type}: {e}") from None


async def offline_pause(mode: str = "plain") -> None:
    await inject_model_fault(mode)
    await asyncio.sleep(OFFLINE_TURN_SECONDS * speed())


async def call_tool(agent: ModuleType, name: str, args: Dict[str, Any], context: Dict[str, Any],
                    run: Optional[Any] = None) -> Dict[str, Any]:
    """Run one agent tool against its (mock) system, with the demo's failure injection.

    `run` lets a framework supply its own way of invoking the tool (LangChain's
    `tool.ainvoke`); by default the agent module's plain Python function runs.
    """
    system = getattr(agent, "TOOL_SYSTEMS", {}).get(name, "agent")
    async with heartbeating(tool=name):
        extra = await asyncio.to_thread(chaos.apply, STORE, "agent", name, system)
        await asyncio.sleep(0.5 * speed() + extra)
        if run is not None:
            return await run()
        return agent.run_tool(name, args, context)
