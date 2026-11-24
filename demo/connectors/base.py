"""Connector interface.

A connector is how a step reaches a real system (AWS, Okta, Ansible, ...).
Every system in the catalog maps to one connector, in `mock` or `real` mode
(see config/connectors.yaml). Workflows never see the difference: they hand a
step to the `run_step` activity, and the activity picks the connector.

To add a system: write a class with `execute()` (and `compensate()` if the step
has an `undo`), register it in connectors/__init__.py, and point a catalog
system at it.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


class StepFailed(Exception):
    """A connector failure with a type name Temporal can match on.

    retryable=False marks a problem that retrying won't fix (quota, bad input).
    """

    def __init__(self, message: str, error_type: str = "StepFailed", retryable: bool = True, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable
        self.details = details or {}


@dataclass
class StepContext:
    request_id: str
    track_id: str
    step_id: str
    step_name: str
    system: str
    system_label: str
    action: str
    params: Dict[str, Any]
    spec: Dict[str, Any]
    context: Dict[str, Any]  # outputs from earlier steps, e.g. account_id
    seconds: float = 3.0  # nominal duration (mock mode)
    speed: float = 1.0
    original_output: Optional[Dict[str, Any]] = None  # set when compensating
    resume: Dict[str, Any] = field(default_factory=dict)  # last heartbeat from a previous attempt
    _progress: Optional[Callable[[Optional[str], Optional[float], Optional[Dict[str, Any]]], None]] = None
    log: List[str] = field(default_factory=list)

    def progress(self, line: Optional[str] = None, pct: Optional[float] = None, checkpoint: Optional[Dict[str, Any]] = None) -> None:
        """Record a log line and heartbeat to Temporal.

        `checkpoint` is saved in the heartbeat; if this attempt dies, the next
        attempt sees it in `resume` (e.g. an AWS CreateAccount request id).
        """
        if line:
            self.log.append(line)
            self.log[:] = self.log[-200:]
        if self._progress:
            self._progress(line, pct, checkpoint)

    async def wait(self, seconds: float) -> None:
        """Sleep while heartbeating, so cancellation and worker loss are noticed."""
        remaining = max(0.0, seconds)
        while remaining > 0:
            chunk = min(0.5, remaining)
            await asyncio.sleep(chunk)
            remaining -= chunk
            self.progress()


class Connector:
    system: str = ""
    mode: str = "mock"

    def __init__(self, system: str, config: Dict[str, Any]):
        self.system = system
        self.config = config

    async def execute(self, ctx: StepContext) -> Dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError

    async def compensate(self, ctx: StepContext) -> Dict[str, Any]:
        ctx.progress(f"No undo defined for {ctx.action}; nothing to roll back")
        return {"undone": False}

    async def verify(self, spec: Dict[str, Any], outputs: Dict[str, Any]) -> List[Dict[str, Any]]:
        return []

    def describe(self) -> Dict[str, Any]:
        return {"system": self.system, "mode": self.mode, "type": type(self).__name__}
