"""Loads config/catalog.yaml and turns a request spec into an execution plan.

Pure Python (no Temporal imports) so it can be unit tested on its own. The plan
is built inside an activity, which records the result in workflow history.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from . import settings
from .models import ApprovalSpec, Plan, SkippedItem, StepSpec, TrackSpec


class CatalogError(ValueError):
    pass


@lru_cache(maxsize=4)
def _load(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    validate(data)
    return data


def load_catalog(path: Optional[Path] = None) -> Dict[str, Any]:
    return _load(str(path or settings.CONFIG_DIR / "catalog.yaml"))


def validate(cat: Dict[str, Any]) -> None:
    systems = cat.get("systems", {})
    track_ids = [t["id"] for t in cat.get("tracks", [])]
    if len(set(track_ids)) != len(track_ids):
        raise CatalogError("Duplicate track ids in catalog")
    approval_ids = {a["id"] for a in cat.get("approvals", [])}
    for t in cat.get("tracks", []):
        for dep in t.get("depends_on", []):
            if dep not in track_ids:
                raise CatalogError(f"Track '{t['id']}' depends on unknown track '{dep}'")
        if t.get("approval") and t["approval"] not in approval_ids:
            raise CatalogError(f"Track '{t['id']}' references unknown approval '{t['approval']}'")
        step_ids = [s["id"] for s in t.get("steps", [])]
        if len(set(step_ids)) != len(step_ids):
            raise CatalogError(f"Duplicate step ids in track '{t['id']}'")
        for s in t.get("steps", []):
            if s["system"] not in systems:
                raise CatalogError(f"Step '{t['id']}.{s['id']}' uses unknown system '{s['system']}'")
            if s.get("kind", "action") == "agent" and not s.get("agent"):
                raise CatalogError(f"Agent step '{t['id']}.{s['id']}' needs an 'agent'")
    for a in cat.get("approvals", []):
        if a.get("blocks", "request") not in ("request", *track_ids):
            raise CatalogError(f"Approval '{a['id']}' blocks unknown target '{a.get('blocks')}'")
    _check_cycles(cat.get("tracks", []))


def _check_cycles(tracks: List[Dict[str, Any]]) -> None:
    deps = {t["id"]: list(t.get("depends_on", [])) for t in tracks}
    seen, stack = set(), set()

    def visit(n: str) -> None:
        if n in stack:
            raise CatalogError(f"Dependency cycle through track '{n}'")
        if n in seen:
            return
        stack.add(n)
        for d in deps.get(n, []):
            visit(d)
        stack.discard(n)
        seen.add(n)

    for n in deps:
        visit(n)


def matches(when: Any, spec: Dict[str, Any]) -> bool:
    """Evaluate a catalog `when` condition against a request spec."""
    if when is None:
        return True
    if isinstance(when, bool):
        return when
    if isinstance(when, str):
        negate = when.startswith("!")
        value = bool(spec.get(when.lstrip("!")))
        return not value if negate else value
    if isinstance(when, dict):
        for key, allowed in when.items():
            value = spec.get(key)
            allowed_list = allowed if isinstance(allowed, list) else [allowed]
            norm = [str(a).lower() for a in allowed_list]
            if str(value).lower() not in norm:
                return False
        return True
    if isinstance(when, list):  # any-of
        return any(matches(w, spec) for w in when)
    raise CatalogError(f"Unsupported condition: {when!r}")


def build_plan(spec: Dict[str, Any], catalog: Optional[Dict[str, Any]] = None) -> Plan:
    cat = catalog or load_catalog()
    systems = cat["systems"]
    plan = Plan()

    included: Dict[str, TrackSpec] = {}
    for t in cat["tracks"]:
        if not matches(t.get("when"), spec):
            plan.skipped.append(SkippedItem(t["id"], t["name"], f"Not needed: {t.get('reason') or 'condition not met'}"))
            continue
        steps: List[StepSpec] = []
        for s in t["steps"]:
            if not matches(s.get("when"), spec):
                continue
            sysdef = systems[s["system"]]
            steps.append(
                StepSpec(
                    id=s["id"],
                    name=s["name"],
                    system=s["system"],
                    system_label=sysdef.get("label", s["system"]),
                    location=sysdef.get("location", "cloud"),
                    task_queue=settings.queue_for_location(sysdef.get("location", "cloud")),
                    action=s["action"],
                    params=dict(s.get("params") or {}),
                    seconds=float(s.get("seconds", 3)),
                    undo=s.get("undo"),
                    kind=s.get("kind", "action"),
                    agent=s.get("agent"),
                    reviewer=s.get("reviewer"),
                )
            )
        if not steps:
            plan.skipped.append(SkippedItem(t["id"], t["name"], "No steps apply to this request"))
            continue
        included[t["id"]] = TrackSpec(
            id=t["id"],
            name=t["name"],
            team=t.get("team", ""),
            depends_on=list(t.get("depends_on", [])),
            approval=t.get("approval"),
            reason=t.get("reason", ""),
            steps=steps,
        )

    # Dependencies on tracks that were left out are treated as satisfied.
    for tr in included.values():
        tr.depends_on = [d for d in tr.depends_on if d in included]
    plan.tracks = list(included.values())

    for a in cat.get("approvals", []):
        if not matches(a.get("when"), spec):
            continue
        blocks = a.get("blocks", "request")
        if blocks != "request" and blocks not in included:
            continue  # gates a track this request doesn't need
        plan.approvals.append(
            ApprovalSpec(
                id=a["id"],
                name=a["name"],
                role=a.get("role", ""),
                approver=a.get("approver", ""),
                escalation=a.get("escalation", ""),
                blocks=blocks,
                reason=a.get("reason", ""),
                auto=bool(a.get("auto_when")) and matches(a.get("auto_when"), spec),
                auto_reason=a.get("auto_reason", ""),
            )
        )
    # Approvals that gate a track nobody gates on are dropped above; tracks that
    # reference a dropped approval run ungated.
    approval_ids = {a.id for a in plan.approvals}
    for tr in plan.tracks:
        if tr.approval and tr.approval not in approval_ids:
            tr.approval = None
    return plan


def depth_map(tracks: List[TrackSpec]) -> Dict[str, int]:
    """Column index for each track in the provisioning map (longest path)."""
    by_id = {t.id: t for t in tracks}
    memo: Dict[str, int] = {}

    def depth(tid: str) -> int:
        if tid not in memo:
            deps = [d for d in by_id[tid].depends_on if d in by_id]
            memo[tid] = 0 if not deps else 1 + max(depth(d) for d in deps)
        return memo[tid]

    return {t.id: depth(t.id) for t in tracks}


def fault_presets(catalog: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    cat = catalog or load_catalog()
    return list(cat.get("faults", []))


def find_fault(fault_id: str, catalog: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    return next((f for f in fault_presets(catalog) if f["id"] == fault_id), None)
