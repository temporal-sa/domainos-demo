"""Live code snippets for the "Under the hood" tab.

Snippets are read straight from the source files between
`# [snippet:<id>]` and `# [/snippet]` markers, so what you see is
the code that's running. `# [mark:<name>]` on a line lets the UI highlight it
while that thing is happening (a Claude turn, a wait for a reviewer, ...).
Notes come from config/code_notes.yaml.
"""

import re
from functools import lru_cache
from typing import Any, Dict, List, Tuple

import yaml

from .. import settings
from ..bugs import BUGS, MARKER, line_status

START = re.compile(r"#\s*\[snippet:([\w-]+)\]")
END = re.compile(r"#\s*\[/snippet\]")
MARK = re.compile(r"\s*#\s*\[mark:([\w-]+)\]")


def _strip(line: str) -> Tuple[str, List[str]]:
    marks = MARK.findall(line)
    return MARK.sub("", line).rstrip(), marks


@lru_cache(maxsize=32)
def _read(path: str, mtime: float) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    lines = open(path, encoding="utf-8").read().splitlines()
    current = None
    for n, raw in enumerate(lines, start=1):
        m = START.search(raw)
        if m:
            current = {"id": m.group(1), "start": n + 1, "lines": []}
            continue
        if current and END.search(raw):
            # trim a common indent so methods don't start halfway across the panel
            body = current["lines"]
            indents = [len(x["text"]) - len(x["text"].lstrip()) for x in body if x["text"].strip()]
            cut = min(indents) if indents else 0
            for x in body:
                x["text"] = x["text"][cut:]
            out[current["id"]] = current
            current = None
            continue
        if current is not None:
            text, marks = _strip(raw)
            if MARKER in raw and "bug_mode(" in raw:
                marks = marks + ["demo-bug"]
            current["lines"].append({"n": n, "text": text, "marks": marks})
    return out


def snippets() -> List[Dict[str, Any]]:
    notes_path = settings.CONFIG_DIR / "code_notes.yaml"
    try:
        notes = yaml.safe_load(notes_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        notes = {}
    out = []
    for sid, note in notes.items():
        rel = note.get("file", "")
        if note.get("code"):  # an illustration: Python written into the notes, not read from a file
            found = {"start": None, "lines": [{"n": i, "text": t, "marks": []} for i, t in enumerate(str(note["code"]).rstrip().splitlines(), 1)]}
            rel = ""
        else:
            path = settings.ROOT / rel
            try:
                found = _read(str(path), path.stat().st_mtime).get(sid)
            except OSError:
                found = None
        out.append({
            "id": sid,
            "title": note.get("title", sid),
            "summary": note.get("summary", ""),
            "points": note.get("points", []),
            "section": note.get("section", "hood"),  # "lab" = shown on the Agent lab tab only
            "mode": note.get("mode"),
            "illustration": bool(note.get("code")),
            "file": rel,
            "start": found["start"] if found else None,
            "lines": found["lines"] if found else [],
            "missing": found is None,
        })
    return out


def bugs_status(store_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    enabled = store_data.get("bugs", {})
    return [{"id": bid, **meta, **line_status(bid), "enabled": bool(enabled.get(bid))} for bid, meta in BUGS.items()]
