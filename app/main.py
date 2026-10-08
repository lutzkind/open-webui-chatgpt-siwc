from __future__ import annotations

import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Settings, get_settings
from .connect import router as connect_router
from .store import CredentialError, CredentialStore

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


def _response_from_sse_data(data: str) -> dict | None:
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

    if event.get("type") not in TERMINAL_RESPONSE_EVENTS:
        return None

    response = event.get("response")
    if not isinstance(response, dict):
        raise HTTPException(status_code=502, detail="OpenAI Responses stream ended with an invalid response.")
    return response


async def read_non_streaming_response(upstream: httpx.Response) -> dict:
    """Collect the terminal Response object when the caller requested JSON."""
    data_lines: list[str] = []

    def parse_pending() -> dict | None:
        if not data_lines:
            return None
        data = "\n".join(data_lines)
        data_lines.clear()
        return _response_from_sse_data(data)

    async for line in upstream.aiter_lines():
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
        if not line:
            response = parse_pending()
            if response is not None:
                return response

    response = parse_pending()
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
        yield

    application = FastAPI(
        title="Open WebUI ChatGPT SIWC",
        version="0.1.1",
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
        if not credential_store.is_configured():
            raise HTTPException(status_code=503, detail="Sign in with ChatGPT to connect this adapter.")
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
            raise HTTPException(status_code=502, detail="OpenAI Responses request failed.")

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
            finally:
                await upstream.aclose()
                await client.aclose()
            return JSONResponse(status_code=status_code, content={"error": safe_error})

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        # SIWC is requested as an upstream SSE stream; restore the caller's
        # requested response mode at this OpenAI-compatible boundary.
        if stream_requested:
            return StreamingResponse(stream(), status_code=upstream.status_code, media_type="text/event-stream")

        try:
            try:
                response = await read_non_streaming_response(upstream)
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail="OpenAI Responses request failed.") from exc
            return JSONResponse(status_code=upstream.status_code, content=response)
        finally:
            await upstream.aclose()
            await client.aclose()

    application.include_router(connect_router(models))
    return application


app = create_app()
