"""Connector registry: catalog system id -> connector instance (mock or real).

Which connector a system uses comes from config/catalog.yaml (`connector:`),
and whether it is mock or real comes from config/connectors.yaml. Override from
the environment without editing files:

    CONNECTORS_MODE=real              # every system
    CONNECTOR_MODE_AWS_ORGS=real      # one system
"""

import os
from functools import lru_cache
from typing import Any, Dict, List, Tuple, Type

import yaml

from .. import settings
from ..catalog import load_catalog
from .ansible import AnsibleMockConnector, AnsibleRunnerConnector
from .aws import AwsConnector
from .base import Connector, StepContext, StepFailed
from .mock import MockConnector
from .saas import OktaConnector, ServiceNowConnector, SplunkConnector

__all__ = ["get", "describe_all", "Connector", "StepContext", "StepFailed"]

TYPES: Dict[Tuple[str, str], Type[Connector]] = {
    ("aws", "mock"): MockConnector,
    ("aws", "real"): AwsConnector,
    ("ansible", "mock"): AnsibleMockConnector,
    ("ansible", "real"): AnsibleRunnerConnector,
    ("servicenow", "mock"): MockConnector,
    ("servicenow", "real"): ServiceNowConnector,
    ("okta", "mock"): MockConnector,
    ("okta", "real"): OktaConnector,
    ("splunk", "mock"): MockConnector,
    ("splunk", "real"): SplunkConnector,
}


@lru_cache(maxsize=1)
def load_config() -> Dict[str, Any]:
    path = settings.CONFIG_DIR / "connectors.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def mode_for(system: str) -> str:
    env = os.getenv(f"CONNECTOR_MODE_{system.upper()}") or os.getenv("CONNECTORS_MODE")
    if env:
        return env.lower()
    cfg = load_config()
    return str(((cfg.get("systems") or {}).get(system) or {}).get("mode") or (cfg.get("defaults") or {}).get("mode") or "mock").lower()


def connector_type(system: str) -> str:
    sysdef = load_catalog()["systems"].get(system)
    if not sysdef:
        raise StepFailed(f"Unknown system '{system}'", "UnknownSystem", retryable=False)
    return sysdef.get("connector", "mock")


_instances: Dict[str, Connector] = {}


def get(system: str) -> Connector:
    if system not in _instances:
        ctype, mode = connector_type(system), mode_for(system)
        cls = TYPES.get((ctype, mode)) or MockConnector
        section = dict(load_config().get(ctype) or {})
        section.update(((load_config().get("systems") or {}).get(system) or {}).get("options") or {})
        inst = cls(system, section)
        inst.mode = "mock" if cls in (MockConnector, AnsibleMockConnector) else inst.mode
        _instances[system] = inst
    return _instances[system]


def describe_all() -> List[Dict[str, Any]]:
    out = []
    for sid, sysdef in load_catalog()["systems"].items():
        try:
            d = get(sid).describe()
        except Exception as e:  # noqa: BLE001
            d = {"system": sid, "mode": "error", "error": str(e)}
        d.update({"label": sysdef.get("label", sid), "location": sysdef.get("location", "cloud"), "connector": sysdef.get("connector")})
        out.append(d)
    return out
