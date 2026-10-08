from __future__ import annotations

from typing import ClassVar

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app, normalize_models, sanitize_responses_payload

API_KEY = "test-adapter-key-" + "0" * 32
HOST_ID = "00000000-0000-4000-8000-000000000001"
ORIGIN = "https://open-webui.example.test"


class StubStore:
    def __init__(self, token="chatgpt-access-token"):
        self.token = token

    def is_configured(self):
        return True

    def load(self):
        return {"access_token": self.token}

    async def access_token(self):
        return self.token


def settings(tmp_path):
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


class FakeUpstream:
    def __init__(self, status_code=200, body=b"data: {\"type\":\"response.completed\"}\n\n"):
        self.status_code = status_code
        self.body = body
        self.closed = False

    async def aiter_bytes(self):
        yield self.body

    async def aread(self):
        return self.body

    async def aclose(self):
        self.closed = True


class FakeAsyncClient:
    sent: ClassVar[list] = []
    get_response = None
    send_response = None

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, **kwargs):
        self.sent.append(("GET", url, kwargs))
        return self.get_response

    def build_request(self, method, url, **kwargs):
        return {"method": method, "url": url, **kwargs}

    async def send(self, request, stream=False):
        self.sent.append(("SEND", request, {"stream": stream}))
        return self.send_response

    async def aclose(self):
        return None


def test_responses_payload_keeps_supported_fields_and_enforces_plan_rules():
    result = sanitize_responses_payload(
        {
            "model": "available-model",
            "input": [{"role": "user", "content": "hello"}],
            "instructions": "Be useful",
            "tools": [{"type": "function", "name": "lookup"}],
            "tool_choice": "auto",
            "reasoning": {"effort": "medium"},
            "store": True,
            "stream": False,
            "temperature": 0.7,
            "metadata": {"x": "y"},
            "previous_response_id": "resp-old",
        }
    )
    assert result["store"] is False
    assert result["stream"] is True
    assert result["model"] == "available-model"
    assert result["instructions"] == "Be useful"
    assert result["tools"] == [{"type": "function", "name": "lookup"}]
    assert result["tool_choice"] == "auto"
    assert result["reasoning"] == {"effort": "medium"}
    assert "temperature" not in result
    assert "metadata" not in result
    assert "previous_response_id" not in result


def test_responses_payload_without_reasoning_setting_remains_unchanged():
    result = sanitize_responses_payload({"input": [{"role": "user", "content": "hello"}]})
    assert "reasoning" not in result
    assert "reasoning_effort" not in result


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_legacy_reasoning_effort_maps_to_responses_shape(effort):
    result = sanitize_responses_payload(
        {"input": [{"role": "user", "content": "hello"}], "reasoning_effort": effort}
    )
    assert result["reasoning"] == {"effort": effort}
    assert "reasoning_effort" not in result


def test_legacy_reasoning_effort_merges_and_wins_conflicts_deterministically():
    result = sanitize_responses_payload(
        {
            "input": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "high",
            "reasoning": {"effort": "low", "summary": "auto"},
        }
    )
    assert result["reasoning"] == {"effort": "high", "summary": "auto"}
    assert "reasoning_effort" not in result


def test_system_input_role_becomes_developer_and_non_array_is_rejected():
    result = sanitize_responses_payload({"input": [{"role": "system", "content": "Rules"}]})
    assert result["input"] == [{"role": "developer", "content": "Rules"}]
    with pytest.raises(Exception, match="input to be an array"):
        sanitize_responses_payload({"input": "hello"})


def test_model_catalog_exposes_only_explicitly_listed_models():
    result = normalize_models(
        {
            "models": [
                {"slug": "model-sol", "display_name": "Sol", "visibility": "list"},
                {"slug": "hidden", "display_name": "Hidden", "visibility": "hidden"},
                {"slug": "unknown", "display_name": "Unknown"},
            ]
        }
    )
    assert result == {
        "object": "list",
        "data": [
            {"id": "model-sol", "object": "model", "owned_by": "openai", "name": "Sol"}
        ],
    }


def test_adapter_authentication_and_health_are_safe(tmp_path):
    with TestClient(create_app(settings(tmp_path), StubStore())) as client:
        assert client.get("/health").json() == {"ok": True, "configured": True}
        assert client.get("/v1/models").status_code == 401
        assert client.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert "email" not in client.get("/health").json()


def test_models_request_authenticates_and_normalizes_catalog(monkeypatch, tmp_path):
    FakeAsyncClient.sent = []
    FakeAsyncClient.get_response = type(
        "Response",
        (),
        {
            "status_code": 200,
            "json": lambda self: {"models": [
                {"slug": "listed-model", "visibility": "list"},
                {"slug": "private-model", "visibility": "private"},
            ]},
        },
    )()
    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)
    with TestClient(create_app(settings(tmp_path), StubStore())) as client:
        response = client.get("/v1/models", headers={"Authorization": f"Bearer {API_KEY}"})
        assert response.status_code == 200
        assert [entry["id"] for entry in response.json()["data"]] == ["listed-model"]
    _, url, kwargs = FakeAsyncClient.sent[0]
    assert url == "https://api.openai.com/v1/models"
    assert kwargs["headers"]["Authorization"] == "Bearer chatgpt-access-token"


def test_responses_stream_is_forwarded_with_sanitized_body(monkeypatch, tmp_path):
    FakeAsyncClient.sent = []
    FakeAsyncClient.send_response = FakeUpstream()
    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)
    with TestClient(create_app(settings(tmp_path), StubStore())) as client:
        response = client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "model": "listed-model",
                "input": [{"role": "user", "content": "hello"}],
                "tools": [{"type": "function", "name": "lookup"}],
                "reasoning_effort": "high",
                "store": True,
                "stream": False,
                "temperature": 0,
                "metadata": {"private": "drop"},
            },
        )
    assert response.status_code == 200
    assert "response.completed" in response.text
    _, request, options = FakeAsyncClient.sent[0]
    assert request["url"] == "https://api.openai.com/v1/responses"
    assert request["headers"]["Authorization"] == "Bearer chatgpt-access-token"
    assert request["json"]["store"] is False
    assert request["json"]["stream"] is True
    assert request["json"]["tools"][0]["name"] == "lookup"
    assert request["json"]["reasoning"] == {"effort": "high"}
    assert "reasoning_effort" not in request["json"]
    assert "temperature" not in request["json"]
    assert "metadata" not in request["json"]
    assert options["stream"] is True


def test_responses_preserves_upstream_error_status(monkeypatch, tmp_path):
    FakeAsyncClient.sent = []
    FakeAsyncClient.send_response = FakeUpstream(
        status_code=429,
        body=b'{"error":{"type":"api_error","code":"rate_limit_exceeded","message":"secret-token-value"}}',
    )
    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)
    with TestClient(create_app(settings(tmp_path), StubStore())) as client:
        response = client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"input": [{"role": "user", "content": "hello"}]},
        )
    assert response.status_code == 429
    assert response.json() == {
        "error": {
            "message": "OpenAI Responses request was rejected.",
            "type": "api_error",
            "code": "rate_limit_exceeded",
        }
    }
    assert "secret-token-value" not in response.text
