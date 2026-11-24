"""Feature detection for optional Temporal SDK features.

Activity and child-workflow summaries show up as readable labels in the
Temporal UI timeline. They need a reasonably recent SDK; older SDKs simply run
without them.
"""

import inspect

from temporalio import workflow

ACTIVITY_SUMMARY = "summary" in inspect.signature(workflow.execute_activity).parameters
CHILD_SUMMARY = "static_summary" in inspect.signature(workflow.start_child_workflow).parameters


def activity_kwargs(summary: str) -> dict:
    return {"summary": summary[:200]} if ACTIVITY_SUMMARY else {}


def child_kwargs(summary: str, details: str = "") -> dict:
    if not CHILD_SUMMARY:
        return {}
    out = {"static_summary": summary[:200]}
    if details:
        out["static_details"] = details
    return out
