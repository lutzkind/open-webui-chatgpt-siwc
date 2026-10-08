from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings
from app.store import REQUIRED_SCOPES, CredentialError, CredentialStore

HOST_ID = "00000000-0000-4000-8000-000000000001"
ORIGIN = "https://open-webui.example.test"
API_KEY = "test-adapter-key-" + "0" * 32


def make_settings(tmp_path):
    return Settings(
        ADAPTER_API_KEY=API_KEY,
        SIWC_CREDENTIAL_KEY=Fernet.generate_key().decode(),
        SIWC_HOST_ID=HOST_ID,
        CONNECT_ADMIN_EMAILS="admin@example.test",
        OPEN_WEBUI_PUBLIC_ORIGIN=ORIGIN,
        CONNECT_PUBLIC_URL=ORIGIN + "/siwc/connect",
        ADAPTER_INTERNAL_URL="http://siwc-adapter:8080/v1",
        DATA_DIR=tmp_path,
    )


def credential_record(**overrides):
    record = {
        "client_id": "issued-client-id",
        "id_token": "synthetic-id-token",
        "access_token": "synthetic-access-token",
        "refresh_token": "synthetic-refresh-token",
        "ext_agent_host_id": HOST_ID,
        "resource": "https://api.openai.com/v1",
        "scope": " ".join(sorted(REQUIRED_SCOPES)),
        "expires_in": 3600,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    record.update(overrides)
    return record


def fake_id_token(store, *, nonce="synthetic-nonce", subject="synthetic-subject", audience="issued-client-id", issuer="https://auth.openai.com"):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    store._jwks_client = SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=public_key)
    )
    return jwt.encode(
        {
            "iss": issuer,
            "aud": audience,
            "sub": subject,
            "nonce": nonce,
            "exp": int(time.time()) + 300,
        },
        private_key,
        algorithm="RS256",
    )


def test_host_id_and_scope_validation_fail_closed(tmp_path, monkeypatch):
    store = CredentialStore(make_settings(tmp_path))
    monkeypatch.setattr(store, "_validate_id_token", lambda *args, **kwargs: {"sub": "synthetic-subject"})
    assert store.validate_record(credential_record(ext_agent_host_id=f"urn:uuid:{HOST_ID}"))["ext_agent_host_id"] == f"urn:uuid:{HOST_ID}"
    with pytest.raises(CredentialError, match="does not match"):
        store.validate_record(credential_record(ext_agent_host_id="00000000-0000-4000-8000-000000000002"))
    with pytest.raises(CredentialError, match="missing required scopes"):
        store.validate_record(credential_record(scope="openid profile email"))


def test_id_token_requires_valid_signature_issuer_audience_expiry_and_nonce(tmp_path):
    store = CredentialStore(make_settings(tmp_path))
    token = fake_id_token(store)
    claims = store._validate_id_token(
        token,
        "issued-client-id",
        expected_subject="synthetic-subject",
        expected_nonce="synthetic-nonce",
    )
    assert claims["sub"] == "synthetic-subject"

    with pytest.raises(CredentialError, match="ID token validation failed"):
        store._validate_id_token(token, "wrong-client-id")
    with pytest.raises(CredentialError, match="nonce mismatch"):
        store._validate_id_token(token, "issued-client-id", expected_nonce="wrong-nonce")


def test_encrypted_credential_storage_has_no_plaintext_file(tmp_path, monkeypatch):
    store = CredentialStore(make_settings(tmp_path))
    monkeypatch.setattr(store, "_validate_id_token", lambda *args, **kwargs: {"sub": "synthetic-subject"})
    validated = store.validate_record(credential_record())
    store.save(validated)

    encrypted = store.settings.encrypted_credentials_path
    assert encrypted.is_file()
    assert encrypted.stat().st_mode & 0o777 == 0o600
    contents = encrypted.read_bytes()
    assert b"synthetic-access-token" not in contents
    assert b"synthetic-refresh-token" not in contents
    assert store.load()["subject"] == "synthetic-subject"
    assert [path.name for path in tmp_path.iterdir()] == ["credentials.enc"]


class RefreshResponse:
    status_code = 200

    def json(self):
        return {
            "access_token": "rotated-access-token",
            "refresh_token": "rotated-refresh-token",
            "expires_in": 3600,
            "scope": " ".join(sorted(REQUIRED_SCOPES)),
            "earliest_refresh_at": time.time() - 1,
        }


class FakeAsyncClient:
    request = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, **kwargs):
        self.request = (url, kwargs)
        FakeAsyncClient.request = self.request
        return RefreshResponse()


def test_expired_access_token_refreshes_and_persists_rotation(tmp_path, monkeypatch):
    store = CredentialStore(make_settings(tmp_path))
    store.save(
        credential_record(
            expires_at=time.time() - 10,
            earliest_refresh_at=time.time() - 5,
            subject="synthetic-subject",
        )
    )
    monkeypatch.setattr("app.store.httpx.AsyncClient", FakeAsyncClient)

    assert asyncio.run(store.access_token()) == "rotated-access-token"
    refreshed = store.load()
    assert refreshed["refresh_token"] == "rotated-refresh-token"
    assert set(refreshed["scopes"]) == REQUIRED_SCOPES
    url, kwargs = FakeAsyncClient.request
    assert url.endswith("/api/accounts/oauth/token")
    assert kwargs["data"]["grant_type"] == "refresh_token"
    assert kwargs["data"]["resource"] == "https://api.openai.com/v1"


def test_refresh_failure_does_not_leak_provider_body_or_tokens(tmp_path, monkeypatch):
    class ErrorResponse:
        status_code = 401
        text = "synthetic-refresh-token"

    class ErrorClient(FakeAsyncClient):
        async def post(self, url, **kwargs):
            return ErrorResponse()

    store = CredentialStore(make_settings(tmp_path))
    store.save(credential_record(expires_at=time.time() - 10, earliest_refresh_at=time.time() - 1))
    monkeypatch.setattr("app.store.httpx.AsyncClient", ErrorClient)
    with pytest.raises(CredentialError) as error:
        asyncio.run(store.access_token())
    assert "401" in str(error.value)
    assert "synthetic-refresh-token" not in str(error.value)
