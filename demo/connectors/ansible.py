"""Runs Ansible playbooks as Temporal activity steps.

This is the "build our own automation platform on Temporal" piece: Temporal
schedules, retries and tracks each playbook run; Ansible does the device work
(AD, Infoblox, Panorama, Windows DNS). Steps that use this connector run on
the on-prem worker pool, next to the inventory.

mock mode: reads the real playbook file and replays its task names with
           realistic output, so the demo shows exactly what would run.
real mode: runs the playbook with ansible-runner and streams every task event
           back to Temporal as a heartbeat.

Playbooks hand results back to the workflow by ending with:
    - name: Return output to Temporal
      ansible.builtin.debug:
        var: temporal_output
"""

import asyncio
import queue
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List

import yaml

from .. import settings
from .base import Connector, StepContext, StepFailed
from .mock import MockConnector

SAMPLE_HOSTS = {
    "domain_controllers": "dc01.corp.example",
    "infoblox": "gm01.infoblox.corp.example",
    "panorama": "panorama01.corp.example",
    "dns_servers": "dns01.corp.example",
}


def _playbook_dir(cfg: Dict[str, Any]) -> Path:
    return Path(cfg.get("playbook_dir") or settings.ANSIBLE_DIR / "playbooks")


def extravars(ctx: StepContext, state: str) -> Dict[str, Any]:
    keys = ("account_name", "environment", "cidr_size", "region", "onprem_targets", "owners", "cost_center", "data_classification")
    ev: Dict[str, Any] = {k: ctx.spec.get(k) for k in keys}
    ev.update({k: v for k, v in ctx.context.items() if isinstance(v, (str, int, float, bool, list))})
    ev.update({"request_id": ctx.request_id, "temporal_track": ctx.track_id, "temporal_step": ctx.step_id, "state": state})
    if ctx.original_output:
        ev.update({k: v for k, v in ctx.original_output.items() if isinstance(v, (str, int, float, bool, list))})
    return ev


def playbook_tasks(path: Path) -> List[Dict[str, str]]:
    """Flatten a playbook into [{play, hosts, task}] for the mock replay."""
    try:
        plays = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError):
        return []
    out: List[Dict[str, str]] = []

    def walk(tasks: List[Dict[str, Any]], play: str, hosts: str) -> None:
        for t in tasks or []:
            if not isinstance(t, dict):
                continue
            if "block" in t:
                walk(t.get("block", []), play, hosts)
                continue
            out.append({"play": play, "hosts": hosts, "task": t.get("name", "unnamed task")})

    for p in plays if isinstance(plays, list) else []:
        if isinstance(p, dict):
            walk(p.get("tasks", []), p.get("name", "play"), str(p.get("hosts", "localhost")))
    return out


class AnsibleMockConnector(MockConnector):
    """Mock that mirrors the real playbook's tasks."""

    async def execute(self, ctx: StepContext) -> Dict[str, Any]:
        return await self._replay(ctx, "present")

    async def compensate(self, ctx: StepContext) -> Dict[str, Any]:
        await self._replay(ctx, "absent")
        return {"undone": True}

    async def _replay(self, ctx: StepContext, state: str) -> Dict[str, Any]:
        playbook = ctx.params.get("playbook", "")
        tasks = playbook_tasks(_playbook_dir(self.config) / playbook)
        out = self.build_output(ctx) if state == "present" else {}
        lines: List[str] = [f"ansible-playbook {playbook} -e state={state} -e request_id={ctx.request_id}"]
        current_play = None
        hosts = "localhost"
        for t in tasks:
            if t["play"] != current_play:
                current_play = t["play"]
                hosts = SAMPLE_HOSTS.get(t["hosts"], t["hosts"])
                lines.append(f"PLAY [{current_play}]")
            if t["task"] == "Return output to Temporal":
                continue
            verb = "ok" if t["task"].lower().startswith(("gather", "look up", "check", "read", "validate")) else "changed"
            lines.append(f"TASK [{t['task']}] {verb}: [{hosts}]")
        n = max(1, len(tasks))
        lines.append(f"PLAY RECAP {hosts}: ok={n} changed={max(0, n - 1)} failed=0")
        await self.play(ctx, lines)
        return out


class AnsibleRunnerConnector(Connector):
    """Real mode: ansible-runner, events streamed as heartbeats."""

    mode = "real"

    async def execute(self, ctx: StepContext) -> Dict[str, Any]:
        return await self._run(ctx, "present")

    async def compensate(self, ctx: StepContext) -> Dict[str, Any]:
        await self._run(ctx, "absent")
        return {"undone": True}

    async def _run(self, ctx: StepContext, state: str) -> Dict[str, Any]:
        try:
            import ansible_runner
        except ImportError as e:  # pragma: no cover
            raise StepFailed("ansible-runner is not installed on this worker (pip install ansible-runner)", "WorkerMisconfigured", retryable=False) from e

        playbook = ctx.params.get("playbook", "")
        pb_dir = _playbook_dir(self.config)
        inventory = self.config.get("inventory") or str(settings.ANSIBLE_DIR / "inventory.ini")
        events: "queue.Queue[str]" = queue.Queue()
        outputs: Dict[str, Any] = {}
        failures: List[str] = []
        stop = threading.Event()

        def handler(ev: Dict[str, Any]) -> bool:
            data = ev.get("event_data", {})
            kind = ev.get("event", "")
            host = data.get("host", "")
            task = data.get("task", "")
            if kind == "playbook_on_play_start":
                events.put(f"PLAY [{data.get('play', '')}]")
            elif kind == "runner_on_ok":
                res = data.get("res", {}) or {}
                if "temporal_output" in res:
                    outputs.update(res["temporal_output"] or {})
                else:
                    events.put(f"TASK [{task}] {'changed' if res.get('changed') else 'ok'}: [{host}]")
            elif kind == "runner_on_failed" and not data.get("ignore_errors"):
                msg = (data.get("res", {}) or {}).get("msg", "task failed")
                failures.append(f"{task}: {msg}")
                events.put(f"TASK [{task}] FAILED: [{host}] {msg}")
            elif kind == "runner_on_unreachable":
                failures.append(f"{host} unreachable")
                events.put(f"UNREACHABLE: [{host}]")
            return True

        def run() -> Any:
            with tempfile.TemporaryDirectory(prefix="domainos-ansible-") as tmp:
                return ansible_runner.run(
                    private_data_dir=tmp,
                    project_dir=str(pb_dir),
                    playbook=playbook,
                    inventory=inventory,
                    extravars=extravars(ctx, state),
                    event_handler=handler,
                    cancel_callback=stop.is_set,
                    quiet=True,
                )

        ctx.progress(f"ansible-playbook {playbook} -e state={state}")
        fut = asyncio.ensure_future(asyncio.to_thread(run))
        try:
            while not fut.done():
                await ctx.wait(0.5)
                while not events.empty():
                    ctx.progress(events.get_nowait())
        except asyncio.CancelledError:
            stop.set()
            raise
        result = await fut
        while not events.empty():
            ctx.progress(events.get_nowait())
        if result.status != "successful":
            unreachable = any("unreachable" in f for f in failures)
            raise StepFailed(
                "; ".join(failures) or f"Playbook {playbook} ended with status {result.status}",
                "HostUnreachable" if unreachable else "PlaybookFailed",
                retryable=unreachable,
            )
        return outputs
