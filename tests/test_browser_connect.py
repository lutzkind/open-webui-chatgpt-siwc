from __future__ import annotations

import hashlib
import time

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.store import CredentialError, CredentialStore

ORIGIN = "https://open-webui.example.test"
API_KEY = "test-adapter-key-" + "0" * 32
VERIFIER = "x" * 43
HOST_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    settings = Settings(
        ADAPTER_API_KEY=API_KEY,
        SIWC_CREDENTIAL_KEY=Fernet.generate_key().decode(),
        SIWC_HOST_ID=HOST_ID,
        CONNECT_ADMIN_EMAILS="admin@example.test",
        OPEN_WEBUI_PUBLIC_ORIGIN=ORIGIN,
        CONNECT_PUBLIC_URL=ORIGIN + "/siwc/connect",
        ADAPTER_INTERNAL_URL="http://siwc-adapter:8080/v1",
        DATA_DIR=tmp_path,
    )
    store = CredentialStore(settings)
    monkeypatch.setattr(store, "validate_record", lambda record: record)

    def validate(token, client_id, *, expected_nonce=None):
        if expected_nonce is None:
            return {"sub": "synthetic-subject"}
        if expected_nonce != "valid-nonce":
            raise CredentialError("Nonce mismatch")
        return {"sub": "synthetic-subject"}

    monkeypatch.setattr(store, "_validate_id_token", validate)
    with TestClient(create_app(settings, store)) as client:
        yield client, store, monkeypatch


def approved(client):
    return client.post(
        "/siwc/pair",
        headers={"Origin": ORIGIN, "X-Auth-Request-Email": "admin@example.test"},
        json={"challenge": hashlib.sha256(VERIFIER.encode()).hexdigest()},
    )


def pair_header(ticket, verifier=VERIFIER):
    return {"Authorization": f"Pair {ticket}.{verifier}"}


def test_connect_requires_admin_and_exact_origin(setup):
    client, store, _ = setup
    assert client.get("/siwc/connect").status_code == 200
    assert "no-store" in client.get("/siwc/connect").headers["cache-control"]
    assert client.get("/siwc/status").status_code == 401
    assert client.get("/siwc/status", headers={"X-Auth-Request-Email": "admin@example.test"}).status_code == 200
    assert client.get("/siwc/status", headers={"X-Auth-Request-Email": "user@example.test"}).status_code == 403
    assert client.post("/siwc/pair", headers={"Origin": ORIGIN}, json={}).status_code == 401
    assert client.post(
        "/siwc/pair",
        headers={"Authorization": "Bearer synthetic-session", "Origin": "https://attacker.example.test"},
        json={"challenge": "bad"},
    ).status_code == 403
    assert not store.is_configured()


def test_pair_grant_nonce_replay_and_encrypted_handoff(setup):
    client, store, monkeypatch = setup
    ticket = approved(client).json()["ticket"]
    assert len(ticket) == 43
    assert client.post("/siwc/handoff/prepare", headers=pair_header(ticket, "y" * 43)).status_code == 401
    prepared = client.post("/siwc/handoff/prepare", headers=pair_header(ticket)).json()
    assert prepared["host_id"] == f"urn:uuid:{HOST_ID}"
    record = {
        "id_token": "synthetic-id-token",
        "client_id": "synthetic-client",
        "nonce": prepared["nonce"],
    }

    assert client.post("/siwc/handoff/complete", headers=pair_header(ticket), json=record).status_code == 400
    assert not store.is_configured()
    monkeypatch.setattr(
        store,
        "_validate_id_token",
        lambda token, client_id, **kwargs: {"sub": "synthetic-subject"}
        if kwargs["expected_nonce"] == prepared["nonce"]
        else (_ for _ in ()).throw(CredentialError("bad nonce")),
    )

    assert client.post("/siwc/handoff/complete", headers=pair_header(ticket), json=record).json() == {"configured": True}
    encrypted = store.settings.encrypted_credentials_path.read_bytes()
    assert b"synthetic-id-token" not in encrypted
    assert store.load() == record
    assert [path.name for path in store.settings.DATA_DIR.iterdir()] == ["credentials.enc"]
    assert client.post("/siwc/handoff/complete", headers=pair_header(ticket), json=record).status_code == 401
    assert approved(client).status_code == 409
    assert store.settings.encrypted_credentials_path.read_bytes() == encrypted


def test_expired_and_bounded_pairing_attempts(setup):
    client, _, monkeypatch = setup
    ticket = approved(client).json()["ticket"]
    now = time.monotonic()
    monkeypatch.setattr("app.connect.time.monotonic", lambda: now + 601)
    assert client.post("/siwc/handoff/prepare", headers=pair_header(ticket)).status_code == 401
    for _ in range(8):
        assert approved(client).status_code == 200
    assert approved(client).status_code == 429


def test_companion_download_and_page_are_instance_local(setup):
    client, store, _ = setup
    assert client.get("/siwc/download").status_code == 401
    remote = client.get(
        "/siwc/download",
        headers={"X-Auth-Request-Email": "admin@example.test"},
        follow_redirects=False,
    )
    assert remote.status_code == 302
    assert remote.headers["location"].endswith("Connect-ChatGPT-macOS.zip")
    artifact = store.settings.DATA_DIR / "companion" / "Connect ChatGPT.zip"
    artifact.parent.mkdir()
    artifact.write_bytes(b"synthetic-package")
    local = client.get("/siwc/download", headers={"X-Auth-Request-Email": "admin@example.test"})
    assert local.content == b"synthetic-package"
    for asset in ("connect.js", "connect.css"):
        response = client.get("/siwc/connect/" + asset)
        assert response.status_code == 200
        assert "raw.githubusercontent.com" not in response.text
        assert "localStorage" not in response.text
    assert client.get("/siwc/connect/../../main.py").status_code == 404


def test_oversized_handoff_is_rejected_before_validation(setup):
    client, store, _ = setup
    ticket = approved(client).json()["ticket"]
    response = client.post(
        "/siwc/handoff/complete",
        headers=pair_header(ticket),
        content=b"x" * 32769,
    )
    assert response.status_code == 413
    assert not store.is_configured()


def test_non_object_handoff_is_rejected_without_stack_trace(setup):
    client, store, _ = setup
    ticket = approved(client).json()["ticket"]
    response = client.post("/siwc/handoff/complete", headers=pair_header(ticket), json=[])
    assert response.status_code == 400
    assert not store.is_configured()


def test_admin_verification_uses_authenticated_model_discovery(setup, monkeypatch):
    client, store, _ = setup
    store.save({"access_token": "synthetic-access-token", "expires_at": 9999999999})

    async def get(self, url, **kwargs):
        return httpx.Response(200, json={"models": [{"slug": "synthetic-model", "visibility": "list"}]})

    monkeypatch.setattr("app.main.httpx.AsyncClient.get", get)
    response = client.post(
        "/siwc/verify",
        headers={"Origin": ORIGIN, "X-Auth-Request-Email": "admin@example.test"},
    )
    assert response.status_code == 200
    assert response.json() == {"configured": True, "models": ["synthetic-model"]}
    assert "synthetic-access-token" not in response.text
