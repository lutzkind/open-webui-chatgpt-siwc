from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken

from .config import Settings, canonical_host_id

REQUIRED_SCOPES = {
    "openid",
    "profile",
    "email",
    "offline_access",
    "resource.invoke",
    "chatgpt.tokens.use.direct",
}

class CredentialError(RuntimeError):
    pass


def _parse_time(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _scopes(record: dict[str, Any]) -> set[str]:
    value = record.get("scopes", record.get("scope", []))
    if isinstance(value, str):
        return {part for part in value.split() if part}
    if isinstance(value, list):
        return {str(part) for part in value if str(part).strip()}
    return set()


class CredentialStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(settings.SIWC_CREDENTIAL_KEY.get_secret_value().encode())
        self._refresh_lock = asyncio.Lock()
        self._jwks_client = jwt.PyJWKClient(
            f"{settings.OPENAI_AUTH_BASE_URL}/.well-known/jwks.json",
            cache_keys=True,
        )

    def is_configured(self) -> bool:
        return self.settings.encrypted_credentials_path.exists()

    def _validate_id_token(
        self,
        id_token: str,
        client_id: str,
        *,
        expected_subject: str | None = None,
        expected_nonce: str | None = None,
    ) -> dict[str, Any]:
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(id_token)
            claims = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=["RS256", "PS256", "ES256"],
                audience=client_id,
                issuer=self.settings.OPENAI_AUTH_BASE_URL,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except Exception as exc:
            # Token/JWKS library errors can include provider response details.
            # Keep credential material and provider internals out of logs/API errors.
            raise CredentialError("ID token validation failed.") from exc

        if expected_subject and claims.get("sub") != expected_subject:
            raise CredentialError("Refreshed identity does not match the saved ChatGPT account.")
        if expected_nonce is not None and claims.get("nonce") != expected_nonce:
            raise CredentialError("ID token nonce mismatch.")
        return claims

    def validate_record(self, record: dict[str, Any]) -> dict[str, Any]:
        client_id = str(record.get("client_id", "")).strip()
        id_token = str(record.get("id_token", "")).strip()
        access_token = str(record.get("access_token", "")).strip()
        refresh_token = str(record.get("refresh_token", "")).strip()
        host_id = str(record.get("ext_agent_host_id", "")).strip()
        resource = str(record.get("resource", "")).strip()

        if not all((client_id, id_token, access_token, refresh_token, host_id, resource)):
            raise CredentialError("Credential record is missing required SIWC fields.")
        if client_id == "dynamic_agent_client":
            raise CredentialError("Credential record does not contain an issued SIWC client ID.")
        try:
            host_id = canonical_host_id(host_id)
            configured_host_id = canonical_host_id(self.settings.SIWC_HOST_ID.get_secret_value())
        except ValueError as exc:
            raise CredentialError("Credential host ID does not match this VM.") from exc
        if host_id != configured_host_id:
            raise CredentialError("Credential host ID does not match this VM.")
        if resource.rstrip("/") != self.settings.OPENAI_API_BASE_URL.rstrip("/"):
            raise CredentialError("Credential resource does not match the configured OpenAI API.")
        missing = REQUIRED_SCOPES - _scopes(record)
        if missing:
            raise CredentialError(
                "Credential record is missing required scopes: " + ", ".join(sorted(missing))
            )

        claims = self._validate_id_token(id_token, client_id)
        record = dict(record)
        record["ext_agent_host_id"] = host_id
        record["subject"] = claims["sub"]
        record.pop("email", None)
        record["scopes"] = sorted(_scopes(record))
        return record

    def _atomic_write(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
            os.chmod(path, 0o600)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def save(self, record: dict[str, Any]) -> None:
        payload = json.dumps(record, separators=(",", ":"), sort_keys=True).encode()
        self._atomic_write(self.settings.encrypted_credentials_path, self._fernet.encrypt(payload))

    def load(self) -> dict[str, Any]:
        path = self.settings.encrypted_credentials_path
        if not path.exists():
            raise CredentialError("ChatGPT plan credentials have not been connected.")
        try:
            plaintext = self._fernet.decrypt(path.read_bytes())
            record = json.loads(plaintext)
        except (InvalidToken, json.JSONDecodeError) as exc:
            raise CredentialError("Stored ChatGPT credentials cannot be decrypted.") from exc
        if not isinstance(record, dict):
            raise CredentialError("Stored ChatGPT credential record is invalid.")
        return record

    def _expires_at(self, record: dict[str, Any]) -> float:
        explicit = _parse_time(record.get("expires_at"))
        if explicit is not None:
            return explicit
        saved = _parse_time(record.get("saved_at")) or time.time()
        try:
            lifetime = float(record.get("expires_in", 3600))
        except (TypeError, ValueError):
            lifetime = 3600
        return saved + lifetime

    async def _refresh(self, record: dict[str, Any]) -> dict[str, Any]:
        earliest = _parse_time(record.get("earliest_refresh_at"))
        if earliest and time.time() < earliest:
            return record

        data = {
            "grant_type": "refresh_token",
            "client_id": record["client_id"],
            "refresh_token": record["refresh_token"],
            "resource": self.settings.OPENAI_API_BASE_URL,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.settings.OPENAI_AUTH_BASE_URL}/api/accounts/oauth/token",
                data=data,
                headers={"Accept": "application/json"},
            )
        if response.status_code != 200:
            raise CredentialError(
                f"ChatGPT OAuth refresh failed with HTTP {response.status_code}."
            )

        refreshed = response.json()
        try:
            refreshed_lifetime = float(refreshed.get("expires_in"))
        except (TypeError, ValueError):
            refreshed_lifetime = 0
        if not str(refreshed.get("access_token", "")).strip() or refreshed_lifetime <= 0:
            raise CredentialError("ChatGPT OAuth refresh returned an incomplete token response.")
        new_record = dict(record)
        for key in (
            "access_token",
            "refresh_token",
            "id_token",
            "token_type",
            "expires_in",
            "earliest_refresh_at",
        ):
            if refreshed.get(key) is not None:
                new_record[key] = refreshed[key]
        if refreshed.get("expires_at") is None:
            new_record.pop("expires_at", None)

        # A refresh response may rotate the granted scope set. `scopes` is the
        # normalized field used by this store, so don't let an old copy mask it.
        if refreshed.get("scope") is not None:
            new_record["scopes"] = sorted(_scopes({"scope": refreshed["scope"]}))

        new_record["saved_at"] = datetime.now().astimezone().isoformat()
        new_record["scopes"] = sorted(_scopes(new_record))
        missing = REQUIRED_SCOPES - set(new_record["scopes"])
        if missing:
            raise CredentialError(
                "Refreshed token lost required scopes: " + ", ".join(sorted(missing))
            )

        if refreshed.get("id_token"):
            claims = self._validate_id_token(
                new_record["id_token"],
                new_record["client_id"],
                expected_subject=record.get("subject"),
            )
            new_record["subject"] = claims["sub"]
            new_record.pop("email", None)

        self.save(new_record)
        return new_record

    async def access_token(self) -> str:
        async with self._refresh_lock:
            record = self.load()
            if self._expires_at(record) <= time.time() + self.settings.TOKEN_REFRESH_SKEW_SECONDS:
                record = await self._refresh(record)
            if self._expires_at(record) <= time.time():
                raise CredentialError(
                    "ChatGPT access token is expired and cannot yet be refreshed."
                )
            token = str(record.get("access_token", "")).strip()
            if not token:
                raise CredentialError("Stored ChatGPT access token is missing.")
            return token

    def status(self) -> dict[str, Any]:
        if not self.is_configured():
            return {"configured": False}
        record = self.load()
        return {
            "configured": True,
            "expires_at": self._expires_at(record),
            "scopes_ok": REQUIRED_SCOPES.issubset(_scopes(record)),
        }
