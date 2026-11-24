"""Demo bugs: real exceptions in real code, reachable only in "bug mode".

Each bug is one line in the source, gated by a switch in the demo controls:

    if bug_mode("vpc_subnets"): raise IndexError(...)  # DEMO-BUG

With bug mode on, the line throws and the step keeps retrying. Comment the
line out and save: the demo redeploys the workers, the next retry runs the
fixed code, and the request carries on from that step. Once the line is
commented out, bug mode no longer matters.

Bugs live in activity code on purpose. Changing workflow code under running
workflows needs versioning, which is a different conversation.
"""

import re
from typing import Any, Dict, Optional

from . import settings
from .chaos import RuntimeStore

BUGS: Dict[str, Dict[str, Any]] = {
    "vpc_subnets": {
        "label": "VPC subnet planning bug",
        "description": "An IndexError in the code that splits the VPC CIDR into subnets. Hits “Create VPC and subnets”.",
        "file": "demo/subnets.py",
        "step": "network.create_vpc",
    },
}

MARKER = "DEMO-BUG"
_store: Optional[RuntimeStore] = None


def bug_mode(bug_id: str) -> bool:
    global _store
    if _store is None:
        _store = RuntimeStore()
    return bool(_store.read().get("bugs", {}).get(bug_id))


def line_status(bug_id: str) -> Dict[str, Any]:
    """Where the bug line is and whether it is live (not commented out) on disk."""
    bug = BUGS[bug_id]
    path = settings.ROOT / bug["file"]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        return {"file": bug["file"], "line": None, "active": False, "error": str(e)}
    pattern = re.compile(rf"bug_mode\(\s*[\"']{re.escape(bug_id)}[\"']\s*\)")
    for i, text in enumerate(lines, start=1):
        if MARKER in text and pattern.search(text):
            return {"file": bug["file"], "line": i, "active": not text.lstrip().startswith("#"), "text": text.strip()}
    return {"file": bug["file"], "line": None, "active": False, "error": "Bug line not found (deleted?)"}
