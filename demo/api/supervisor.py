"""Starts, stops and kills worker processes for the demo controls.

"Kill" sends SIGKILL: no graceful shutdown, no chance to report back, exactly
like a pod being OOM-killed or a VM disappearing. Temporal notices through the
missed heartbeat and hands the work to another worker.
"Stop" sends SIGTERM: the worker finishes in-flight tasks and exits cleanly.

Deploys: every worker records the version (a hash) of the code it started
with. When the code on disk changes, `deploy()` checks it compiles, starts a
replacement for each worker on old code, and stops the old worker only once
its replacement is up. If the new code won't start, the old workers keep
running and the deploy reports the error.
"""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import settings

POOLS = {
    "core": {
        "label": "Core workers",
        "description": "Workflows, Claude, AWS, Okta, ServiceNow",
        "queue": settings.CORE_QUEUE,
        "where": "Cloud or Kubernetes",
    },
    "onprem": {
        "label": "On-prem workers",
        "description": "Ansible: AD, Infoblox, Panorama, DNS; Splunk",
        "queue": settings.ONPREM_QUEUE,
        "where": "Inside the data center, outbound only",
    },
}


class WorkerSupervisor:
    def __init__(self, managed: bool = settings.MANAGE_WORKERS):
        self.managed = managed
        self.procs: Dict[str, Dict[str, Any]] = {}
        self.counter = {p: 0 for p in POOLS}
        self.stats_dir = settings.RUNTIME_DIR / "workers"
        self.log_dir = settings.RUNTIME_DIR / "logs"
        self.stats_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        for f in self.stats_dir.glob("*.json"):  # stale files from a previous run
            f.unlink(missing_ok=True)
        self.deployed_version = code_version()
        self.rollouts: List[Dict[str, Any]] = []
        self.last_deploy: Optional[Dict[str, Any]] = None
        self._seen_version = (self.deployed_version, time.time())

    # --- lifecycle -------------------------------------------------------------
    def spawn(self, pool: str) -> str:
        if pool not in POOLS:
            raise ValueError(f"Unknown pool '{pool}'")
        if not self.managed:
            raise RuntimeError("Worker management is off (DEMO_MANAGE_WORKERS=false)")
        self.counter[pool] += 1
        wid = f"{pool}-{self.counter[pool]}"
        log = open(self.log_dir / f"{wid}.log", "ab")
        env = {**os.environ, "DEMO_WORKER_ID": wid, "DEMO_WORKER_POOL": pool, "PYTHONUNBUFFERED": "1"}
        proc = subprocess.Popen(
            [sys.executable, "-m", "demo.worker", "--pool", pool, "--identity", wid],
            cwd=str(settings.ROOT),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.procs[wid] = {"id": wid, "pool": pool, "proc": proc, "pid": proc.pid, "started_at": time.time(),
                           "status": "starting", "exit_code": None, "ended_at": None, "log": log,
                           "version": code_version()}
        return wid

    def kill(self, wid: str, graceful: bool = False) -> None:
        w = self.procs.get(wid)
        if not w or w["proc"].poll() is not None:
            raise KeyError(f"No running worker '{wid}'")
        if graceful:
            w["proc"].send_signal(signal.SIGTERM)
            w["status"] = "stopping"
        else:
            w["proc"].send_signal(signal.SIGKILL)
            w["status"] = "killed"
            w["ended_at"] = time.time()

    def scale(self, pool: str, count: int) -> List[str]:
        alive = [w for w in self.procs.values() if w["pool"] == pool and w["proc"].poll() is None and w["status"] != "stopping"]
        changed = []
        count = max(0, min(8, count))
        while len(alive) < count:
            changed.append(self.spawn(pool))
            alive.append(self.procs[changed[-1]])
        for w in sorted(alive, key=lambda w: w["started_at"], reverse=True)[: max(0, len(alive) - count)]:
            self.kill(w["id"], graceful=True)
            changed.append(w["id"])
        return changed

    def shutdown(self) -> None:
        for w in self.procs.values():
            if w["proc"].poll() is None:
                w["proc"].send_signal(signal.SIGTERM)
        deadline = time.time() + 5
        for w in self.procs.values():
            try:
                w["proc"].wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                w["proc"].kill()

    # --- status ----------------------------------------------------------------
    def _stats(self, wid: str) -> Optional[Dict[str, Any]]:
        p = self.stats_dir / f"{wid}.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def refresh(self) -> None:
        now = time.time()
        for w in self.procs.values():
            code = w["proc"].poll()
            if code is None:
                stats = self._stats(w["id"])
                if w["status"] in ("starting", "running", "unresponsive"):
                    if stats and stats.get("pid") == w["pid"]:
                        w["status"] = "running" if now - stats.get("updated_at", 0) < 3 else "unresponsive"
                continue
            if w["exit_code"] is None:
                w["exit_code"] = code
                w["ended_at"] = w["ended_at"] or now
                if w["status"] != "killed":
                    w["status"] = "stopped" if code == 0 else ("killed" if code == -9 else "crashed")
                try:
                    w["log"].close()
                except Exception:  # noqa: BLE001
                    pass
        # Forget dead workers after a while so the panel stays tidy.
        for wid in [k for k, w in self.procs.items() if w["ended_at"] and now - w["ended_at"] > 45]:
            self.procs.pop(wid, None)

    def list(self) -> List[Dict[str, Any]]:
        self.refresh()
        out = []
        for w in sorted(self.procs.values(), key=lambda w: (w["pool"], w["started_at"])):
            stats = self._stats(w["id"]) if w["proc"].poll() is None else None
            out.append({
                "version": w.get("version"),
                "replacing": next((r["old"] for r in self.rollouts if r["new"] == w["id"]), None),
                "id": w["id"], "pool": w["pool"], "pid": w["pid"], "status": w["status"], "exit_code": w["exit_code"],
                "started_at": w["started_at"], "ended_at": w["ended_at"],
                "current": (stats or {}).get("current", []),
                "completed": (stats or {}).get("completed", 0),
                "failed": (stats or {}).get("failed", 0),
                "log_tail": self.log_tail(w["id"]) if w["status"] == "crashed" else None,
            })
        return out

    def log_tail(self, wid: str, lines: int = 6) -> str:
        p: Path = self.log_dir / f"{wid}.log"
        try:
            return "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
        except OSError:
            return ""

    def current_by_step(self) -> Dict[str, Dict[str, Any]]:
        """(workflow_id, step) -> live activity info from whichever worker runs it."""
        out: Dict[str, Dict[str, Any]] = {}
        for w in self.list():
            for c in w["current"]:
                if c.get("step"):
                    out[f"{c.get('workflow_id')}|{c.get('step')}"] = {**c, "worker": w["id"]}
        return out

    # --- deploys -----------------------------------------------------------------
    def deploy(self, reason: str = "manual") -> Dict[str, Any]:
        """Rolling restart of every worker running code older than what's on disk."""
        version = code_version()
        errors = compile_errors()
        if errors:
            self.last_deploy = {"at": time.time(), "version": version, "state": "failed", "reason": reason,
                                "error": errors[0], "replaced": 0}
            return self.last_deploy
        stale = [w for w in self.procs.values()
                 if w["proc"].poll() is None and w["status"] in ("running", "starting", "unresponsive")
                 and w.get("version") != version and not any(r["old"] == w["id"] for r in self.rollouts)]
        for w in stale:
            new_id = self.spawn(w["pool"])
            self.rollouts.append({"old": w["id"], "new": new_id, "pool": w["pool"], "started": time.time()})
        self.deployed_version = version
        self.last_deploy = {"at": time.time(), "version": version, "state": "rolling" if stale else "done",
                            "reason": reason, "error": None, "replaced": len(stale)}
        return self.last_deploy

    def tick(self, auto: bool) -> None:
        """Called about once a second: advance rollouts, and auto-deploy on file changes."""
        self.refresh()
        for r in list(self.rollouts):
            new, old = self.procs.get(r["new"]), self.procs.get(r["old"])
            if new and new["status"] == "running":
                if old and old["proc"].poll() is None:
                    self.kill(r["old"], graceful=True)
                self.rollouts.remove(r)
            elif not new or new["status"] in ("crashed", "stopped", "killed") or time.time() - r["started"] > 45:
                self.rollouts.remove(r)
                tail = self.log_tail(r["new"], 4) if new else ""
                self.last_deploy = {**(self.last_deploy or {}), "state": "failed",
                                    "error": f"{r['new']} did not start; {r['old']} kept running. {tail}".strip()}
        if self.last_deploy and self.last_deploy.get("state") == "rolling" and not self.rollouts:
            self.last_deploy["state"] = "done"
        if not auto or not self.managed:
            return
        version = code_version()
        if version != self._seen_version[0]:
            self._seen_version = (version, time.time())  # wait for the editor to finish saving
            return
        if version != self.deployed_version and time.time() - self._seen_version[1] > 1.5:
            self.deploy("code changed on disk")

    def status(self) -> Dict[str, Any]:
        return {"disk_version": code_version(), "deployed_version": self.deployed_version,
                "last_deploy": self.last_deploy, "rollouts": list(self.rollouts)}


# Files workers load. The API's own files and the UI are left out on purpose:
# editing them shouldn't restart workers.
def _worker_sources() -> List[Path]:
    root = settings.ROOT / "demo"
    files = [p for p in root.rglob("*.py") if "api" not in p.relative_to(root).parts]
    files += [p for p in settings.CONFIG_DIR.glob("*.yaml") if p.name != "code_notes.yaml"]
    return sorted(files)


def code_version() -> str:
    h = hashlib.sha256()
    for p in _worker_sources():
        try:
            h.update(str(p.relative_to(settings.ROOT)).encode())
            h.update(p.read_bytes())
        except (OSError, ValueError):
            continue
    return h.hexdigest()[:7]


def compile_errors() -> List[str]:
    errors = []
    for p in _worker_sources():
        if p.suffix != ".py":
            continue
        try:
            compile(p.read_text(encoding="utf-8"), str(p), "exec")
        except SyntaxError as e:
            errors.append(f"{p.relative_to(settings.ROOT)} line {e.lineno}: {e.msg}")
        except (OSError, ValueError) as e:
            errors.append(f"{p.relative_to(settings.ROOT)}: {e}")
    return errors
