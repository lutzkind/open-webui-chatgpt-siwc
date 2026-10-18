from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import ClassVar

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.telemetry import (
    TELEMETRY_LOGGER_NAME,
    RecentRequests,
    RequestTelemetry,
    StreamObserver,
    apply_cache_routing_key,
    build_prompt_cache_key,
    fingerprint_request,
    summarize_events,
)

API_KEY = "test-adapter-key-" + "0" * 32
HOST_ID = "00000000-0000-4000-8000-000000000001"
ORIGIN = "https://open-webui.example.test"
CANARY = "canary-private-conversation-content"


class StubStore:
    def is_configured(self):
        return True

    def load(self):
        return {"access_token": "chatgpt-access-token"}

    async def access_token(self):
        return "chatgpt-access-token"


class FakeUpstream:
    def __init__(self, status_code=200, body=b"", headers=None):
        self.status_code = status_code
        self.body = body
        self.headers = headers or {"x-ratelimit-remaining-tokens": "100", "set-cookie": "secret"}
        self.closed = False

    async def aiter_bytes(self):
        yield self.body

    async def aiter_lines(self):
        for line in self.body.splitlines():
            yield line.decode()

    async def aread(self):
        return self.body

    async def aclose(self):
        self.closed = True


class FakeAsyncClient:
    sent: ClassVar[list] = []
    get_response = None
    send_response = None

    def __init__(self, *args, **kwargs):
        pass

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


def settings(tmp_path, **overrides):
    values = {
        "ADAPTER_API_KEY": API_KEY,
        "SIWC_CREDENTIAL_KEY": Fernet.generate_key().decode(),
        "SIWC_HOST_ID": HOST_ID,
        "CONNECT_ADMIN_EMAILS": "admin@example.test",
        "OPEN_WEBUI_PUBLIC_ORIGIN": ORIGIN,
        "CONNECT_PUBLIC_URL": ORIGIN + "/siwc/connect",
        "ADAPTER_INTERNAL_URL": "http://siwc-adapter:8080/v1",
        "DATA_DIR": tmp_path,
    }
    values.update(overrides)
    return Settings(**values)


@contextmanager
def captured_events():
    class _Collector(logging.Handler):
        def __init__(self):
            super().__init__()
            self.messages: list[str] = []

        def emit(self, record):
            self.messages.append(record.getMessage())

    collector = _Collector()
    logger = logging.getLogger(TELEMETRY_LOGGER_NAME)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(collector)
    try:
        yield collector.messages
    finally:
        logger.removeHandler(collector)
        logger.setLevel(previous_level)


def _completed_body(
    response_id="resp-test",
    usage=None,
    text="assistant text that must never be logged",
):
    usage = usage or {
        "input_tokens": 1000,
        "output_tokens": 50,
        "total_tokens": 1050,
        "input_tokens_details": {"cached_tokens": 800, "cache_write_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 12},
    }
    events = [
        {
            "type": "response.output_text.delta",
            "delta": text,
        },
        {
            "type": "response.created",
            "response": {"id": response_id, "status": "in_progress"},
        },
        {
            "type": "response.completed",
            "response": {"id": response_id, "status": "completed", "usage": usage},
        },
    ]
    body = b""
    for event in events:
        body += b"event: " + event["type"].encode() + b"\n"
        body += b"data: " + json.dumps(event).encode() + b"\n\n"
    return body


def test_stream_observer_extracts_ids_usage_and_errors_without_content():
    observer = StreamObserver()
    observer.feed(
        _completed_body(response_id="resp-1")
        + b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"'
        + CANARY.encode()
        + b'"}\n\n'
    )
    assert observer.response_id == "resp-1"
    assert observer.status == "completed"
    assert observer.terminal_event == "response.completed"
    assert observer.usage == {
        "input_tokens": 1000,
        "output_tokens": 50,
        "total_tokens": 1050,
        "cached_tokens": 800,
        "cache_write_tokens": 0,
        "reasoning_tokens": 12,
    }
    assert observer.error_code is None

    failed = StreamObserver()
    failed.feed(
        b'event: response.failed\ndata: {"type":"response.failed","response":{"id":"resp-2",'
        b'"status":"failed","error":{"code":"subscription_sharing_usage_limit_exceeded","param":"model"}}}\n\n'
    )
    assert failed.error_code == "subscription_sharing_usage_limit_exceeded"
    assert failed.error_param == "model"
    assert failed.terminal_event == "response.failed"


def test_stream_observer_ignores_arbitrary_json_lines():
    observer = StreamObserver()
    observer.feed(b'data: {"random":"value","secret":"' + CANARY.encode() + b'"}\n\n')
    assert observer.response_id is None
    assert observer.usage == {}


def test_fingerprint_derives_sizes_and_hashes_only():
    body = {
        "model": "gpt-6.1-sol",
        "instructions": "system rules",
        "tools": [{"type": "function", "name": "lookup"}],
        "reasoning": {"effort": "high"},
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": CANARY}]},
            {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
            {"type": "function_call", "call_id": "c1", "name": "lookup", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": CANARY},
        ],
    }
    fingerprint = fingerprint_request("r-test", body)
    assert fingerprint.model == "gpt-6.1-sol"
    assert fingerprint.reasoning_effort == "high"
    assert fingerprint.item_count == 4
    assert [item.kind for item in fingerprint.items] == [
        "message:user",
        "reasoning",
        "function_call",
        "function_call_output",
    ]
    assert fingerprint.exact_kind_bytes("function_call_output") > 0
    assert fingerprint.exact_kind_bytes("function_call") < fingerprint.exact_kind_bytes("function_call_output")
    assert fingerprint.tools_count == 1
    assert fingerprint.tools_bytes > 0
    assert fingerprint.instructions_sha16 is not None
    # No log field may carry raw content.
    event_json = json.dumps(
        {
            "instructions_sha16": fingerprint.instructions_sha16,
            "items": [item.as_list() for item in fingerprint.items],
        }
    )
    assert CANARY not in event_json
    assert all(len(item.sha16) == 16 for item in fingerprint.items)


def test_recent_requests_finds_longest_common_prefix_and_first_difference():
    recent = RecentRequests()
    base = fingerprint_request(
        "r-1",
        {
            "instructions": "rules",
            "input": [
                {"role": "user", "content": "one"},
                {"role": "user", "content": "two"},
            ],
        },
    )
    recent.add(base)

    extension = fingerprint_request(
        "r-2",
        {
            "instructions": "rules",
            "input": [
                {"role": "user", "content": "one"},
                {"role": "user", "content": "two"},
                {"role": "assistant", "content": "three"},
            ],
        },
    )
    comparison = recent.compare(extension)
    assert comparison["previous"]["common_items"] == 2
    assert comparison["previous"]["extension"] is True
    assert comparison["previous"]["first_diff"] is None

    mutated = fingerprint_request(
        "r-3",
        {
            "instructions": "rules",
            "input": [
                {"role": "user", "content": "one"},
                {"role": "user", "content": "TWO CHANGED"},
                {"role": "assistant", "content": "three"},
            ],
        },
    )
    comparison = recent.compare(mutated)
    assert comparison["previous"]["common_items"] == 1
    assert comparison["previous"]["first_diff"] == 1
    assert comparison["previous"]["extension"] is False


def test_prompt_cache_key_is_stable_across_appends_and_unique_per_conversation():
    first = {"role": "user", "content": "conversation start"}
    body_a1 = {"model": "gpt-6.1-sol", "input": [first]}
    body_a2 = {
        "model": "gpt-6.1-sol",
        "input": [first, {"type": "function_call_output", "call_id": "c1", "output": "x"}],
    }
    body_b = {"model": "gpt-6.1-sol", "input": [{"role": "user", "content": "other start"}]}
    assert build_prompt_cache_key(body_a1) == build_prompt_cache_key(body_a2)
    assert build_prompt_cache_key(body_a1) != build_prompt_cache_key(body_b)
    assert build_prompt_cache_key({"model": "gpt-6.1-sol", "input": []}) is None


def test_apply_cache_routing_key_respects_client_value_and_disable_flag():
    body = {"model": "gpt-6.1-sol", "input": [{"role": "user", "content": "hi"}]}
    updated, source = apply_cache_routing_key(dict(body, prompt_cache_key="mine"), enabled=True)
    assert source == "client"
    assert updated["prompt_cache_key"] == "mine"

    updated, source = apply_cache_routing_key(body, enabled=False)
    assert source == "disabled"
    assert "prompt_cache_key" not in updated

    updated, source = apply_cache_routing_key({"model": "gpt-6.1-sol", "input": []}, enabled=True)
    assert source == "absent"


def test_request_telemetry_event_contains_only_safe_metadata(tmp_path):
    settings_value = settings(tmp_path)
    recent = RecentRequests()
    body = {
        "model": "gpt-6.1-sol",
        "instructions": CANARY,
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": CANARY}]}],
        "stream": True,
        "store": False,
    }
    with captured_events() as records:
        telemetry = RequestTelemetry(settings_value, body, cache_key_source="generated", recent=recent)
        telemetry.note_stream_requested(True)
        telemetry.note_upstream_status(200, {"x-ratelimit-remaining-tokens": "42", "authorization": "Bearer secret"})
        telemetry.observe_chunk(_completed_body())
        telemetry.finish()

    assert len(records) == 1
    event = json.loads(records[0])
    assert event["event"] == "siwc.request"
    assert event["model"] == "gpt-6.1-sol"
    assert event["upstream_status"] == 200
    assert event["cache_routing"] == "generated"
    assert event["response_id"] == "resp-test"
    assert event["usage"]["cached_tokens"] == 800
    assert event["rate_limit"] == {"x-ratelimit-remaining-tokens": "42"}
    assert CANARY not in records[0]
    assert "Bearer" not in records[0]


def test_rejected_request_telemetry_records_safe_error_code(tmp_path):
    settings_value = settings(tmp_path)
    with captured_events() as records:
        telemetry = RequestTelemetry(settings_value, {"model": "m", "input": [{"role": "user", "content": "x"}]}, "generated", RecentRequests())
        telemetry.note_upstream_status(400, None)
        telemetry.note_error_body(
            json.dumps(
                {
                    "error": {
                        "type": "invalid_request_error",
                        "code": "subscription_sharing_unsupported_capability",
                        "param": "prompt_cache_key",
                        "message": CANARY,
                    }
                }
            ).encode()
        )
        telemetry.finish()
    event = json.loads(records[0])
    assert event["error_code"] == "subscription_sharing_unsupported_capability"
    assert event["error_param"] == "prompt_cache_key"
    assert CANARY not in records[0]


def test_telemetry_can_be_disabled(tmp_path):
    settings_value = settings(tmp_path, REQUEST_TELEMETRY=False)
    with captured_events() as records:
        telemetry = RequestTelemetry(
            settings_value,
            {"model": "m", "input": [{"role": "user", "content": "x"}]},
            "generated",
            RecentRequests(),
        )
        telemetry.finish()
    assert records == []


def test_api_request_emits_one_event_per_inbound_request_without_content(monkeypatch, tmp_path):
    FakeAsyncClient.sent = []
    FakeAsyncClient.send_response = FakeUpstream(
        body=_completed_body(text=CANARY),
        headers={"x-ratelimit-remaining-tokens": "7", "set-cookie": "secret"},
    )
    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)
    with captured_events() as records, TestClient(create_app(settings(tmp_path), StubStore())) as client:
        response = client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "model": "gpt-6.1-sol",
                "instructions": CANARY,
                "input": [{"role": "user", "content": CANARY}],
                "stream": True,
            },
        )
        assert response.status_code == 200
        assert CANARY in response.text  # relay is untouched
    events = [json.loads(record) for record in records]
    assert len(events) == 1
    event = events[0]
    assert event["stream_requested"] is True
    assert event["upstream_dispatched"] is True
    assert event["usage"]["cached_tokens"] == 800
    assert event["items"][0][0] == "message:user"
    assert not any(CANARY in record or "secret" in record for record in records)


def test_non_streaming_api_request_emits_event_after_completion(monkeypatch, tmp_path):
    FakeAsyncClient.sent = []
    completed = {
        "id": "resp-ns",
        "object": "response",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": CANARY}],
            }
        ],
    }
    body = (
        b'event: response.created\ndata: {"type":"response.created","response":{"id":"resp-ns"}}\n\n'
        + b"event: response.completed\ndata: "
        + json.dumps(
            {
                "type": "response.completed",
                "response": completed,
            }
        ).encode()
        + b"\n\n"
    )
    FakeAsyncClient.send_response = FakeUpstream(body=body)
    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)
    with captured_events() as records, TestClient(create_app(settings(tmp_path), StubStore())) as client:
        response = client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt-6.1-sol", "input": [{"role": "user", "content": CANARY}], "stream": False},
        )
        assert response.status_code == 200
    events = [json.loads(record) for record in records]
    assert len(events) == 1
    assert events[0]["stream_requested"] is False
    assert events[0]["response_id"] == "resp-ns"
    assert not any(CANARY in record for record in records)


def test_summarize_events_aggregates_cache_ratios():
    summary = summarize_events(
        [
            {"model": "gpt-6.1-sol", "usage": {"input_tokens": 100, "cached_tokens": 80, "output_tokens": 5}},
            {"model": "gpt-6.1-sol", "usage": {"input_tokens": 100, "cached_tokens": 20, "output_tokens": 5}},
        ]
    )
    assert summary["calls"] == 2
    assert summary["input_tokens"] == 200
    assert summary["cached_tokens"] == 100
    assert summary["cache_hit_ratio"] == 0.5
