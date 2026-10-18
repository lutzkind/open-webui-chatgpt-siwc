"""Persistent Responses WebSocket transport for the ChatGPT-plan (SIWC) route.

One authenticated WebSocket connection to ``wss://api.openai.com/v1/responses``
carries FIFO ``response.create`` events. Live acceptance on the current SIWC
resource rejects the WebSocket-only ``stream_id`` parameter ("Unsupported
parameter: stream_id"), so a connection carries a single implicit lane and the
adapter serializes requests per connection instead of multiplexing named
streams. Conversation continuity is still maintained per conversation with
``previous_response_id`` plus only the new input items.

Live acceptance also showed the SIWC route reports cached tokens for the
retained prefix on a continuation, which the HTTP route never did; the
transport therefore exists to keep tool loops incremental.

State kept here is deliberately minimal and content-free: a lane holds the
latest upstream response id, the canonical item digests of the last request,
comparison signatures of the response output items, a connection generation
and a last-used timestamp. Prompt text, tool arguments and tool results are
never stored.

Recovery: a connection that closes, expires (60-minute server limit), or whose
token rotated is replaced on the next request; every lane from the previous
generation is invalid and the next request per conversation sends full context
again, then incremental continuation resumes from the new chain.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
from websockets.protocol import State

from .config import Settings
from .continuation import (
    ContinuationPlan,
    LaneState,
    OutputSignature,
    continuation_eligible,
    full_context_plan,
    plan_continuation,
    signature_from_output_item,
)
from .store import CredentialError, CredentialStore

WS_URL = "wss://api.openai.com/v1/responses"

TERMINAL_EVENT_TYPES = frozenset(
    {"response.completed", "response.failed", "response.incomplete", "response.cancelled"}
)

# WebSocket-transport internals that must not be relayed to Open WebUI:
# `response.metadata` carries moderation metadata (which can embed prompt and
# output excerpts) and `codex.response.metadata` carries opaque turn-state
# tokens. The HTTP route never receives these events.
INTERNAL_EVENT_TYPES = frozenset(
    {"codex.response.metadata", "response.metadata", "responsesapi.websocket_timing"}
)

# Fields that exist only for the HTTP SSE transport. `stream_id` is never
# sent: the SIWC resource rejects it with HTTP 400.
DROP_FRAME_FIELDS = ("stream", "stream_options", "stream_id")

MAX_REQUEST_ATTEMPTS = 2
MAX_RECONNECTS_PER_REQUEST = 1
MAX_LANES = 64
ESTABLISH_COOLDOWN_SECONDS = 30.0
SAFE_ERROR_FIELDS = ("type", "code", "param")


class WsUnavailable(RuntimeError):
    """The WebSocket transport cannot serve this request; use the HTTP path."""


class WsStreamError(RuntimeError):
    """The WebSocket stream ended after events may already have been relayed."""


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _safe_error(event: dict[str, Any]) -> dict[str, Any]:
    detail = event.get("error")
    if not isinstance(detail, dict):
        detail = {}
    safe: dict[str, Any] = {"message": "OpenAI Responses request was rejected."}
    for name in SAFE_ERROR_FIELDS:
        value = detail.get(name)
        if isinstance(value, str) and value.isascii() and value.replace("_", "").isalnum() and len(value) <= 64:
            safe[name] = value
    status = event.get("status")
    if isinstance(status, int) and 400 <= status <= 599:
        safe["status"] = status
    return safe


def classify_establish_error(exc: BaseException) -> str:
    """Short, non-secret label for a failed connection attempt."""
    if isinstance(exc, CredentialError):
        return "credentials_unavailable"
    if isinstance(exc, InvalidStatus):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if status in (401, 403):
            return "websocket_unauthorized"
        return f"websocket_handshake_{status}" if status else "websocket_handshake_rejected"
    if isinstance(exc, InvalidHandshake):
        return "websocket_handshake_failed"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "websocket_timeout"
    name = type(exc).__name__
    if name in {"ConnectionRefusedError", "gaierror"}:
        return "websocket_connect_refused"
    if isinstance(exc, OSError):
        return "websocket_network_error"
    return "websocket_connect_failed"


class _Connection:
    """One authenticated WebSocket connection and its conversation lanes."""

    def __init__(
        self,
        ws: ClientConnection,
        *,
        generation: int,
        token_fingerprint: str,
        max_age: float,
    ) -> None:
        self.ws = ws
        self.generation = generation
        self.token_fingerprint = token_fingerprint
        self.max_age = max_age
        self.created = time.monotonic()
        self.lock = asyncio.Lock()
        self.requests = 0

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.created) >= self.max_age

    @property
    def open(self) -> bool:
        return self.ws.state is State.OPEN

    async def close(self) -> None:
        try:
            await self.ws.close()
        except Exception:  # noqa: BLE001,S110 - best-effort close of a dead socket
            pass


class WsSession:
    """One ``response.create`` round trip with lane bookkeeping."""

    def __init__(
        self,
        transport: WebSocketTransport,
        connection: _Connection,
        *,
        conversation_key: str,
        body: dict[str, Any],
        plan: ContinuationPlan,
        item_hashes: tuple[str, ...],
        model: str | None,
        instructions_sha: str | None,
        tools_sha: str | None,
        report: dict[str, Any],
    ) -> None:
        self.transport = transport
        self.connection = connection
        self.conversation_key = conversation_key
        self.body = body
        self.plan = plan
        self.item_hashes = item_hashes
        self.model = model
        self.instructions_sha = instructions_sha
        self.tools_sha = tools_sha
        self.report = report
        self.error: dict[str, Any] | None = None
        self.error_status: int | None = None
        self.response_id: str | None = None
        self.terminal_event: str | None = None
        self.relayed_events = 0
        self._first_event: dict[str, Any] | None = None
        self._output_item_events: list[dict[str, Any]] = []
        self._terminal_output_signatures: list[OutputSignature] = []
        self._finished = False
        self._released = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Send the frame and wait for the first event, with bounded recovery."""
        for attempt in range(MAX_REQUEST_ATTEMPTS):
            try:
                await self._send_frame()
            except ConnectionClosed:
                self.transport.note_connection_failure("websocket_connection_closed")
                if attempt + 1 < MAX_REQUEST_ATTEMPTS and await self._recover():
                    continue
                raise WsUnavailable("websocket_closed_before_first_event") from None
            try:
                event = await self._recv_event()
            except WsStreamError:
                event = None
            if event is None:
                if attempt + 1 < MAX_REQUEST_ATTEMPTS and await self._recover():
                    continue
                raise WsUnavailable("websocket_closed_before_first_event")
            if event.get("type") == "error":
                if _is_lane_error(event) and attempt + 1 < MAX_REQUEST_ATTEMPTS:
                    # The continuation chain is no longer valid on this
                    # connection: replay the full context instead of failing.
                    self.transport.note_lane_invalidated(self.conversation_key)
                    self.plan = self.transport.full_context_plan(self)
                    self.report["mode"] = "full"
                    self.report["previous_response_used"] = False
                    self.report["previous_response_id"] = None
                    self.report["fallback_reason"] = "previous_response_invalid"
                    continue
                if (
                    _is_connection_limit_error(event)
                    and attempt + 1 < MAX_REQUEST_ATTEMPTS
                    and await self._recover()
                ):
                    continue
                self.error = _safe_error(event)
                status = event.get("status")
                self.error_status = status if isinstance(status, int) else 502
                return
            self._first_event = event
            self._observe(event)
            return
        raise WsUnavailable("websocket_attempts_exhausted")

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        """Yield relayable upstream events up to the terminal event."""
        try:
            if self._first_event is not None and self.transport.is_relayable(self._first_event):
                yield self._first_event
            while self.terminal_event is None and self.error is None:
                event = await self._recv_event()
                if event is None:
                    raise WsStreamError("WebSocket connection closed before the response finished.")
                if event.get("type") == "error":
                    self.error = _safe_error(event)
                    status = event.get("status")
                    self.error_status = status if isinstance(status, int) else 502
                    return
                self._observe(event)
                if self.transport.is_relayable(event):
                    self.relayed_events += 1
                    yield event
        finally:
            self.finish()
            self.release()

    def finish(self) -> None:
        """Update lane state from the observed exchange (idempotent)."""
        if self._finished:
            return
        self._finished = True
        connection = self.connection
        lane = self.transport.lanes.get(self.conversation_key)
        if lane is None:
            lane = LaneState(key=self.conversation_key)
            self.transport.lanes[self.conversation_key] = lane
            self.transport.prune_lanes()
        lane.generation = connection.generation
        lane.last_used = time.monotonic()
        lane.last_mode = self.plan.mode
        lane.model = self.model
        lane.instructions_sha = self.instructions_sha
        lane.tools_sha = self.tools_sha
        signatures = self._output_signatures()
        if self.terminal_event == "response.completed" and self.response_id and signatures:
            lane.response_id = self.response_id
            lane.item_hashes = self.item_hashes
            lane.outputs = signatures
            lane.usable = True
        else:
            lane.usable = False
        self.report["response_id"] = self.response_id
        self.report["terminal_event"] = self.terminal_event
        self.report["output_items"] = len(signatures)

    def abort(self) -> None:
        """Mark the lane unusable when the caller abandons the exchange."""
        if not self._finished:
            lane = self.transport.lanes.get(self.conversation_key)
            if lane is not None:
                lane.usable = False
        self.finish()
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self.connection.lock.locked():
            try:
                self.connection.lock.release()
            except RuntimeError:  # pragma: no cover - already released
                pass

    # -- internals ---------------------------------------------------------

    def _output_signatures(self) -> tuple[OutputSignature, ...]:
        signatures: list[OutputSignature] = []
        for item in self._output_item_events:
            signature = signature_from_output_item(item)
            if signature is not None:
                signatures.append(signature)
        if signatures:
            return tuple(signatures)
        return tuple(self._terminal_output_signatures)

    def _frame(self) -> dict[str, Any]:
        frame = {
            key: value
            for key, value in self.body.items()
            if key not in DROP_FRAME_FIELDS and key != "input"
        }
        frame["type"] = "response.create"
        frame["input"] = self.plan.input_items
        if self.plan.previous_response_id:
            frame["previous_response_id"] = self.plan.previous_response_id
        return frame

    async def _send_frame(self) -> None:
        encoded = json.dumps(self._frame(), separators=(",", ":"), ensure_ascii=False)
        await self.connection.ws.send(encoded)
        self.connection.requests += 1

    async def _recv_event(self) -> dict[str, Any] | None:
        timeout = self.transport.settings.WS_EVENT_TIMEOUT_SECONDS
        try:
            raw = await asyncio.wait_for(self.connection.ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            self.transport.note_connection_failure("websocket_event_timeout")
            raise WsStreamError("WebSocket event timeout.") from None
        except ConnectionClosed:
            self.transport.note_connection_failure("websocket_connection_closed")
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            event = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.transport.note_connection_failure("websocket_protocol_error")
            raise WsStreamError("Invalid WebSocket event from OpenAI.") from None
        if not isinstance(event, dict):
            self.transport.note_connection_failure("websocket_protocol_error")
            raise WsStreamError("Invalid WebSocket event from OpenAI.") from None
        return event

    async def _recover(self) -> bool:
        if self.report.get("reconnects", 0) >= MAX_RECONNECTS_PER_REQUEST:
            return False
        replacement = await self.transport.reconnect(self.connection, self.report)
        if replacement is None:
            return False
        self.connection = replacement
        # A new connection has no connection-local state: the continuation
        # chain is gone, so the retry must replay the full context.
        if self.plan.is_continuation:
            self.plan = self.transport.full_context_plan(self)
            self.report["mode"] = "full"
            self.report["previous_response_used"] = False
            self.report["previous_response_id"] = None
            self.report["fallback_reason"] = "reconnected"
        return True

    def _observe(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict):
                self._output_item_events.append(item)
        if event_type in TERMINAL_EVENT_TYPES:
            self.terminal_event = event_type
            response = event.get("response")
            if isinstance(response, dict):
                response_id = response.get("id")
                if isinstance(response_id, str) and response_id:
                    self.response_id = response_id
                output = response.get("output")
                if isinstance(output, list):
                    self._terminal_output_signatures = [
                        signature
                        for signature in (signature_from_output_item(item) for item in output)
                        if signature is not None
                    ]


def _is_lane_error(event: dict[str, Any]) -> bool:
    detail = event.get("error")
    if not isinstance(detail, dict):
        return False
    code = detail.get("code")
    if code == "previous_response_not_found":
        return True
    if detail.get("param") == "previous_response_id":
        return True
    message = detail.get("message")
    return isinstance(message, str) and detail.get("type") == "invalid_request_error" and "previous_response_id" in message


def _is_connection_limit_error(event: dict[str, Any]) -> bool:
    detail = event.get("error")
    return isinstance(detail, dict) and detail.get("code") == "websocket_connection_limit_reached"


class WebSocketTransport:
    """Single-connection, lane-based Responses WebSocket transport."""

    def __init__(self, settings: Settings, store: CredentialStore) -> None:
        self.settings = settings
        self.store = store
        self._connection: _Connection | None = None
        self._guard = asyncio.Lock()
        self.lanes: dict[str, LaneState] = {}
        self._generations = itertools.count(1)
        self._auto_lanes = itertools.count(1)
        self.reconnects = 0
        self.fallback_reason: str | None = None
        self._retry_after = 0.0

    # -- connection management --------------------------------------------

    async def _establish(self) -> _Connection:
        token = await self.store.access_token()
        ws = await websockets.connect(
            WS_URL,
            additional_headers={"Authorization": f"Bearer {token}"},
            open_timeout=self.settings.WS_OPEN_TIMEOUT_SECONDS,
            close_timeout=5,
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
        )
        return _Connection(
            ws,
            generation=next(self._generations),
            token_fingerprint=_sha16(token),
            max_age=self.settings.WS_CONNECTION_MAX_AGE_SECONDS,
        )

    async def connection(self) -> _Connection:
        if time.monotonic() < self._retry_after:
            raise WsUnavailable(self.fallback_reason or "websocket_cooldown")
        async with self._guard:
            connection = self._connection
            if connection is not None and connection.open and not connection.expired:
                try:
                    token = await self.store.access_token()
                except CredentialError as exc:
                    raise WsUnavailable("credentials_unavailable") from exc
                if _sha16(token) == connection.token_fingerprint:
                    return connection
                self.fallback_reason = "credential_rotated"
            if self._connection is not None:
                await self._connection.close()
                self._connection = None
            self._connection = await self._establish_guarded()
            return self._connection

    async def _establish_guarded(self) -> _Connection:
        """Establish a connection, converting failures into bounded fallback."""
        try:
            return await self._establish()
        except CredentialError as exc:
            raise WsUnavailable("credentials_unavailable") from exc
        except Exception as exc:  # noqa: BLE001 - classified to a short safe label
            self.fallback_reason = classify_establish_error(exc)
            self._retry_after = time.monotonic() + ESTABLISH_COOLDOWN_SECONDS
            raise WsUnavailable(self.fallback_reason) from None

    async def reconnect(self, connection: _Connection, report: dict[str, Any]) -> _Connection | None:
        """Replace a dead/expired connection. Returns the new connection."""
        async with self._guard:
            report["reconnects"] = report.get("reconnects", 0) + 1
            if connection is not self._connection:
                # Another task already replaced the socket; this request may
                # retry once on the fresh connection.
                return self._connection
            await connection.close()
            self._connection = None
            self.reconnects += 1
            try:
                self._connection = await self._establish_guarded()
            except WsUnavailable:
                return None
            return self._connection

    def note_connection_failure(self, reason: str) -> None:
        self.fallback_reason = reason

    def note_lane_invalidated(self, conversation_key: str) -> None:
        lane = self.lanes.get(conversation_key)
        if lane is not None:
            lane.usable = False

    def prune_lanes(self) -> None:
        if len(self.lanes) <= MAX_LANES:
            return
        now = time.monotonic()
        idle_ttl = self.settings.WS_LANE_IDLE_TTL_SECONDS
        for key, lane in sorted(self.lanes.items(), key=lambda entry: entry[1].last_used):
            if len(self.lanes) <= MAX_LANES:
                break
            if now - lane.last_used > idle_ttl:
                del self.lanes[key]

    @staticmethod
    def full_context_plan(session: WsSession) -> ContinuationPlan:
        """Full-context plan for a session whose continuation was rejected."""
        return full_context_plan(
            session.body["input"], session.plan.reason or "previous_response_invalid"
        )

    # -- request entry point ----------------------------------------------

    def conversation_key(self, headers: Any) -> str | None:
        """Opaque per-conversation key from an explicitly forwarded header.

        The raw identifier is never stored or logged; only a truncated digest.
        """
        for name in (self.settings.WS_CONVERSATION_HEADER, "X-OpenWebUI-Chat-Id", "X-SIWC-Chat-Id"):
            value = headers.get(name) if hasattr(headers, "get") else None
            if value is None:
                continue
            value = value.strip()
            if not value or len(value) > 200 or not value.isascii():
                continue
            if any(character.isspace() or ord(character) < 32 for character in value):
                continue
            return f"h:{_sha16(name.lower() + ':' + value)}"
        return None

    def _find_lane_by_extension(
        self,
        connection: _Connection,
        *,
        fingerprint: Any,
        generation: int,
        now: float,
    ) -> tuple[str, LaneState | None]:
        """Conversation correlation without a forwarded identifier.

        A lane is reused only when the incoming request is a proven extension
        of that lane's last request; otherwise a fresh lane starts a new chain.
        """
        best: tuple[str, LaneState] | None = None
        for key, lane in self.lanes.items():
            if best is not None and lane.item_count <= best[1].item_count:
                continue
            if continuation_eligible(
                item_hashes=fingerprint.item_hashes,
                model=fingerprint.model,
                instructions_sha=fingerprint.instructions_sha16,
                tools_sha=fingerprint.tools_sha16,
                lane=lane,
                generation=generation,
                now=now,
                idle_ttl=self.settings.WS_LANE_IDLE_TTL_SECONDS,
            ):
                best = (key, lane)
        if best is not None:
            return best
        return f"auto-{next(self._auto_lanes)}", None

    async def begin(
        self,
        *,
        body: dict[str, Any],
        conversation_key: str | None,
        fingerprint: Any,
        report: dict[str, Any],
    ) -> WsSession:
        connection = await self.connection()
        now = time.monotonic()
        generation = connection.generation
        key = conversation_key
        lane: LaneState | None = None
        if key is not None:
            lane = self.lanes.get(key)
        else:
            key, lane = self._find_lane_by_extension(
                connection, fingerprint=fingerprint, generation=generation, now=now
            )
        plan = plan_continuation(
            current_items=body["input"],
            item_hashes=fingerprint.item_hashes,
            model=fingerprint.model,
            instructions_sha=fingerprint.instructions_sha16,
            tools_sha=fingerprint.tools_sha16,
            lane=lane,
            generation=generation,
            now=now,
            idle_ttl=self.settings.WS_LANE_IDLE_TTL_SECONDS,
            instructions_present=bool(body.get("instructions")),
        )
        report.update(
            {
                "kind": "websocket",
                "lane": key,
                "generation": generation,
                "mode": plan.mode,
                "continuation": plan.is_continuation,
                "previous_response_used": plan.is_continuation,
                "previous_response_id": plan.previous_response_id,
                "sent_items": plan.sent_item_count,
                "sent_bytes": plan.sent_bytes,
                "sent_tool_bytes": plan.sent_tool_bytes,
                "received_items": fingerprint.item_count,
                "received_bytes": fingerprint.input_bytes,
                "received_tool_bytes": fingerprint.exact_kind_bytes("function_call_output"),
                "stripped_items": plan.stripped_items,
                "fallback_reason": None if plan.is_continuation else plan.reason,
                "reconnects": 0,
            }
        )
        try:
            await asyncio.wait_for(
                connection.lock.acquire(), timeout=self.settings.WS_LOCK_TIMEOUT_SECONDS
            )
        except (asyncio.TimeoutError, TimeoutError):
            raise WsUnavailable("websocket_busy") from None
        session = WsSession(
            self,
            connection,
            conversation_key=key,
            body=body,
            plan=plan,
            item_hashes=fingerprint.item_hashes,
            model=fingerprint.model,
            instructions_sha=fingerprint.instructions_sha16,
            tools_sha=fingerprint.tools_sha16,
            report=report,
        )
        try:
            await session.start()
        except BaseException:
            session.release()
            raise
        return session

    async def aclose(self) -> None:
        async with self._guard:
            if self._connection is not None:
                await self._connection.close()
                self._connection = None

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def is_relayable(event: dict[str, Any]) -> bool:
        event_type = event.get("type")
        if not isinstance(event_type, str):
            return False
        if event_type in INTERNAL_EVENT_TYPES:
            return False
        return event_type.startswith("response.")
