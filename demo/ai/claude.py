"""Thin async wrapper around the Anthropic Messages API with JSON structured output.

Client-side retries are disabled on purpose: the calls run inside Temporal
activities, and Temporal owns retries (with backoff, visible in the event
history, and durable across worker crashes).
"""

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from .. import settings


class AIRetryable(Exception):
    """Rate limit, overload, network blip: let Temporal retry."""


class AIFatal(Exception):
    """Bad key, bad request: retrying won't help."""


_client = None


def enabled() -> bool:
    if not settings.ai_configured():
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def _get_client():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.AsyncAnthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            max_retries=0,
            timeout=60.0,
        )
    return _client


async def _create(output_config: Dict[str, Any], **kwargs: Any):
    """messages.create with error mapping. output_config goes through extra_body
    so this works with any recent SDK version, typed support or not."""
    import anthropic

    client = _get_client()
    output_config = dict(output_config)
    if settings.ANTHROPIC_EFFORT:
        output_config["effort"] = settings.ANTHROPIC_EFFORT
    try:
        try:
            return await client.messages.create(model=settings.ANTHROPIC_MODEL, extra_body={"output_config": output_config} if output_config else None, **kwargs)
        except anthropic.BadRequestError as e:
            # Some models don't accept every effort level; retry once without it.
            if "effort" in output_config and "effort" in str(e).lower():
                output_config.pop("effort")
                return await client.messages.create(model=settings.ANTHROPIC_MODEL, extra_body={"output_config": output_config} if output_config else None, **kwargs)
            raise
    except anthropic.AuthenticationError as e:
        raise AIFatal(f"Anthropic authentication failed: {e}") from e
    except anthropic.PermissionDeniedError as e:
        raise AIFatal(f"Anthropic permission denied: {e}") from e
    except anthropic.BadRequestError as e:
        raise AIFatal(f"Anthropic rejected the request: {e}") from e
    except anthropic.NotFoundError as e:
        raise AIFatal(f"Model not found ({settings.ANTHROPIC_MODEL}): {e}") from e
    except anthropic.RateLimitError as e:
        raise AIRetryable(f"Anthropic rate limit: {e}") from e
    except anthropic.APIStatusError as e:
        if e.status_code >= 500 or e.status_code == 429:
            raise AIRetryable(f"Anthropic server error {e.status_code}: {e}") from e
        raise AIFatal(f"Anthropic client error {e.status_code}: {e}") from e
    except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
        raise AIRetryable(f"Could not reach Anthropic: {e}") from e


def _meta(resp: Any, started: float) -> Dict[str, Any]:
    usage = getattr(resp, "usage", None)
    return {
        "source": "claude",
        "model": getattr(resp, "model", settings.ANTHROPIC_MODEL),
        "latency_ms": int((time.monotonic() - started) * 1000),
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
    }


async def structured(system: str, prompt: str, schema: Dict[str, Any], max_tokens: int = 4000) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """One Claude call that must return JSON matching `schema`. Returns (data, metadata)."""
    started = time.monotonic()
    resp = await _create({"format": {"type": "json_schema", "schema": schema}}, max_tokens=max_tokens, system=system,
                         messages=[{"role": "user", "content": prompt}])
    text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise AIRetryable(f"Model returned invalid JSON: {text[:200]}") from e
    return data, _meta(resp, started)


# [snippet:plain-claude-call]
async def tool_turn(system: str, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], max_tokens: int = 4000) -> Dict[str, Any]:
    """One agent turn with tools. Returns the assistant content as plain dicts.

    Content blocks are kept whole (including any thinking blocks and their
    signatures) because they must go back to the API unchanged next turn.
    """
    started = time.monotonic()
    from ..frameworks.common import inject_model_fault  # the Agent lab's injected 429s

    await inject_model_fault("plain")
    resp = await _create({}, max_tokens=max_tokens, system=system, messages=messages, tools=tools)
    content = [b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else dict(b) for b in resp.content]
    return {"content": content, "stop_reason": getattr(resp, "stop_reason", None), **_meta(resp, started)}
# [/snippet]


def heuristic_meta(reason: str, error: Optional[str] = None) -> Dict[str, Any]:
    meta: Dict[str, Any] = {"source": "heuristic", "model": None, "latency_ms": 0, "reason": reason}
    if error:
        meta["error"] = error
    return meta
