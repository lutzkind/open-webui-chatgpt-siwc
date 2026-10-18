"""Safe, content-free request telemetry for the SIWC adapter.

The adapter runs an OpenAI-compatible Responses boundary. To audit ChatGPT
plan usage (tool-loop request counts, prompt-cache reuse, request prefixes)
without ever logging conversation content, this module records derived
per-request metrics only:

- sizes, item counts and structural fingerprints (truncated SHA-256) of the
  instructions block, the tool definitions and every `input` item,
- a structural comparison against recent requests (longest common item
  prefix, first difference position, strict-extension flag),
- upstream response metadata: status, duration, response ID and usage
  counters (input/output/reasoning/cached/cache-write tokens) parsed from the
  SIWC SSE stream,
- non-secret rate-limit headers when present.

Raw prompts, assistant content, tool arguments and tool results are never
read into the log record: only their lengths and hashes. Response IDs are
opaque server identifiers.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import secrets
import sys
import time
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

TELEMETRY_LOGGER_NAME = "siwc.telemetry"

MAX_RECENT_REQUESTS = 32
MAX_SSE_LINE_BYTES = 1 << 21
MAX_IDENTIFIER_LENGTH = 64

# `prompt_cache_key` length: 32 hex characters (128 bits) is comfortably below
# any documented key-size bound while making accidental collisions irrelevant.
CACHE_KEY_HEX_LENGTH = 32

INTERESTING_EVENT_MARKERS = (
    b'"response.created"',
    b'"response.completed"',
    b'"response.incomplete"',
    b'"response.failed"',
    b'"response.cancelled"',
    b'"error"',
)

TERMINAL_EVENT_TYPES = frozenset(
    {"response.completed", "response.incomplete", "response.failed", "response.cancelled"}
)

# Only counters and opaque identifiers; never headers that can carry
# credentials, cookies or user data.
SAFE_RESPONSE_HEADERS = (
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-tokens",
    "retry-after",
)

_LOGGING_CONFIGURED = False
_REQUEST_SEQUENCE = itertools.count(1)


def setup_logging() -> None:
    """Attach a stdout handler to the telemetry logger exactly once."""
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    logger = logging.getLogger(TELEMETRY_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.propagate = False
    _LOGGING_CONFIGURED = True


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _safe_identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if not value or len(value) > MAX_IDENTIFIER_LENGTH or not value.isascii():
        return None
    if not all(char.isalnum() or char in "_-.:" for char in value):
        return None
    return value


def _safe_status(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if not value.isascii() or len(value) > 32:
        return None
    return value


def _item_kind(item: Any) -> str:
    if not isinstance(item, dict):
        return "unknown"
    item_type = item.get("type")
    role = item.get("role")
    if isinstance(item_type, str) and item_type:
        if item_type == "message" and isinstance(role, str) and role:
            return f"message:{role}"
        return item_type
    if isinstance(role, str) and role:
        return f"message:{role}"
    return "item"


@dataclass(frozen=True)
class ItemFingerprint:
    kind: str
    size_bytes: int
    sha16: str

    def as_list(self) -> list[Any]:
        return [self.kind, self.size_bytes, self.sha16]


@dataclass(frozen=True)
class RequestFingerprint:
    req_id: str
    model: str | None
    reasoning_effort: str | None
    instructions_bytes: int
    instructions_sha16: str | None
    tools_count: int
    tools_bytes: int
    tools_sha16: str | None
    items: tuple[ItemFingerprint, ...]
    input_bytes: int
    body_bytes: int

    @property
    def item_count(self) -> int:
        return len(self.items)

    def exact_kind_bytes(self, *kinds: str) -> int:
        return sum(item.size_bytes for item in self.items if item.kind in kinds)

    def kind_prefix_bytes(self, prefix: str) -> int:
        return sum(item.size_bytes for item in self.items if item.kind.startswith(prefix))


def _reasoning_effort(body: dict) -> str | None:
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str):
        return reasoning["effort"][:24]
    flat = body.get("reasoning_effort")
    if isinstance(flat, str):
        return flat[:24]
    return None


def fingerprint_request(req_id: str, body: dict) -> RequestFingerprint:
    raw_input = body.get("input")
    input_items = raw_input if isinstance(raw_input, list) else []
    items: list[ItemFingerprint] = []
    for item in input_items:
        text = _canonical(item)
        items.append(ItemFingerprint(kind=_item_kind(item), size_bytes=len(text), sha16=_sha16(text)))

    instructions = body.get("instructions")
    instructions_text = instructions if isinstance(instructions, str) else None

    raw_tools = body.get("tools")
    tools = raw_tools if isinstance(raw_tools, list) and raw_tools else None
    tools_text = _canonical(tools) if tools is not None else None

    model = body.get("model")
    return RequestFingerprint(
        req_id=req_id,
        model=model if isinstance(model, str) else None,
        reasoning_effort=_reasoning_effort(body),
        instructions_bytes=len(instructions_text) if instructions_text else 0,
        instructions_sha16=_sha16(instructions_text) if instructions_text else None,
        tools_count=len(tools) if tools is not None else 0,
        tools_bytes=len(tools_text) if tools_text else 0,
        tools_sha16=_sha16(tools_text) if tools_text else None,
        items=tuple(items),
        input_bytes=len(_canonical(input_items)),
        body_bytes=len(_canonical(body)),
    )


def build_prompt_cache_key(body: dict) -> str | None:
    """Derive a stable routing key for the conversation this request belongs to.

    Prompt caching on the Responses API keys on the longest common request
    prefix. A key derived from the *anchor* of the conversation (the model and
    the first input item) is identical for every tool-loop round of one visible
    conversation and different across conversations, so the backend can route
    continuations of the same prefix to the machine that cached it. The key is
    an opaque digest; no conversation content is readable from it.
    """
    input_items = body.get("input")
    if not isinstance(input_items, list) or not input_items:
        return None
    anchor = {"model": body.get("model"), "first_input_item": input_items[0]}
    return hashlib.sha256(_canonical(anchor).encode("utf-8")).hexdigest()[:CACHE_KEY_HEX_LENGTH]


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


def _extract_usage(usage: Any) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {}
    extracted: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            extracted[key] = int(value)
    input_details = usage.get("input_tokens_details")
    if isinstance(input_details, dict):
        for key in ("cached_tokens", "cache_write_tokens"):
            value = input_details.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                extracted[key] = int(value)
    output_details = usage.get("output_tokens_details")
    if isinstance(output_details, dict):
        value = output_details.get("reasoning_tokens")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            extracted["reasoning_tokens"] = int(value)
    return extracted


class StreamObserver:
    """Extract response identifiers and usage counters from relayed SSE bytes.

    The observer only JSON-parses lines that contain one of a small set of
    event-type markers; every other byte is dropped. It never retains or logs
    message content, tool arguments or tool results.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.response_id: str | None = None
        self.status: str | None = None
        self.terminal_event: str | None = None
        self.usage: dict[str, int] = {}
        self.error_code: str | None = None
        self.error_param: str | None = None
        self.error_type: str | None = None

    def feed(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)
        newline = self._buffer.find(b"\n")
        while newline >= 0:
            line = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            self.observe_line(line)
            newline = self._buffer.find(b"\n")
        if len(self._buffer) > MAX_SSE_LINE_BYTES:
            self._buffer.clear()

    def observe_line(self, line: str | bytes) -> None:
        if isinstance(line, str):
            line = line.encode("utf-8", "surrogatepass")
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            return
        if not any(marker in payload for marker in INTERESTING_EVENT_MARKERS):
            return
        try:
            event = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if isinstance(event, dict):
            self._observe_event(event)

    def _observe_event(self, event: dict) -> None:
        event_type = event.get("type")
        if event_type == "error":
            error = event.get("error")
            if not isinstance(error, dict):
                error = event
            self.note_error_object(error)
            return
        if event_type not in TERMINAL_EVENT_TYPES and event_type != "response.created":
            return
        response = event.get("response")
        if isinstance(response, dict):
            if self.response_id is None:
                self.response_id = _safe_identifier(response.get("id"))
            status = _safe_status(response.get("status"))
            if status:
                self.status = status
            usage = _extract_usage(response.get("usage"))
            if usage:
                self.usage.update(usage)
            error = response.get("error")
            if isinstance(error, dict):
                self.note_error_object(error)
        if event_type in TERMINAL_EVENT_TYPES:
            self.terminal_event = event_type

    def note_error_object(self, error: dict) -> None:
        code = _safe_identifier(error.get("code"))
        if code and self.error_code is None:
            self.error_code = code
        param = _safe_identifier(error.get("param"))
        if param and self.error_param is None:
            self.error_param = param
        error_type = _safe_identifier(error.get("type"))
        if error_type and self.error_type is None:
            self.error_type = error_type


def _common_prefix(
    recent: RequestFingerprint, current: RequestFingerprint
) -> tuple[int, int, bool]:
    """Return (common item count, common bytes, strict extension flag)."""
    common = 0
    common_bytes = 0
    limit = min(recent.item_count, current.item_count)
    for index in range(limit):
        if recent.items[index].sha16 != current.items[index].sha16:
            break
        common += 1
        common_bytes += recent.items[index].size_bytes
    is_extension = (
        common == recent.item_count
        and current.item_count >= recent.item_count
        and recent.instructions_sha16 == current.instructions_sha16
        and recent.tools_sha16 == current.tools_sha16
    )
    return common, common_bytes, is_extension


class RecentRequests:
    """Bounded in-memory view of recent request fingerprints (no content)."""

    def __init__(self, maxlen: int = MAX_RECENT_REQUESTS) -> None:
        self._entries: deque[RequestFingerprint] = deque(maxlen=maxlen)

    def add(self, fingerprint: RequestFingerprint) -> None:
        self._entries.append(fingerprint)

    def compare(self, fingerprint: RequestFingerprint) -> dict[str, Any]:
        previous = self._entries[-1] if self._entries else None
        comparison: dict[str, Any] = {}
        if previous is not None and previous.req_id != fingerprint.req_id:
            comparison["previous"] = _comparison_entry(previous, fingerprint)
        best: RequestFingerprint | None = None
        best_common = -1
        for entry in reversed(self._entries):
            if entry.req_id == fingerprint.req_id:
                continue
            common, _, _ = _common_prefix(entry, fingerprint)
            if common > best_common:
                best = entry
                best_common = common
        if best is not None and (previous is None or best.req_id != previous.req_id):
            comparison["best"] = _comparison_entry(best, fingerprint)
        return comparison


def _comparison_entry(recent: RequestFingerprint, current: RequestFingerprint) -> dict[str, Any]:
    common, common_bytes, is_extension = _common_prefix(recent, current)
    return {
        "req_id": recent.req_id,
        "common_items": common,
        "common_bytes": common_bytes,
        "first_diff": common if common < min(recent.item_count, current.item_count) else None,
        "items": recent.item_count,
        "instructions_same": recent.instructions_sha16 == current.instructions_sha16,
        "tools_same": recent.tools_sha16 == current.tools_sha16,
        "extension": is_extension,
    }


class RequestTelemetry:
    """Per-request lifecycle: fingerprint, upstream metadata, one log record."""

    def __init__(
        self,
        settings: Any,
        body: dict,
        cache_key_source: str,
        recent: RecentRequests,
    ) -> None:
        self.enabled = bool(getattr(settings, "REQUEST_TELEMETRY", True))
        self.recent = recent
        self.cache_key_source = cache_key_source
        self.started = time.monotonic()
        self.req_id = f"r-{secrets.token_hex(6)}"
        self.seq = next(_REQUEST_SEQUENCE)
        self._started_at = datetime.now(timezone.utc)
        self.fingerprint = fingerprint_request(self.req_id, body)
        self.observer = StreamObserver()
        self.upstream_dispatched = False
        self.upstream_status: int | None = None
        self.rate_limit: dict[str, str] = {}
        self.stream_requested: bool | None = None
        self.error: str | None = None
        self._finished = False

    def note_stream_requested(self, requested: bool) -> None:
        self.stream_requested = requested

    def note_upstream_status(self, status_code: int, headers: Any = None) -> None:
        self.upstream_dispatched = True
        self.upstream_status = status_code
        if isinstance(headers, Mapping):
            for name in SAFE_RESPONSE_HEADERS:
                value = headers.get(name)
                if isinstance(value, str) and value.isascii() and len(value) <= 64:
                    self.rate_limit[name] = value

    def observe_chunk(self, chunk: bytes) -> None:
        self.observer.feed(chunk)

    def observe_line(self, line: str | bytes) -> None:
        self.observer.observe_line(line)

    def note_error(self, error: str) -> None:
        # Local (adapter-side) failure label only; never upstream content.
        safe = _safe_identifier(error)
        if safe:
            self.error = safe

    def note_error_body(self, content: bytes) -> None:
        """Record only the structured error code/param/type of a rejected request."""
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            self.observer.note_error_object(payload["error"])

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        if not self.enabled:
            return
        self.recent.add(self.fingerprint)
        try:
            event = self._build_event()
            logging.getLogger(TELEMETRY_LOGGER_NAME).info(
                json.dumps(event, separators=(",", ":"), ensure_ascii=False)
            )
        except Exception:
            logging.getLogger(TELEMETRY_LOGGER_NAME).debug("telemetry emission failed", exc_info=True)

    def _build_event(self) -> dict[str, Any]:
        fp = self.fingerprint
        observer = self.observer
        event: dict[str, Any] = {
            "event": "siwc.request",
            "req_id": fp.req_id,
            "seq": self.seq,
            "ts": self._started_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "model": fp.model,
            "reasoning_effort": fp.reasoning_effort,
            "stream_requested": self.stream_requested,
            "input_items": fp.item_count,
            "input_bytes": fp.input_bytes,
            "body_bytes": fp.body_bytes,
            "instructions_bytes": fp.instructions_bytes,
            "instructions_sha16": fp.instructions_sha16,
            "tools_count": fp.tools_count,
            "tools_bytes": fp.tools_bytes,
            "tools_sha16": fp.tools_sha16,
            "items": [item.as_list() for item in fp.items],
            "category_bytes": {
                "messages": fp.kind_prefix_bytes("message"),
                "function_calls": fp.exact_kind_bytes("function_call"),
                "tool_results": fp.exact_kind_bytes("function_call_output"),
                "reasoning": fp.exact_kind_bytes("reasoning"),
            },
            "cache_routing": self.cache_key_source,
            "upstream_dispatched": self.upstream_dispatched,
            "upstream_status": self.upstream_status,
            "duration_ms": round((time.monotonic() - self.started) * 1000, 1),
            "response_id": observer.response_id,
            "response_status": observer.status,
            "terminal_event": observer.terminal_event,
            "usage": observer.usage or None,
            "error": self.error,
            "error_code": observer.error_code,
            "error_param": observer.error_param,
            "error_type": observer.error_type,
            "rate_limit": self.rate_limit or None,
        }
        comparison = self.recent.compare(fp)
        if comparison:
            event["prefix"] = comparison
        return event


def summarize_events(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Small helper used by tests and audits to aggregate a telemetry batch."""
    total_input = 0
    total_cached = 0
    total_output = 0
    calls = 0
    per_model: dict[str, int] = {}
    for event in events:
        calls += 1
        usage = event.get("usage") or {}
        total_input += usage.get("input_tokens") or 0
        total_cached += usage.get("cached_tokens") or 0
        total_output += usage.get("output_tokens") or 0
        model = event.get("model") or "unknown"
        per_model[model] = per_model.get(model, 0) + 1
    return {
        "calls": calls,
        "input_tokens": total_input,
        "cached_tokens": total_cached,
        "cache_hit_ratio": (total_cached / total_input) if total_input else 0.0,
        "output_tokens": total_output,
        "per_model": per_model,
    }
