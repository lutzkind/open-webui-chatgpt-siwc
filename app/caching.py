"""Prompt-cache policy for the SIWC adapter boundary.

The adapter shapes cache-related request fields according to OpenAI's current
prompt-caching contract:

- Explicit cache breakpoints (``prompt_cache_breakpoint``) mark reusable
  prompt-prefix boundaries. For tool loops the documented pattern is to keep
  a breakpoint after the stable head and after tool results, so later rounds
  can reuse the earlier prefix instead of re-billing it as fresh input.
  Breakpoint placement here is deterministic: replaying the same item in a
  later round reproduces its mark, which is required for the cache lookup to
  match.
- ``prompt_cache_key`` optimizes cache routing on models before GPT-5.6.
  GPT-5.6 and later route automatically and use keys only for separate cache
  accounting, so injection is opt-in.

Neither function changes any model-visible content: they only add cache
metadata fields.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# `prompt_cache_key` length: 32 hex characters (128 bits) is comfortably below
# any documented key-size bound while making accidental collisions irrelevant.
CACHE_KEY_HEX_LENGTH = 32

_BREAKPOINT = {"mode": "explicit"}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _anchor_item(body: dict) -> Any:
    """The conversation anchor: the first user-role item.

    Open WebUI (and other clients) may lead the input with a developer or
    system item — the same block for every conversation of one user and
    model. Using it as the anchor would collapse all conversations onto one
    routing key, so prefer the first user message and fall back to the first
    input item only when no user item exists.
    """
    input_items = body.get("input")
    if not isinstance(input_items, list) or not input_items:
        return None
    for item in input_items:
        if isinstance(item, dict) and item.get("role") == "user":
            return item
    return input_items[0]


def build_prompt_cache_key(body: dict) -> str | None:
    """Derive a stable routing key for the conversation this request belongs to."""
    anchor = _anchor_item(body)
    if anchor is None:
        return None
    material = {"model": body.get("model"), "anchor": anchor}
    return hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()[:CACHE_KEY_HEX_LENGTH]


def apply_cache_routing_key(body: dict, enabled: bool) -> tuple[dict, str]:
    """Inject `prompt_cache_key` when the caller did not provide one.

    Returns the (possibly updated) body and the source of the key:
    ``client`` (already present), ``generated``, ``absent`` or ``disabled``.
    """
    if not enabled:
        return body, "disabled"
    if "prompt_cache_key" in body:
        return body, "client"
    key = build_prompt_cache_key(body)
    if key is None:
        return body, "absent"
    return {**body, "prompt_cache_key": key}, "generated"


def _mark_last_text_part(parts: Any) -> bool:
    """Attach an explicit breakpoint to the last text part of a content list."""
    if not isinstance(parts, list):
        return False
    for part in reversed(parts):
        if not isinstance(part, dict):
            continue
        if part.get("type") != "input_text" or not isinstance(part.get("text"), str):
            continue
        if part.get("prompt_cache_breakpoint") == _BREAKPOINT:
            return False
        part["prompt_cache_breakpoint"] = dict(_BREAKPOINT)
        return True
    return False


def apply_cache_breakpoints(body: dict) -> tuple[dict, int]:
    """Mark stable reusable prefix boundaries with explicit cache breakpoints.

    Marks are deterministic and append-stable, so every tool-loop round sends
    the same marks for the same replayed items:

    - the last text block of the first developer/system message (the stable
      head shared by every request), and
    - the last text block of every function-call output item (tool result).

    Returns the updated body and the number of breakpoints added.
    """
    input_items = body.get("input")
    if not isinstance(input_items, list) or not input_items:
        return body, 0

    added = 0
    head_marked = False
    for item in input_items:
        if not isinstance(item, dict):
            continue
        if not head_marked and item.get("role") in {"developer", "system"}:
            if _mark_last_text_part(item.get("content")):
                added += 1
            head_marked = True
        elif item.get("type") == "function_call_output":
            if _mark_last_text_part(item.get("output")):
                added += 1
    return body, added


def apply_cache_policy(body: dict, settings: Any) -> tuple[dict, dict[str, Any]]:
    """Apply the configured cache policy to a sanitized upstream body.

    Returns the (possibly updated) body and a report used by telemetry:
    ``{"routing_key": <source>, "breakpoints_added": <count>}``.
    """
    routing_enabled = bool(getattr(settings, "CACHE_ROUTING_KEY", False))
    breakpoints_enabled = bool(getattr(settings, "CACHE_BREAKPOINTS", False))
    body, routing_source = apply_cache_routing_key(body, routing_enabled)
    breakpoints_added = 0
    if breakpoints_enabled:
        body, breakpoints_added = apply_cache_breakpoints(body)
    return body, {"routing_key": routing_source, "breakpoints_added": breakpoints_added}
