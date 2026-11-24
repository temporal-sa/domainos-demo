"""Runtime demo settings and failure injection, shared across processes.

The API writes `runtime/settings.json`; worker processes read it on every
activity attempt, so changes in the UI take effect immediately, including on
steps that are already running.

This is a file because every process runs on one machine in the demo. To run
workers elsewhere (Kubernetes, ECS), swap RuntimeStore for Redis or an HTTP call
to the API; nothing else needs to change.
"""

import json
import os
import random
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import settings

try:
    import fcntl  # POSIX only

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - Windows
    _HAS_FCNTL = False


DEFAULTS: Dict[str, Any] = {
    "speed": 1.0,  # multiplier on mock step duration (0.5 = twice as fast)
    "approvals": {"auto_approve_seconds": 0, "sla_seconds": 60},
    "retries": {"max_attempts": 6, "heartbeat_seconds": 8},
    "systems": {},  # system id -> {"error_rate": 0..1, "latency_ms": int}
    "faults": [],  # armed faults
    "bugs": {},  # bug id -> bool (see demo/bugs.py)
    "deploy": {"auto": True},  # redeploy workers when source files change
    "agent_mode": "plain",  # firewall agent runtime for new requests (see demo/frameworks)
    "lab": {},  # current Agent lab run group
}


class InjectedFault(Exception):
    """Raised by `apply()`; activities translate it into a Temporal error."""

    def __init__(self, message: str, error_type: str, retryable: bool, fault_id: str = ""):
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable
        self.fault_id = fault_id


class RuntimeStore:
    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root or settings.RUNTIME_DIR)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "settings.json"
        self.lock_path = self.root / ".settings.lock"
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_mtime = 0.0

    @contextmanager
    def _locked(self):
        if not _HAS_FCNTL:
            yield
            return
        with open(self.lock_path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _read_unlocked(self) -> Dict[str, Any]:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return _merge_defaults({})
        if self._cache is not None and mtime == self._cache_mtime:
            return json.loads(json.dumps(self._cache))
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, OSError):
            data = {}
        data = _merge_defaults(data)
        self._cache, self._cache_mtime = data, mtime
        return json.loads(json.dumps(data))

    def _write_unlocked(self, data: Dict[str, Any]) -> None:
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        self._cache = None

    def read(self) -> Dict[str, Any]:
        return self._read_unlocked()

    def update(self, fn: Callable[[Dict[str, Any]], Any]) -> Dict[str, Any]:
        with self._locked():
            data = self._read_unlocked()
            fn(data)
            self._write_unlocked(data)
            return data

    # --- convenience mutations used by the API ---------------------------
    def arm_fault(self, preset: Dict[str, Any]) -> Dict[str, Any]:
        def _arm(d: Dict[str, Any]) -> None:
            d["faults"] = [f for f in d["faults"] if f.get("preset") != preset["id"]]
            d["faults"].append(
                {
                    "id": uuid.uuid4().hex[:8],
                    "preset": preset["id"],
                    "label": preset.get("label", preset["id"]),
                    "kind": preset.get("kind", "hard"),
                    "target": preset["target"],
                    "error_type": preset.get("error_type", "InjectedFailure"),
                    "message": preset.get("message", "Injected failure"),
                    "remaining": int(preset.get("times", 1)),
                    "armed_at": time.time(),
                }
            )

        return self.update(_arm)

    def disarm_fault(self, preset_id: str) -> Dict[str, Any]:
        return self.update(lambda d: d.__setitem__("faults", [f for f in d["faults"] if f.get("preset") != preset_id]))

    def set_system(self, system: str, error_rate: Optional[float] = None, latency_ms: Optional[int] = None) -> Dict[str, Any]:
        def _set(d: Dict[str, Any]) -> None:
            cur = d["systems"].setdefault(system, {"error_rate": 0.0, "latency_ms": 0})
            if error_rate is not None:
                cur["error_rate"] = max(0.0, min(1.0, float(error_rate)))
            if latency_ms is not None:
                cur["latency_ms"] = max(0, int(latency_ms))

        return self.update(_set)

    # --- used by activities ----------------------------------------------
    def consume_fault(self, track_id: str, step_id: str, system: str) -> Optional[Dict[str, Any]]:
        """Return (and use up one charge of) the first armed fault matching this step."""
        targets = {f"{track_id}.{step_id}", f"system:{system}"}
        hit: Dict[str, Any] = {}

        def _consume(d: Dict[str, Any]) -> None:
            for f in d["faults"]:
                if f.get("target") in targets and int(f.get("remaining", 0)) > 0:
                    f["remaining"] = int(f["remaining"]) - 1
                    hit.update(f)
                    break
            d["faults"] = [f for f in d["faults"] if int(f.get("remaining", 0)) > 0]

        if not any(f.get("target") in targets for f in self.read()["faults"]):
            return None  # fast path: no lock when nothing is armed for this step
        self.update(_consume)
        return hit or None


def _merge_defaults(data: Dict[str, Any]) -> Dict[str, Any]:
    out = json.loads(json.dumps(DEFAULTS))
    for k, v in data.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def apply(store: RuntimeStore, track_id: str, step_id: str, system: str, rng: Optional[random.Random] = None) -> float:
    """Raise InjectedFault if a fault fires for this attempt.

    Returns extra latency (seconds) to add for this system.
    """
    rng = rng or random
    fault = store.consume_fault(track_id, step_id, system)
    if fault:
        raise InjectedFault(
            fault["message"],
            fault.get("error_type", "InjectedFailure"),
            retryable=fault.get("kind") == "transient",
            fault_id=fault.get("preset", ""),
        )
    cfg = store.read()["systems"].get(system, {})
    rate = float(cfg.get("error_rate", 0) or 0)
    if rate > 0 and rng.random() < rate:
        raise InjectedFault(
            f"Transient error calling {system}: connection reset by peer",
            "TransientUpstreamError",
            retryable=True,
            fault_id="flaky",
        )
    return float(cfg.get("latency_ms", 0) or 0) / 1000.0
