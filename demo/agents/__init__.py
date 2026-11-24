"""Agents the workflows can run.

Each agent module defines its prompt, tool schemas, tool implementations and a
deterministic stand-in "brain" for runs without an API key. The agent loop
itself lives in a workflow (see FirewallRuleAgentWorkflow), which is the
"no framework" mode: every model turn and every tool call is its own Temporal
activity.

LangChain and LangGraph runtimes can be added later as other modes that reuse
the same prompt and tools, so the comparison stays like for like.
"""

from types import ModuleType
from typing import Dict

from . import firewall

AGENTS: Dict[str, ModuleType] = {"firewall_rules": firewall}


def get(agent_id: str) -> ModuleType:
    if agent_id not in AGENTS:
        raise KeyError(f"Unknown agent '{agent_id}'")
    return AGENTS[agent_id]
