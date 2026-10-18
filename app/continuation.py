"""Deterministic continuation planning for the Responses WebSocket transport.

The adapter keeps only derived, content-free state per conversation: the
canonical item fingerprints of the last request, the latest upstream response
id, and comparison signatures of the response's output items. Incremental
continuation (``previous_response_id`` plus only the new input items) is used
only when the incoming request is a proven extension of the previous request
for the same conversation: same model, same instructions, same tool
definitions and every earlier input item byte-identical after canonical
serialization.

Item comparison uses the same projection Open WebUI applies when it replays
stored response output items (``type``/``role``/``content`` for messages,
``call_id``/``name``/``arguments`` for tool calls), so a replayed item matches
the item the model produced even though local bookkeeping fields differ.

Nothing in this module stores prompt text, tool arguments or tool results; only
lengths and truncated SHA-256 digests.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

COMPARE_FIELDS: dict[str, tuple[str, ...]] = {
    "message": ("type", "role", "content"),
    "function_call": ("type", "call_id", "name", "arguments"),
    "function_call_output": ("type", "call_id", "output"),
}

CALL_ID_KINDS = frozenset({"function_call", "function_call_output"})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _compare_key(item: dict[str, Any]) -> str:
    fields = COMPARE_FIELDS.get(item.get("type"))
    if fields is None:
        return _sha16(_canonical(item))
    projection = {name: item[name] for name in fields if name in item}
    return _sha16(_canonical(projection))


def _text_digest(item: dict[str, Any]) -> str | None:
    """Digest of the visible text of a message item, ignoring local fields."""
    content = item.get("content")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
    if not parts:
        return None
    return _sha16("\u0000".join(parts))


@dataclass(frozen=True)
class OutputSignature:
    """Comparison signature of one response output item (no content retained)."""

    kind: str
    compare_key: str
    call_id: str | None = None
    text_digest: str | None = None


def signature_from_output_item(item: Any) -> OutputSignature | None:
    if not isinstance(item, dict):
        return None
    kind = item.get("type")
    if not isinstance(kind, str) or not kind:
        return None
    call_id = item.get("call_id") if kind in CALL_ID_KINDS else None
    return OutputSignature(
        kind=kind,
        compare_key=_compare_key(item),
        call_id=call_id if isinstance(call_id, str) and call_id else None,
        text_digest=_text_digest(item) if kind == "message" else None,
    )


def matches_output(item: Any, signature: OutputSignature) -> bool:
    if not isinstance(item, dict) or item.get("type") != signature.kind:
        return False
    if signature.call_id is not None:
        return item.get("call_id") == signature.call_id
    if _compare_key(item) == signature.compare_key:
        return True
    return signature.text_digest is not None and _text_digest(item) == signature.text_digest


def strip_replayed_items(
    delta: list[Any], outputs: tuple[OutputSignature, ...]
) -> tuple[list[Any], int] | None:
    """Remove the leading items the caller replayed from the previous response.

    Returns ``(remaining_items, stripped_count)`` or ``None`` when the replay
    cannot be recognized unambiguously; callers must then send full context.
    """
    if not outputs:
        return delta, 0
    index = 0
    consumed = 0
    while index < len(delta) and consumed < len(outputs):
        match_index = next(
            (
                position
                for position in range(consumed, len(outputs))
                if matches_output(delta[index], outputs[position])
            ),
            None,
        )
        if match_index is None:
            break
        consumed = match_index + 1
        index += 1
    for item in delta[index:]:
        if any(matches_output(item, signature) for signature in outputs[consumed:]):
            # An item outside the contiguous replay prefix still matches a
            # response output: the caller's history is not a clean extension.
            return None
    return delta[index:], index


@dataclass
class LaneState:
    """In-memory, content-free continuity state for one conversation."""

    key: str
    generation: int = 0
    response_id: str | None = None
    model: str | None = None
    instructions_sha: str | None = None
    tools_sha: str | None = None
    item_hashes: tuple[str, ...] = ()
    outputs: tuple[OutputSignature, ...] = ()
    usable: bool = True
    last_used: float = 0.0
    last_mode: str = "full"

    @property
    def item_count(self) -> int:
        return len(self.item_hashes)


@dataclass(frozen=True)
class ContinuationPlan:
    mode: str
    reason: str | None
    input_items: list[Any]
    previous_response_id: str | None
    sent_item_count: int
    full_item_count: int
    stripped_items: int
    sent_bytes: int = 0
    sent_tool_bytes: int = 0

    @property
    def is_continuation(self) -> bool:
        return self.mode == "continuation"


def _measure(items: list[Any]) -> tuple[int, int]:
    """(total canonical bytes, tool-result bytes) for a list of input items."""
    total = 0
    tool = 0
    for item in items:
        size = len(_canonical(item))
        total += size
        if isinstance(item, dict) and isinstance(item.get("type"), str) and item["type"] == "function_call_output":
            tool += size
    return total, tool


def _block_reason(
    *,
    item_hashes: tuple[str, ...],
    model: str | None,
    instructions_sha: str | None,
    tools_sha: str | None,
    lane: LaneState | None,
    generation: int,
    now: float,
    idle_ttl: float,
    instructions_present: bool,
) -> str | None:
    """Why continuation is not allowed for this request, or None when it is."""
    if instructions_present:
        # Continuation frames omit `instructions` (the retained request state
        # already contains them), so requests that set them always resend the
        # full context instead of risking a duplicated or dropped prompt.
        return "instructions_present"
    if lane is None:
        return "no_lane"
    if not lane.usable:
        return "lane_unusable"
    if lane.generation != generation:
        return "connection_changed"
    if not lane.response_id:
        return "no_previous_response"
    if now - lane.last_used > idle_ttl:
        return "lane_idle"
    if lane.model != model:
        return "model_changed"
    if lane.instructions_sha != instructions_sha:
        return "instructions_changed"
    if lane.tools_sha != tools_sha:
        return "tools_changed"
    if len(item_hashes) <= len(lane.item_hashes):
        return "no_new_items"
    for index, digest in enumerate(lane.item_hashes):
        if item_hashes[index] != digest:
            return "prefix_mismatch"
    return None


def full_context_plan(current_items: list[Any], reason: str | None = None) -> ContinuationPlan:
    """A plan that resends the complete canonical caller input."""
    total, tool = _measure(current_items)
    return ContinuationPlan(
        mode="full",
        reason=reason,
        input_items=current_items,
        previous_response_id=None,
        sent_item_count=len(current_items),
        full_item_count=len(current_items),
        stripped_items=0,
        sent_bytes=total,
        sent_tool_bytes=tool,
    )


def continuation_eligible(
    *,
    item_hashes: tuple[str, ...],
    model: str | None,
    instructions_sha: str | None,
    tools_sha: str | None,
    lane: LaneState | None,
    generation: int,
    now: float,
    idle_ttl: float,
    instructions_present: bool = False,
) -> bool:
    """True when the lane's request shape and item prefix allow continuation."""
    return (
        _block_reason(
            item_hashes=item_hashes,
            model=model,
            instructions_sha=instructions_sha,
            tools_sha=tools_sha,
            lane=lane,
            generation=generation,
            now=now,
            idle_ttl=idle_ttl,
            instructions_present=instructions_present,
        )
        is None
    )


def plan_continuation(
    *,
    current_items: list[Any],
    item_hashes: tuple[str, ...],
    model: str | None,
    instructions_sha: str | None,
    tools_sha: str | None,
    lane: LaneState | None,
    generation: int,
    now: float,
    idle_ttl: float,
    instructions_present: bool,
) -> ContinuationPlan:
    """Choose between an incremental continuation and a full-context request.

    Every failure mode falls back to full context; this function never guesses.
    """

    def full(reason: str) -> ContinuationPlan:
        return full_context_plan(current_items, reason)

    reason = _block_reason(
        item_hashes=item_hashes,
        model=model,
        instructions_sha=instructions_sha,
        tools_sha=tools_sha,
        lane=lane,
        generation=generation,
        now=now,
        idle_ttl=idle_ttl,
        instructions_present=instructions_present,
    )
    if reason is not None:
        return full(reason)
    assert lane is not None  # narrowed by _block_reason
    stripped = strip_replayed_items(current_items[len(lane.item_hashes) :], lane.outputs)
    if stripped is None:
        return full("ambiguous_replay")
    remaining, removed = stripped
    if not remaining:
        return full("no_new_items")
    total, tool = _measure(remaining)
    return ContinuationPlan(
        mode="continuation",
        reason=None,
        input_items=remaining,
        previous_response_id=lane.response_id,
        sent_item_count=len(remaining),
        full_item_count=len(current_items),
        stripped_items=removed,
        sent_bytes=total,
        sent_tool_bytes=tool,
    )
