"""Worker process.

    python -m demo.worker --pool core   --identity core-1
    python -m demo.worker --pool onprem --identity onprem-1

core    task queue vending-core: the workflows (including the agent workflows for
        any LangChain/LangGraph mode that's installed), Claude, ServiceNow, AWS.
onprem  task queue vending-onprem: the steps that must run inside the data
        center (Ansible against AD, Infoblox, Panorama, Windows DNS; Splunk).
        It only ever makes outbound connections to Temporal.

The demo UI starts and kills these processes for you. In a real deployment
they are just containers (e.g. a Kubernetes Deployment per pool).
"""

import argparse
import asyncio
import logging
import os
import signal
import socket

from temporalio.worker import Worker

from . import settings, worker_stats


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pool", choices=["core", "onprem"], default="core")
    parser.add_argument("--identity", default="")
    parser.add_argument("--max-activities", type=int, default=settings.WORKER_MAX_ACTIVITIES)
    args = parser.parse_args()

    identity = args.identity or f"{args.pool}-{socket.gethostname()}"
    os.environ["DEMO_WORKER_ID"] = identity
    os.environ["DEMO_WORKER_POOL"] = args.pool
    worker_stats.configure(identity, args.pool)

    # Imported after DEMO_WORKER_ID is set so activities report the right identity.
    from . import frameworks
    from .activities import CORE_ACTIVITIES, ONPREM_ACTIVITIES
    from .temporal_client import connect
    from .workflows import AccountVendingWorkflow, FirewallRuleAgentWorkflow, ProvisioningTrackWorkflow

    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s [{identity}] %(levelname)s %(message)s")
    client = await connect(identity=f"{identity}@{socket.gethostname()}")

    if args.pool == "core":
        # [snippet:worker-registration]
        # LangChain / LangGraph agent modes, when their packages are installed.
        extras = frameworks.worker_extras()
        kwargs = {"plugins": extras["plugins"]} if extras["plugins"] else {}
        worker = Worker(
            client,
            task_queue=settings.CORE_QUEUE,
            workflows=[AccountVendingWorkflow, ProvisioningTrackWorkflow, FirewallRuleAgentWorkflow, *extras["workflows"]],
            activities=[*CORE_ACTIVITIES, *extras["activities"]],
            max_concurrent_activities=args.max_activities,
            identity=f"{identity}@{socket.gethostname()}",
            **kwargs,
        )
        # [/snippet]
        logging.info("Agent modes: plain%s", "".join(f", {m}" for m in extras["loaded"]))
    else:
        worker = Worker(
            client,
            task_queue=settings.ONPREM_QUEUE,
            activities=ONPREM_ACTIVITIES,
            max_concurrent_activities=args.max_activities,
            identity=f"{identity}@{socket.gethostname()}",
        )

    writer = asyncio.create_task(worker_stats.writer_loop())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    logging.info("Polling %s (max %d concurrent activities)", settings.CORE_QUEUE if args.pool == "core" else settings.ONPREM_QUEUE, args.max_activities)
    async with worker:
        await stop.wait()
        logging.info("Shutting down gracefully")
    writer.cancel()
    worker_stats._state["stopped"] = True
    worker_stats.write_once()


if __name__ == "__main__":
    asyncio.run(main())
