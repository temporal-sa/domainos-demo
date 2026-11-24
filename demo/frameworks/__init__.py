"""Agent runtimes: the same firewall-rule agent, three ways.

    plain      No framework. The workflow is the agent loop; each model turn and
               each tool call is its own activity. (demo/workflows.py)
    langchain  LangChain, fine-grained. ChatAnthropic and the @tool functions run
               inside activities; the workflow still owns the loop. Temporal has
               no official LangChain plugin, so this is the wrapping you write.
    langgraph  LangGraph on Temporal's LangGraph plugin (Public Preview). The
               graph is the agent; each node runs as an activity, and the review
               step is a LangGraph interrupt() resumed from a Workflow Update.

Same prompt, same tools, same offline brain, same review interface, so the
Agent lab compares orchestration and nothing else.

This module stays import-light: workflows import MODES, and nothing here pulls
in LangChain or LangGraph until a worker or the API asks what's installed.
"""

import importlib
import logging
import sys
from functools import lru_cache
from typing import Any, Dict, List, Optional

GRAPH_NAME = "firewall-rules"

MODES: Dict[str, Dict[str, Any]] = {
    "plain": {
        "label": "No framework",
        "workflow": "FirewallRuleAgentWorkflow",
        "summary": "Each model turn and each tool call is its own activity.",
        "boundary": "Every model turn and every tool call",
        "integration": "Temporal SDK only",
        "install": "",
        "requires": [],
    },
    "langchain": {
        "label": "LangChain",
        "workflow": "LangChainAgentWorkflow",
        "summary": "LangChain inside activities (fine-grained): each model turn and each tool call is its own activity.",
        "boundary": "Every model turn and every tool call (you wrap them)",
        "integration": "No official plugin: wrap the model call and each tool in an activity",
        "install": "pip install langchain-anthropic",
        "requires": ["langchain_core", "langchain_anthropic"],
    },
    "langgraph": {
        "label": "LangGraph",
        "workflow": "LangGraphAgentWorkflow",
        "summary": "A LangGraph graph on Temporal's LangGraph plugin: each node runs as an activity.",
        "boundary": "Every graph node",
        "integration": "Temporal LangGraph plugin (Public Preview)",
        "install": "pip install temporalio-langgraph langgraph langchain-anthropic",
        "requires": ["langchain_core", "langchain_anthropic", "langgraph"],
        "preview": True,
    },
}

log = logging.getLogger(__name__)


def plugin_module():
    """Temporal's LangGraph plugin. New package path first, then the older contrib path."""
    try:
        return importlib.import_module("temporalio.langgraph")
    except ImportError:
        return importlib.import_module("temporalio.contrib.langgraph")


@lru_cache(maxsize=None)
def check(mode: str) -> Optional[str]:
    """None if this mode can run in this Python environment, else the reason it can't."""
    meta = MODES.get(mode)
    if meta is None:
        return f"Unknown agent mode '{mode}'"
    if mode == "langgraph" and sys.version_info < (3, 11):
        return "LangGraph mode needs Python 3.11+"
    for mod in meta["requires"]:
        try:
            importlib.import_module(mod)
        except Exception as e:  # noqa: BLE001 - any import problem means "not available"
            return f"{mod} is not installed ({type(e).__name__}). {meta['install']}"
    if mode == "langgraph":
        try:
            mod = plugin_module()
            getattr(mod, "LangGraphPlugin")
            getattr(mod, "graph")
        except Exception as e:  # noqa: BLE001
            return f"Temporal's LangGraph plugin isn't installed ({type(e).__name__}: {e}). {meta['install']}"
    return None


def availability() -> Dict[str, Dict[str, Any]]:
    return {m: {**{k: v for k, v in meta.items() if k != "requires"}, "id": m, "available": check(m) is None, "reason": check(m)}
            for m, meta in MODES.items()}


def worker_extras() -> Dict[str, List[Any]]:
    """Workflows, activities and plugins for every framework mode installed here.

    A mode that fails to load is logged and skipped, so a missing optional
    package never stops the core worker (and the plain demo) from starting.
    """
    out: Dict[str, List[Any]] = {"workflows": [], "activities": [], "plugins": [], "loaded": []}
    if check("langchain") is None:
        try:
            from .langchain_mode import LANGCHAIN_ACTIVITIES
            from .langchain_workflow import LangChainAgentWorkflow

            out["workflows"].append(LangChainAgentWorkflow)
            out["activities"].extend(LANGCHAIN_ACTIVITIES)
            out["loaded"].append("langchain")
        except Exception:  # noqa: BLE001
            log.exception("LangChain mode failed to load; skipping it")
    if check("langgraph") is None:
        try:
            # [snippet:lg-plugin]
            from .langgraph_mode import build_graph
            from .langgraph_workflow import LangGraphAgentWorkflow

            # Register the graph with Temporal's plugin; the worker gets the plugin.
            plugin = plugin_module().LangGraphPlugin(graphs={GRAPH_NAME: build_graph()})
            out["workflows"].append(LangGraphAgentWorkflow)
            out["plugins"].append(plugin)  # -> Worker(..., plugins=[plugin])
            # [/snippet]
            out["loaded"].append("langgraph")
        except Exception:  # noqa: BLE001
            log.exception("LangGraph mode failed to load; skipping it")
    return out
