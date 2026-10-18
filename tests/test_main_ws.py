"""Adapter-level tests for the WebSocket transport boundary.

These exercise ``POST /v1/responses`` through ``TestClient`` with a real
scripted WebSocket server, covering streaming, non-streaming, incremental
continuation, error mapping and the bounded HTTP fallback.
"""
from __future__ import annotations

import json
import logging
from typing import ClassVar

from fastapi.testclient import TestClient
from ws_fakes import (
    API_KEY,
    FakeResponsesServer,
    StubStore,
    completed_events,
    function_call_item,
    message_item,
    settings_values,
    ws_settings,
)

from app.main import create_app


def auth() -> dict:
    return {"Authorization": f"Bearer {API_KEY}"}


def conversation(chat_id: str = "chat-1") -> dict:
    return {"X-OpenWebUI-Chat-Id": chat_id}


class FakeUpstream:
    def __init__(self, status_code=200, body=b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp-http","status":"completed","output":[]}}\n\n'):
        self.status_code = status_code
        self.body = body
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
    send_response = None

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def get(self, url, **kwargs):  # pragma: no cover - not used here
        raise AssertionError("model discovery is not exercised in these tests")

    def build_request(self, method, url, **kwargs):
        return {"method": method, "url": url, **kwargs}

    async def send(self, request, stream=False):
        self.sent.append(("SEND", request))
        return self.send_response

    async def aclose(self):
        return None


def test_websocket_transport_streams_sse_and_continues_incrementally(tmp_path, monkeypatch):
    def responder(frame, index, srv):
        if index == 0:
            return completed_events("resp-1", [message_item("all good")])
        return completed_events("resp-2", [message_item("still good")])

    server = FakeResponsesServer(responder)
    server.start()
    monkeypatch.setattr("app.ws_transport.WS_URL", server.url)
    first_input = [message_item("status?", "user")]
    try:
        with TestClient(create_app(ws_settings(tmp_path), StubStore())) as client:
            first = client.post(
                "/v1/responses",
                headers={**auth(), **conversation()},
                json={"model": "gpt-6.1-sol", "input": first_input, "stream": True},
            )
            assert first.status_code == 200
            assert first.headers["content-type"].startswith("text/event-stream")
            assert "response.completed" in first.text
            assert "response.output_item.done" in first.text
            assert "codex.response.metadata" not in first.text
            assert "response.metadata" not in first.text

            second_input = [*first_input, message_item("all good"), message_item("and now?", "user")]
            second = client.post(
                "/v1/responses",
                headers={**auth(), **conversation()},
                json={"model": "gpt-6.1-sol", "input": second_input, "stream": True},
            )
            assert second.status_code == 200
    finally:
        server.stop()

    assert len(server.frames) == 2
    assert server.frames[0].get("previous_response_id") is None
    assert server.frames[0]["input"] == first_input
    assert server.frames[1]["previous_response_id"] == "resp-1"
    assert server.frames[1]["input"] == [message_item("and now?", "user")]
    assert server.frames[1]["store"] is False
    assert "stream_id" not in server.frames[1]


def test_websocket_transport_serves_non_streaming_callers(tmp_path, monkeypatch):
    server = FakeResponsesServer(lambda frame, index, srv: completed_events("resp-ns", [message_item("done")]))
    server.start()
    monkeypatch.setattr("app.ws_transport.WS_URL", server.url)
    try:
        with TestClient(create_app(ws_settings(tmp_path), StubStore())) as client:
            response = client.post(
                "/v1/responses",
                headers={**auth(), **conversation()},
                json={"model": "gpt-6.1-sol", "input": [message_item("hello", "user")], "stream": False},
            )
    finally:
        server.stop()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["status"] == "completed"
    assert body["output"] == [message_item("done")]


def test_websocket_tool_continuation_sends_only_the_new_tool_output(tmp_path, monkeypatch):
    tools = [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}]

    def responder(frame, index, srv):
        if index == 0:
            return completed_events("resp-1", [function_call_item("call-9")])
        return completed_events("resp-2", [message_item("finished")])

    server = FakeResponsesServer(responder)
    server.start()
    monkeypatch.setattr("app.ws_transport.WS_URL", server.url)
    first_input = [message_item("check the service", "user")]
    tool_result = {"type": "function_call_output", "call_id": "call-9", "output": "z" * 4096}
    try:
        with TestClient(create_app(ws_settings(tmp_path), StubStore())) as client:
            client.post(
                "/v1/responses",
                headers={**auth(), **conversation()},
                json={"model": "gpt-6.1-sol", "input": first_input, "tools": tools, "stream": True},
            )
            second_input = [*first_input, function_call_item("call-9"), tool_result]
            response = client.post(
                "/v1/responses",
                headers={**auth(), **conversation()},
                json={"model": "gpt-6.1-sol", "input": second_input, "tools": tools, "stream": True},
            )
    finally:
        server.stop()

    assert response.status_code == 200
    assert server.frames[1]["previous_response_id"] == "resp-1"
    assert server.frames[1]["input"] == [tool_result]
    assert server.frames[1]["tools"] == tools


def test_websocket_error_event_maps_to_json_error_with_status(tmp_path, monkeypatch):
    def responder(frame, index, srv):
        return [
            {
                "type": "error",
                "status": 429,
                "error": {
                    "type": "rate_limit_error",
                    "code": "subscription_sharing_usage_limit_exceeded",
                    "message": "quota exhausted for this account",
                },
            }
        ]

    server = FakeResponsesServer(responder)
    server.start()
    monkeypatch.setattr("app.ws_transport.WS_URL", server.url)
    try:
        with TestClient(create_app(ws_settings(tmp_path), StubStore())) as client:
            response = client.post(
                "/v1/responses",
                headers={**auth(), **conversation()},
                json={"model": "gpt-6.1-sol", "input": [message_item("hello", "user")], "stream": True},
            )
    finally:
        server.stop()

    assert response.status_code == 429
    payload = response.json()
    assert payload["error"]["code"] == "subscription_sharing_usage_limit_exceeded"
    assert payload["error"]["message"] == "OpenAI Responses request was rejected."
    assert "quota exhausted" not in response.text


def test_websocket_mid_stream_close_emits_sse_error_event(tmp_path, monkeypatch):
    server = FakeResponsesServer(
        lambda frame, index, srv: ([{"type": "response.created", "response": {"id": "resp-1"}}], True)
    )
    server.start()
    monkeypatch.setattr("app.ws_transport.WS_URL", server.url)
    try:
        with TestClient(create_app(ws_settings(tmp_path), StubStore())) as client:
            response = client.post(
                "/v1/responses",
                headers={**auth(), **conversation()},
                json={"model": "gpt-6.1-sol", "input": [message_item("hello", "user")], "stream": True},
            )
    finally:
        server.stop()

    assert response.status_code == 200
    assert "response.created" in response.text
    assert "websocket_stream_failed" in response.text
    assert response.text.count("data: ") == 2


def test_websocket_mid_stream_close_returns_502_for_non_streaming_callers(tmp_path, monkeypatch):
    server = FakeResponsesServer(
        lambda frame, index, srv: ([{"type": "response.created", "response": {"id": "resp-1"}}], True)
    )
    server.start()
    monkeypatch.setattr("app.ws_transport.WS_URL", server.url)
    try:
        with TestClient(create_app(ws_settings(tmp_path), StubStore())) as client:
            response = client.post(
                "/v1/responses",
                headers={**auth(), **conversation()},
                json={"model": "gpt-6.1-sol", "input": [message_item("hello", "user")], "stream": False},
            )
    finally:
        server.stop()

    assert response.status_code == 502
    assert response.json()["detail"] == "OpenAI Responses stream ended before completion."


def test_websocket_establish_failure_falls_back_to_http(tmp_path, monkeypatch):
    FakeAsyncClient.sent = []
    FakeAsyncClient.send_response = FakeUpstream()
    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("app.ws_transport.WS_URL", "ws://127.0.0.1:1/v1/responses")
    try:
        with TestClient(create_app(ws_settings(tmp_path), StubStore())) as client:
            response = client.post(
                "/v1/responses",
                headers={**auth(), **conversation()},
                json={"model": "gpt-6.1-sol", "input": [message_item("hello", "user")], "stream": True},
            )
    finally:
        pass

    assert response.status_code == 200
    assert "response.completed" in response.text
    assert [entry[0] for entry in FakeAsyncClient.sent] == ["SEND"]
    _, request = FakeAsyncClient.sent[0]
    assert request["url"] == "https://api.openai.com/v1/responses"
    assert request["json"]["input"] == [message_item("hello", "user")]


def test_http_transport_remains_the_default(tmp_path, monkeypatch):
    FakeAsyncClient.sent = []
    FakeAsyncClient.send_response = FakeUpstream()
    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)
    from app.config import Settings

    values = settings_values(tmp_path)
    del values["DATA_DIR"]
    with TestClient(create_app(Settings(**values, DATA_DIR=tmp_path), StubStore())) as client:
        response = client.post(
            "/v1/responses",
            headers=auth(),
            json={"model": "gpt-6.1-sol", "input": [message_item("hello", "user")], "stream": True},
        )
    assert response.status_code == 200
    assert [entry[0] for entry in FakeAsyncClient.sent] == ["SEND"]


class ListHandler(logging.Handler):
    def __init__(self, sink: list):
        super().__init__()
        self.sink = sink

    def emit(self, record):  # pragma: no cover - trivial
        self.sink.append(record)


def capture_telemetry():
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("siwc.telemetry")
    handler = ListHandler(records)
    logger.addHandler(handler)
    return logger, handler, records


def test_websocket_telemetry_reports_transport_and_wire_reduction(tmp_path, monkeypatch):
    server = FakeResponsesServer(lambda frame, index, srv: completed_events("resp-tel", [message_item("ok")]))
    server.start()
    monkeypatch.setattr("app.ws_transport.WS_URL", server.url)
    large_user_item = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "q" * 4000}],
    }
    try:
        logger, handler, records = capture_telemetry()
        try:
            with TestClient(create_app(ws_settings(tmp_path), StubStore())) as client:
                first = client.post(
                "/v1/responses",
                headers={**auth(), **conversation()},
                    json={"model": "gpt-6.1-sol", "input": [large_user_item], "stream": True},
                )
                assert first.status_code == 200
                next_item = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "next"}]}
                second_input = [large_user_item, message_item("ok"), next_item]
                second = client.post(
                "/v1/responses",
                headers={**auth(), **conversation()},
                    json={"model": "gpt-6.1-sol", "input": second_input, "stream": True},
                )
                assert second.status_code == 200
        finally:
            logger.removeHandler(handler)
    finally:
        server.stop()

    events = [json.loads(record.getMessage()) for record in records]
    assert len(events) == 2
    first_event, second_event = events
    assert first_event["transport"]["kind"] == "websocket"
    assert first_event["transport"]["mode"] == "full"
    assert first_event["transport"]["continuation"] is False
    assert first_event["transport"]["lane"].startswith("h:")
    assert first_event["transport"]["generation"] >= 1

    transport = second_event["transport"]
    assert transport["mode"] == "continuation"
    assert transport["continuation"] is True
    assert transport["previous_response_used"] is True
    assert transport["previous_response_id"] == "resp-tel"
    assert transport["received_items"] == 3
    assert transport["sent_items"] == 1
    assert transport["sent_bytes"] < transport["received_bytes"]
    assert transport["wire_reduction"] > 0.9
    assert transport["generation"] == first_event["transport"]["generation"]
    assert "q" * 50 not in json.dumps(second_event)


def test_websocket_without_conversation_header_starts_a_new_chain_per_history(tmp_path, monkeypatch):
    server = FakeResponsesServer(lambda frame, index, srv: completed_events(f"resp-{index}", [message_item("ok")]))
    server.start()
    monkeypatch.setattr("app.ws_transport.WS_URL", server.url)
    try:
        with TestClient(create_app(ws_settings(tmp_path), StubStore())) as client:
            for text in ("first conversation", "unrelated second conversation"):
                response = client.post(
                    "/v1/responses",
                    headers=auth(),
                    json={"model": "gpt-6.1-sol", "input": [message_item(text, "user")], "stream": True},
                )
                assert response.status_code == 200
    finally:
        server.stop()

    assert len(server.frames) == 2
    assert all(frame.get("previous_response_id") is None for frame in server.frames)


def test_http_transport_records_the_conversation_lane(tmp_path, monkeypatch):
    logger, handler, records = capture_telemetry()
    FakeAsyncClient.sent = []
    FakeAsyncClient.send_response = FakeUpstream()
    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)
    from app.config import Settings

    try:
        with TestClient(create_app(Settings(**settings_values(tmp_path)), StubStore())) as client:
            response = client.post(
                "/v1/responses",
                headers={**auth(), **conversation("chat-with-lane")},
                json={"model": "gpt-6.1-sol", "input": [message_item("hello", "user")], "stream": True},
            )
            assert response.status_code == 200
    finally:
        logger.removeHandler(handler)

    events = [json.loads(record.getMessage()) for record in records]
    assert events[0]["transport"]["kind"] == "http"
    assert events[0]["transport"]["lane"].startswith("h:")
    assert "chat-with-lane" not in json.dumps(events[0])


def test_http_transport_records_a_missing_conversation_lane(tmp_path, monkeypatch):
    logger, handler, records = capture_telemetry()
    FakeAsyncClient.sent = []
    FakeAsyncClient.send_response = FakeUpstream()
    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)
    from app.config import Settings

    try:
        with TestClient(create_app(Settings(**settings_values(tmp_path)), StubStore())) as client:
            response = client.post(
                "/v1/responses",
                headers=auth(),
                json={"model": "gpt-6.1-sol", "input": [message_item("hello", "user")], "stream": True},
            )
            assert response.status_code == 200
    finally:
        logger.removeHandler(handler)

    events = [json.loads(record.getMessage()) for record in records]
    assert events[0]["transport"] == {"kind": "http", "lane": None}
