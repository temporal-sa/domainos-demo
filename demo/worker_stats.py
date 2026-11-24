"""Per-worker activity tracker.

Each worker process writes runtime/workers/<identity>.json twice a second:
what it is running right now (with the last few log lines) and how many tasks
it has finished. The demo UI reads these files to show which worker is doing
what, so killing the worker that is mid-"Create VPC" is a deliberate click.
"""

import asyncio
import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Optional

from . import settings

_state: Dict[str, Any] = {
    "identity": os.getenv("DEMO_WORKER_ID", ""),
    "pool": os.getenv("DEMO_WORKER_POOL", ""),
    "pid": os.getpid(),
    "started_at": time.time(),
    "completed": 0,
    "failed": 0,
}
_current: Dict[str, Dict[str, Any]] = {}
_recent: Deque[Dict[str, Any]] = deque(maxlen=8)
_path: Optional[Path] = None


def configure(identity: str, pool: str) -> None:
    global _path
    _state.update({"identity": identity, "pool": pool, "pid": os.getpid(), "started_at": time.time()})
    d = settings.RUNTIME_DIR / "workers"
    d.mkdir(parents=True, exist_ok=True)
    _path = d / f"{identity}.json"


def begin(key: str, meta: Dict[str, Any]) -> str:
    _current[key] = {**meta, "since": time.time(), "log": [], "pct": 0}
    return key


def progress(key: str, line: Optional[str], pct: Optional[float]) -> None:
    cur = _current.get(key)
    if not cur:
        return
    if line:
        cur["log"] = (cur["log"] + [line])[-6:]
    if pct is not None:
        cur["pct"] = pct


def end(key: str, ok: bool, error: Optional[str] = None) -> None:
    cur = _current.pop(key, None)
    _state["completed" if ok else "failed"] += 1
    if cur:
        _recent.appendleft({"label": cur.get("label"), "ok": ok, "error": error, "at": time.time(), "attempt": cur.get("attempt")})


def snapshot() -> Dict[str, Any]:
    return {**_state, "current": list(_current.values()), "recent": list(_recent), "updated_at": time.time()}


def write_once() -> None:
    if not _path:
        return
    tmp = _path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot()), encoding="utf-8")
    os.replace(tmp, _path)


async def writer_loop(interval: float = 0.5) -> None:
    while True:
        try:
            write_once()
        except OSError:
            pass
        await asyncio.sleep(interval)
