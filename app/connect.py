"""Browser and local-companion connection flow for SIWC credentials."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from .config import canonical_host_id
from .store import CredentialError

WEB_ROOT = Path(__file__).parent / "connect_web"
_PAIR_VALUE = re.compile(r"^[A-Za-z0-9_-]{43}\.[A-Za-z0-9_-]{43}$")


def router(discover_models) -> APIRouter:
    routes = APIRouter()
    pending: dict[str, dict] = {}
    import_lock = asyncio.Lock()

    def headers() -> dict[str, str]:
        return {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            ),
        }

    async def admin(request: Request) -> str:
        settings = request.app.state.runtime_settings
        identity = request.headers.get(settings.TRUSTED_EMAIL_HEADER, "").strip()
        if not identity or len(identity) > 320 or "\n" in identity or "\r" in identity:
            raise HTTPException(401, "Open WebUI sign-in required.")
        allowed = set(settings.CONNECT_ADMIN_EMAILS.get_secret_value().split(","))
        if identity.casefold() not in allowed:
            raise HTTPException(403, "Only an allowed Open WebUI administrator can connect ChatGPT.")
        return identity

    def same_origin(request: Request) -> None:
        expected = request.app.state.runtime_settings.OPEN_WEBUI_PUBLIC_ORIGIN.get_secret_value()
        if request.headers.get("origin") != expected:
            raise HTTPException(403, "Invalid connection origin.")

    def prune() -> None:
        now = time.monotonic()
        for ticket in list(pending):
            if pending[ticket]["expires"] <= now:
                del pending[ticket]

    def grant(request: Request) -> tuple[str, dict]:
        prune()
        value = request.headers.get("authorization", "")
        pair_value = value.removeprefix("Pair ")
        if not value.startswith("Pair ") or not _PAIR_VALUE.fullmatch(pair_value):
            raise HTTPException(401, "Connection request expired or invalid.")
        ticket, verifier = pair_value.split(".", 1)
        session = pending.get(ticket)
        digest = hashlib.sha256(verifier.encode()).hexdigest()
        if not session or not secrets.compare_digest(digest, session["challenge"]):
            raise HTTPException(401, "Connection request expired or invalid.")
        return ticket, session

    @routes.get("/siwc/connect")
    async def page():
        return FileResponse(WEB_ROOT / "index.html", headers=headers())

    @routes.get("/siwc/connect/{asset}")
    async def asset(asset: str):
        if asset not in ("connect.js", "connect.css"):
            raise HTTPException(404)
        return FileResponse(WEB_ROOT / asset, headers=headers())

    @routes.get("/siwc/config")
    async def public_config(request: Request):
        await admin(request)
        settings = request.app.state.runtime_settings
        return {
            "connect_url": settings.CONNECT_PUBLIC_URL.get_secret_value(),
            "provider_base_url": settings.ADAPTER_INTERNAL_URL,
        }

    @routes.get("/siwc/status")
    async def status(request: Request):
        await admin(request)
        store = request.app.state.credential_store
        configured = store.is_configured()
        connected = False
        if configured:
            try:
                store.load()
                connected = True
            except CredentialError:
                pass
        artifact = store.settings.DATA_DIR / "companion" / "Connect ChatGPT.zip"
        notarized = False
        if artifact.is_file():
            try:
                import zipfile

                with zipfile.ZipFile(artifact) as bundle:
                    metadata = bundle.read(
                        "Connect ChatGPT.app/Contents/Resources/distribution.json"
                    )
                    notarized = json.loads(metadata).get("developer_id_signed_and_notarized") is True
            except (OSError, ValueError, KeyError, zipfile.BadZipFile):
                pass
        return {
            "configured": configured,
            "connected": connected,
            "companion_available": artifact.is_file()
            or bool(store.settings.CONNECT_COMPANION_DOWNLOAD_URL),
            "companion_notarized": notarized,
        }

    @routes.get("/siwc/download")
    async def download(request: Request):
        await admin(request)
        artifact = request.app.state.runtime_settings.DATA_DIR / "companion" / "Connect ChatGPT.zip"
        if artifact.is_file():
            return FileResponse(
                artifact,
                filename="Connect ChatGPT.zip",
                media_type="application/zip",
                headers=headers(),
            )
        url = request.app.state.runtime_settings.CONNECT_COMPANION_DOWNLOAD_URL
        if not url:
            raise HTTPException(503, "The macOS companion is not available yet.")
        return RedirectResponse(url, status_code=302, headers=headers())

    @routes.post("/siwc/verify")
    async def verify(request: Request):
        same_origin(request)
        await admin(request)
        settings = request.app.state.runtime_settings
        catalog = await discover_models(
            request,
            authorization=f"Bearer {settings.ADAPTER_API_KEY.get_secret_value()}",
        )
        return {"configured": True, "models": [model["id"] for model in catalog["data"]]}

    @routes.post("/siwc/pair")
    async def pair(request: Request):
        same_origin(request)
        await admin(request)
        store = request.app.state.credential_store
        if store.is_configured():
            raise HTTPException(409, "ChatGPT is already connected. Existing credentials were preserved.")
        try:
            body = await request.json()
            challenge = body["challenge"]
            if (
                not isinstance(challenge, str)
                or len(challenge) != 64
                or any(char not in "0123456789abcdef" for char in challenge)
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "Invalid companion request.") from None
        prune()
        if len(pending) >= 8:
            raise HTTPException(429, "Too many connection attempts. Wait a few minutes and try again.")
        ticket = secrets.token_urlsafe(32)
        pending[ticket] = {
            "challenge": challenge,
            "nonce": secrets.token_urlsafe(32),
            "expires": time.monotonic() + 600,
        }
        return {"ticket": ticket}

    @routes.post("/siwc/handoff/prepare")
    async def prepare(request: Request):
        _, session = grant(request)
        if request.app.state.credential_store.is_configured():
            raise HTTPException(409, "Already connected.")
        settings = request.app.state.runtime_settings
        return {
            "host_id": canonical_host_id(settings.SIWC_HOST_ID.get_secret_value()),
            "nonce": session["nonce"],
        }

    @routes.post("/siwc/handoff/complete")
    async def complete(request: Request):
        ticket, _ = grant(request)
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            raise HTTPException(400, "Invalid handoff length.") from None
        if length > 32768:
            raise HTTPException(413, "Handoff is too large.")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 32768:
                raise HTTPException(413, "Handoff is too large.")
        try:
            record = json.loads(raw)
            if not isinstance(record, dict):
                raise TypeError
        except (ValueError, TypeError, UnicodeDecodeError):
            raise HTTPException(400, "Invalid protected handoff.") from None

        async with import_lock:
            ticket, session = grant(request)
            store = request.app.state.credential_store
            if store.is_configured():
                raise HTTPException(409, "Existing credentials were preserved.")
            try:
                validated = await asyncio.to_thread(store.validate_record, record)
                await asyncio.to_thread(
                    store._validate_id_token,
                    validated["id_token"],
                    validated["client_id"],
                    expected_nonce=session["nonce"],
                )
                store.save(validated)
            except CredentialError:
                raise HTTPException(400, "ChatGPT credential validation failed.") from None
            del pending[ticket]
        return {"configured": True}

    return routes
