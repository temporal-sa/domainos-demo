"""LangChain message dicts, without importing LangChain.

Workflow state has to be plain JSON, so LangChain-mode conversations are kept
in LangChain's own serialized form (what `messages_to_dict` produces and
`messages_from_dict` reads back):

    {"type": "human" | "ai" | "tool", "data": {"content": ..., ...}}

Workflows build the few messages they need with these helpers, and the offline
brain reads the conversation through `to_anthropic`. Activities do the real
conversion with LangChain itself.
"""

import json
from typing import Any, Dict, List, Optional


def human(text: str) -> Dict[str, Any]:
    return {"type": "human", "data": {"content": text}}


def tool(call_id: str, name: str, content: Any) -> Dict[str, Any]:
    text = content if isinstance(content, str) else json.dumps(content)
    return {"type": "tool", "data": {"content": text, "tool_call_id": call_id, "name": name}}


def ai_from_blocks(blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """An AIMessage dict from Anthropic-style content blocks (the offline brain's output)."""
    text = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
    calls = [{"name": b["name"], "args": b.get("input") or {}, "id": b["id"], "type": "tool_call"}
             for b in blocks if b.get("type") == "tool_use"]
    return {"type": "ai", "data": {"content": text, "tool_calls": calls}}


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content
                        if not isinstance(b, dict) or b.get("type") == "text").strip()
    return ""


def calls(msg: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Tool calls on an AI message dict, as [{"id", "name", "input"}]."""
    if not msg or msg.get("type") != "ai":
        return []
    data = msg.get("data") or {}
    out = [{"id": c["id"], "name": c["name"], "input": c.get("args") or {}} for c in data.get("tool_calls") or []]
    if not out and isinstance(data.get("content"), list):  # older serializations keep them only in content
        out = [{"id": b["id"], "name": b["name"], "input": b.get("input") or {}}
               for b in data["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
    return out


def last_ai(messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return next((m for m in reversed(messages) if m.get("type") == "ai"), None)


def to_anthropic(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """LangChain message dicts -> Anthropic Messages API format (for the offline brain)."""
    out: List[Dict[str, Any]] = []

    def add_user(blocks: List[Dict[str, Any]]) -> None:
        if out and out[-1]["role"] == "user":
            prev = out[-1]["content"]
            out[-1]["content"] = (prev if isinstance(prev, list) else [{"type": "text", "text": prev}]) + blocks
        else:
            out.append({"role": "user", "content": blocks})

    for m in messages:
        kind, data = m.get("type"), m.get("data") or {}
        content = data.get("content", "")
        if kind == "human":
            add_user(content if isinstance(content, list) else [{"type": "text", "text": content}])
        elif kind == "tool":
            add_user([{"type": "tool_result", "tool_use_id": data.get("tool_call_id"), "content": text_of(content) or str(content)}])
        elif kind == "ai":
            blocks: List[Dict[str, Any]] = []
            text = text_of(content)
            if text:
                blocks.append({"type": "text", "text": text})
            blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]} for c in calls(m)]
            out.append({"role": "assistant", "content": blocks})
    # The offline brain reads a plain string first message just like the plain mode's.
    if out and out[0]["role"] == "user" and len(out[0]["content"]) == 1 and out[0]["content"][0].get("type") == "text":
        out[0] = {"role": "user", "content": out[0]["content"][0]["text"]}
    return out
