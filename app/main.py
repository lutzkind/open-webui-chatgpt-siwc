from __future__ import annotations

import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .caching import apply_cache_policy
from .config import Settings, get_settings
from .connect import router as connect_router
from .store import CredentialError, CredentialStore
from .telemetry import (
    RecentRequests,
    RequestTelemetry,
    setup_logging,
)
from .ws_transport import WebSocketTransport, WsStreamError, WsUnavailable

UNSUPPORTED_PLAN_FIELDS = {
    "background",
    "conversation",
    "max_output_tokens",
    "max_tool_calls",
    "metadata",
    "moderation",
    "multi_agent",
    "previous_response_id",
    "prompt",
    "prompt_cache_retention",
    "safety_identifier",
    "temperature",
    "top_logprobs",
    "top_p",
    "truncation",
    "user",
}

TERMINAL_RESPONSE_EVENTS = {
    "response.cancelled",
    "response.completed",
    "response.failed",
    "response.incomplete",
}


def _require_adapter_auth(authorization: str | None, settings: Settings) -> None:
    expected = f"Bearer {settings.ADAPTER_API_KEY.get_secret_value()}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def sanitize_responses_payload(body: dict) -> dict:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Responses request must be a JSON object.")
    clean = {key: value for key, value in body.items() if key not in UNSUPPORTED_PLAN_FIELDS}
    if "reasoning_effort" in clean:
        # Open WebUI sends its native control as a legacy flat field. Responses
        # expects the equivalent value inside `reasoning`; when both forms are
        # present, the explicit UI control wins while other native fields stay.
        effort = clean.pop("reasoning_effort")
        reasoning = clean.get("reasoning")
        normalized_reasoning = dict(reasoning) if isinstance(reasoning, dict) else {}
        normalized_reasoning["effort"] = effort
        clean["reasoning"] = normalized_reasoning
    clean["store"] = False
    clean["stream"] = True
    if not isinstance(clean.get("input"), list):
        raise HTTPException(
            status_code=400,
            detail="ChatGPT plan usage requires Responses API input to be an array.",
        )
    # SIWC does not accept system-role input items. Open WebUI's Responses
    # provider normally sends these as `instructions`, but normalize any item
    # supplied by another compatible client to the documented developer role.
    clean["input"] = [
        {**item, "role": "developer"} if isinstance(item, dict) and item.get("role") == "system" else item
        for item in clean["input"]
    ]
    return clean


def _event_from_sse_data(data: str) -> dict | None:
    if data.strip() == "[DONE]":
        return None

    try:
        event = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=502, detail="Invalid event from OpenAI Responses API.") from exc

    if not isinstance(event, dict):
        raise HTTPException(status_code=502, detail="Invalid event from OpenAI Responses API.")

    if event.get("type") == "error":
        raise HTTPException(status_code=502, detail="OpenAI Responses request failed.")

    return event


class ResponseCollector:
    """Collect a completed Response object when the caller requested JSON."""

    def __init__(self) -> None:
        self.output_items: dict[int, dict] = {}

    def consume(self, event: dict | None) -> dict | None:
        if event is None:
            return None

        event_type = event.get("type")
        if event_type == "response.output_item.done":
            output_index = event.get("output_index")
            item = event.get("item")
            if isinstance(output_index, int) and output_index >= 0 and isinstance(item, dict):
                self.output_items[output_index] = item

        if event_type not in TERMINAL_RESPONSE_EVENTS:
            return None

        response = event.get("response")
        if not isinstance(response, dict):
            raise HTTPException(status_code=502, detail="OpenAI Responses stream ended with an invalid response.")
        if event_type != "response.completed" or response.get("status") != "completed":
            raise HTTPException(status_code=502, detail="OpenAI Responses request did not complete successfully.")

        output = response.get("output")
        if not isinstance(output, list) or not output:
            # SIWC can emit completed output items in the SSE stream while its
            # terminal response object has an empty output array.
            recovered_output = [self.output_items[index] for index in sorted(self.output_items)]
            if not recovered_output:
                raise HTTPException(status_code=502, detail="OpenAI Responses stream completed without output.")
            response["output"] = recovered_output
        return response


async def read_non_streaming_response(upstream: httpx.Response, telemetry: RequestTelemetry | None = None) -> dict:
    """Collect a completed Response object when the caller requested JSON."""
    data_lines: list[str] = []
    collector = ResponseCollector()

    def parse_pending() -> dict | None:
        if not data_lines:
            return None
        data = "\n".join(data_lines)
        data_lines.clear()
        return _event_from_sse_data(data)

    async for line in upstream.aiter_lines():
        if telemetry is not None:
            telemetry.observe_line(line)
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
        if not line:
            response = collector.consume(parse_pending())
            if response is not None:
                return response

    response = collector.consume(parse_pending())
    if response is not None:
        return response
    raise HTTPException(status_code=502, detail="OpenAI Responses stream ended before completion.")


def normalize_models(payload: dict) -> dict:
    raw = payload.get("models")
    if raw is None:
        raw = payload.get("data", [])
    data = []
    for item in raw or []:
        if not isinstance(item, dict) or item.get("visibility") != "list":
            continue
        model_id = item.get("slug") or item.get("id")
        if not model_id:
            continue
        data.append(
            {
                "id": model_id,
                "object": "model",
                "owned_by": "openai",
                "name": item.get("display_name") or item.get("name") or model_id,
            }
        )
    return {"object": "list", "data": data}


def _sse_event_bytes(event: dict) -> bytes:
    """Serialize one upstream Responses event as an SSE record.

    Byte framing matches what the HTTP relay forwards from OpenAI: an
    ``event:`` line for the type plus the single-line JSON ``data:`` payload.
    """
    payload = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
    lines = []
    event_type = event.get("type")
    if isinstance(event_type, str) and event_type:
        lines.append(f"event: {event_type}")
    lines.append(f"data: {payload}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _websocket_stream_error_event() -> dict:
    return {
        "type": "error",
        "status": 502,
        "error": {
            "type": "server_error",
            "code": "websocket_stream_failed",
            "message": "The upstream Responses connection closed before the response finished.",
        },
    }


async def relay_websocket_stream(session, telemetry: RequestTelemetry) -> AsyncIterator[bytes]:
    """Relay WebSocket transport events to a streaming client as SSE."""
    try:
        async for event in session.stream():
            telemetry.observe_event(event)
            yield _sse_event_bytes(event)
    except WsStreamError:
        telemetry.note_error("websocket_stream_failed")
        yield _sse_event_bytes(_websocket_stream_error_event())
    finally:
        telemetry.note_transport(session.report)
        telemetry.finish()


async def collect_websocket_response(session, telemetry: RequestTelemetry) -> dict:
    """Collect a completed Response object for a non-streaming caller."""
    collector = ResponseCollector()
    try:
        async for event in session.stream():
            telemetry.observe_event(event)
            response = collector.consume(event)
            if response is not None:
                return response
    except WsStreamError as exc:
        telemetry.note_error("websocket_stream_failed")
        raise HTTPException(status_code=502, detail="OpenAI Responses stream ended before completion.") from exc
    finally:
        telemetry.note_transport(session.report)
    if session.error is not None:
        raise HTTPException(status_code=session.error_status or 502, detail="OpenAI Responses request failed.")
    raise HTTPException(status_code=502, detail="OpenAI Responses stream ended before completion.")


def create_app(
    settings_override: Settings | None = None,
    store_override: CredentialStore | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        runtime_settings = settings_override or get_settings()
        credential_store = store_override or CredentialStore(runtime_settings)
        application.state.runtime_settings = runtime_settings
        application.state.credential_store = credential_store
        application.state.recent_requests = RecentRequests()
        application.state.ws_transport = WebSocketTransport(runtime_settings, credential_store)
        setup_logging()
        try:
            yield
        finally:
            await application.state.ws_transport.aclose()

    application = FastAPI(
        title="Open WebUI ChatGPT SIWC",
        version="0.2.0",
        lifespan=lifespan,
    )

    @application.get("/health")
    async def health(request: Request) -> dict:
        credential_store = request.app.state.credential_store
        configured = credential_store.is_configured()
        if configured:
            try:
                credential_store.load()
            except CredentialError:
                return {"ok": False, "configured": True, "error": "Stored credentials are invalid."}
        return {"ok": True, "configured": configured}

    @application.get("/v1/models")
    async def models(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        settings = request.app.state.runtime_settings
        credential_store = request.app.state.credential_store
        _require_adapter_auth(authorization, settings)
        if not credential_store.is_configured():
            raise HTTPException(status_code=503, detail="Sign in with ChatGPT to connect this adapter.")
        try:
            token = await credential_store.access_token()
        except CredentialError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{settings.OPENAI_API_BASE_URL}/models",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        if response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"OpenAI model discovery failed (upstream HTTP {response.status_code}).",
            )
        try:
            return normalize_models(response.json())
        except (json.JSONDecodeError, TypeError, AttributeError) as exc:
            raise HTTPException(status_code=502, detail="Invalid model response from OpenAI.") from exc

    @application.post("/v1/responses")
    async def responses(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        settings = request.app.state.runtime_settings
        credential_store = request.app.state.credential_store
        _require_adapter_auth(authorization, settings)
        try:
            incoming = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Request body must be valid JSON.") from exc
        stream_requested = isinstance(incoming, dict) and incoming.get("stream") is True
        body = sanitize_responses_payload(incoming)
        body, cache_report = apply_cache_policy(body, settings)
        telemetry = RequestTelemetry(
            settings,
            body,
            cache_key_source=cache_report["routing_key"],
            recent=request.app.state.recent_requests,
        )
        telemetry.note_cache_breakpoints(cache_report["breakpoints_added"])
        telemetry.note_stream_requested(stream_requested)
        stream_issued = False
        try:
            if not credential_store.is_configured():
                raise HTTPException(status_code=503, detail="Sign in with ChatGPT to connect this adapter.")

            session = None
            if settings.RESPONSES_TRANSPORT == "websocket":
                transport: WebSocketTransport = request.app.state.ws_transport
                report: dict = {}
                try:
                    session = await transport.begin(
                        body=body,
                        conversation_key=transport.conversation_key(request.headers),
                        fingerprint=telemetry.fingerprint,
                        report=report,
                    )
                except WsUnavailable as exc:
                    telemetry.note_error("websocket_unavailable")
                    telemetry.note_transport({"kind": "websocket", "fallback": True, "fallback_reason": str(exc)})
                    session = None
                if session is not None:
                    telemetry.note_transport(session.report)
                    if session.error is not None:
                        session.abort()
                        telemetry.note_error("upstream_rejected")
                        return JSONResponse(
                            status_code=session.error_status or 502,
                            content={"error": session.error},
                        )
                    if stream_requested:
                        stream_issued = True
                        return StreamingResponse(
                            relay_websocket_stream(session, telemetry),
                            status_code=200,
                            media_type="text/event-stream",
                        )
                    response = await collect_websocket_response(session, telemetry)
                    return JSONResponse(status_code=200, content=response)

            try:
                token = await credential_store.access_token()
            except CredentialError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc

            client = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.REQUEST_TIMEOUT_SECONDS, connect=20)
            )
            upstream_request = client.build_request(
                "POST",
                f"{settings.OPENAI_API_BASE_URL}/responses",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                json=body,
            )
            try:
                upstream = await client.send(upstream_request, stream=True)
            except httpx.HTTPError:
                await client.aclose()
                telemetry.note_error("upstream_unreachable")
                raise HTTPException(status_code=502, detail="OpenAI Responses request failed.")
            telemetry.note_upstream_status(upstream.status_code, getattr(upstream, "headers", None))

            if upstream.status_code >= 400:
                status_code = upstream.status_code
                try:
                    content = await upstream.aread()
                    try:
                        upstream_error = json.loads(content)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        upstream_error = {}
                    error = upstream_error.get("error", {}) if isinstance(upstream_error, dict) else {}
                    safe_error = {"message": "OpenAI Responses request was rejected."}
                    for field in ("type", "code"):
                        value = error.get(field) if isinstance(error, dict) else None
                        if isinstance(value, str) and value.isascii() and value.replace("_", "").isalnum() and len(value) <= 64:
                            safe_error[field] = value
                    telemetry.note_error_body(content)
                finally:
                    await upstream.aclose()
                    await client.aclose()
                return JSONResponse(status_code=status_code, content={"error": safe_error})

            async def stream() -> AsyncIterator[bytes]:
                try:
                    async for chunk in upstream.aiter_bytes():
                        telemetry.observe_chunk(chunk)
                        yield chunk
                finally:
                    try:
                        await upstream.aclose()
                        await client.aclose()
                    finally:
                        telemetry.finish()

            # SIWC is requested as an upstream SSE stream; restore the caller's
            # requested response mode at this OpenAI-compatible boundary.
            if stream_requested:
                stream_issued = True
                return StreamingResponse(stream(), status_code=upstream.status_code, media_type="text/event-stream")

            try:
                try:
                    response = await read_non_streaming_response(upstream, telemetry)
                except httpx.HTTPError as exc:
                    raise HTTPException(status_code=502, detail="OpenAI Responses request failed.") from exc
                return JSONResponse(status_code=upstream.status_code, content=response)
            finally:
                await upstream.aclose()
                await client.aclose()
        finally:
            if not stream_issued:
                # The streaming path emits when the relayed body finishes.
                telemetry.finish()

    application.include_router(connect_router(models))
    return application


app = create_app()
