"""Transport tests against an in-process fake Responses WebSocket server.

The fake server mimics the live SIWC route: it uses the implicit default lane,
emits internal transport events (``codex.response.metadata`` and friends) that
must not be relayed, and can script errors, disconnects and connection-limit
failures.
"""
from __future__ import annotations

import asyncio
import json

from ws_fakes import (
    FakeResponsesServer,
    StubStore,
    completed_events,
    function_call_item,
    message_item,
    ws_settings,
)

from app.telemetry import RecentRequests, RequestTelemetry
from app.ws_transport import WebSocketTransport, WsStreamError, WsUnavailable


class settings_like:
    REQUEST_TELEMETRY = False


def run(coro):
    return asyncio.run(coro)


def build_transport(tmp_path, server: FakeResponsesServer, monkeypatch, store: StubStore | None = None, **overrides):
    monkeypatch.setattr("app.ws_transport.WS_URL", server.url)
    store = store or StubStore()
    config = ws_settings(tmp_path, **overrides)
    transport = WebSocketTransport(config, store)
    return transport, store, config


def make_report(body: dict) -> tuple[dict, RequestTelemetry]:
    telemetry = RequestTelemetry(settings_like(), body, cache_key_source="disabled", recent=RecentRequests())
    return {}, telemetry


def test_first_request_streams_full_context_and_relays_only_public_events(tmp_path, monkeypatch):
    server = FakeResponsesServer(lambda frame, index, srv: completed_events("resp-1", [message_item("hello")]))
    server.start()
    body = {"model": "gpt-6.1-sol", "store": False, "input": [message_item("hi", "user")]}
    try:
        async def scenario():
            transport, _, _ = build_transport(tmp_path, server, monkeypatch)
            report, telemetry = make_report(body)
            session = await transport.begin(
                body=body, conversation_key="h:test", fingerprint=telemetry.fingerprint, report=report
            )
            events = [event async for event in session.stream()]
            await transport.aclose()
            return events, report

        events, report = run(scenario())
    finally:
        server.stop()

    assert [event["type"] for event in events] == [
        "response.created",
        "response.in_progress",
        "response.output_item.done",
        "response.completed",
    ]
    assert report["mode"] == "full"
    assert report["continuation"] is False
    assert report["response_id"] == "resp-1"
    assert report["received_items"] == 1
    assert report["sent_items"] == 1
    frame = server.frames[0]
    assert frame["type"] == "response.create"
    assert "stream" not in frame
    assert "stream_id" not in frame
    assert "previous_response_id" not in frame
    assert frame["input"] == body["input"]
    assert frame["store"] is False


def test_second_round_continues_with_previous_response_id_and_only_new_items(tmp_path, monkeypatch):
    def responder(frame, index, srv):
        if index == 0:
            return completed_events("resp-1", [message_item("all good")])
        return completed_events("resp-2", [message_item("still good")])

    server = FakeResponsesServer(responder)
    server.start()
    try:
        async def scenario():
            transport, _, _ = build_transport(tmp_path, server, monkeypatch)
            first_input = [message_item("status?", "user")]
            body_one = {"model": "gpt-6.1-sol", "store": False, "input": first_input}
            report_one = {}
            session = await transport.begin(
                body=body_one, conversation_key="h:test",
                fingerprint=RequestTelemetry(settings_like(), body_one, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                report=report_one,
            )
            [event async for event in session.stream()]

            # Open WebUI replays the assistant message and appends the next
            # user message; only the new user message may be sent upstream.
            second_input = [*first_input, message_item("all good"), message_item("and now?", "user")]
            body_two = {"model": "gpt-6.1-sol", "store": False, "input": second_input}
            report_two = {}
            session = await transport.begin(
                body=body_two, conversation_key="h:test",
                fingerprint=RequestTelemetry(settings_like(), body_two, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                report=report_two,
            )
            [event async for event in session.stream()]
            await transport.aclose()
            return report_one, report_two

        report_one, report_two = run(scenario())
    finally:
        server.stop()

    assert report_one["mode"] == "full"
    assert report_two["mode"] == "continuation"
    assert report_two["continuation"] is True
    assert report_two["previous_response_used"] is True
    assert report_two["stripped_items"] == 1
    assert report_two["sent_items"] == 1
    assert report_two["received_items"] == 3
    assert report_two["sent_bytes"] < report_two["received_bytes"]

    second_frame = server.frames[1]
    assert second_frame["previous_response_id"] == "resp-1"
    assert second_frame["input"] == [message_item("and now?", "user")]


def test_tool_loop_sends_only_the_new_tool_output(tmp_path, monkeypatch):
    tools = [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}]

    def responder(frame, index, srv):
        if index == 0:
            return completed_events("resp-1", [function_call_item("call-77")])
        return completed_events("resp-2", [message_item("done")])

    server = FakeResponsesServer(responder)
    server.start()
    try:
        async def scenario():
            transport, _, _ = build_transport(tmp_path, server, monkeypatch)
            first_input = [message_item("check the service", "user")]
            body_one = {"model": "gpt-6.1-sol", "store": False, "input": first_input, "tools": tools}
            report = {}
            session = await transport.begin(
                body=body_one, conversation_key="h:test",
                fingerprint=RequestTelemetry(settings_like(), body_one, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                report=report,
            )
            [event async for event in session.stream()]

            tool_result = {"type": "function_call_output", "call_id": "call-77", "output": "y" * 5000}
            second_input = [*first_input, function_call_item("call-77"), tool_result]
            body_two = {"model": "gpt-6.1-sol", "store": False, "input": second_input, "tools": tools}
            report_two = {}
            session = await transport.begin(
                body=body_two, conversation_key="h:test",
                fingerprint=RequestTelemetry(settings_like(), body_two, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                report=report_two,
            )
            [event async for event in session.stream()]
            await transport.aclose()
            return report_two

        report = run(scenario())
    finally:
        server.stop()

    assert report["mode"] == "continuation"
    assert report["sent_items"] == 1
    assert report["sent_tool_bytes"] >= 5000
    assert report["received_tool_bytes"] >= 5000
    frame = server.frames[1]
    assert frame["input"] == [{"type": "function_call_output", "call_id": "call-77", "output": "y" * 5000}]
    assert frame["tools"] == tools


def test_invalid_previous_response_retries_once_with_full_context(tmp_path, monkeypatch):
    def responder(frame, index, srv):
        if index == 0:
            return completed_events("resp-1", [message_item("first")])
        if frame.get("previous_response_id"):
            return [
                {
                    "type": "error",
                    "status": 400,
                    "error": {
                        "type": "invalid_request_error",
                        "code": None,
                        "message": "Invalid `previous_response_id`.",
                    },
                }
            ]
        return completed_events("resp-2", [message_item("recovered")])

    server = FakeResponsesServer(responder)
    server.start()
    first_input = [message_item("hello", "user")]
    second_input = [*first_input, message_item("first"), message_item("more", "user")]
    try:
        async def scenario():
            transport, _, _ = build_transport(tmp_path, server, monkeypatch)
            body_one = {"model": "gpt-6.1-sol", "store": False, "input": first_input}
            session = await transport.begin(
                body=body_one, conversation_key="h:test",
                fingerprint=RequestTelemetry(settings_like(), body_one, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                report={},
            )
            [event async for event in session.stream()]

            body_two = {"model": "gpt-6.1-sol", "store": False, "input": second_input}
            report = {}
            session = await transport.begin(
                body=body_two, conversation_key="h:test",
                fingerprint=RequestTelemetry(settings_like(), body_two, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                report=report,
            )
            events = [event async for event in session.stream()]
            await transport.aclose()
            return report, events

        report, events = run(scenario())
    finally:
        server.stop()

    assert report["mode"] == "full"
    assert report["previous_response_used"] is False
    assert report["fallback_reason"] == "previous_response_invalid"
    assert report["response_id"] == "resp-2"
    assert events[-1]["type"] == "response.completed"
    # Third frame is the full-context retry after the rejected continuation.
    assert len(server.frames) == 3
    assert server.frames[2]["input"] == second_input
    assert "previous_response_id" not in server.frames[2]


def test_error_event_is_returned_safely_without_retry(tmp_path, monkeypatch):
    def responder(frame, index, srv):
        return [
            {
                "type": "error",
                "status": 429,
                "error": {
                    "type": "rate_limit_error",
                    "code": "subscription_sharing_usage_limit_exceeded",
                    "message": "plan quota exhausted for this account token abc",
                },
            }
        ]

    server = FakeResponsesServer(responder)
    server.start()
    try:
        async def scenario():
            transport, _, _ = build_transport(tmp_path, server, monkeypatch)
            body = {"model": "gpt-6.1-sol", "store": False, "input": [message_item("hi", "user")]}
            report = {}
            session = await transport.begin(
                body=body, conversation_key="h:test",
                fingerprint=RequestTelemetry(settings_like(), body, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                report=report,
            )
            await transport.aclose()
            return session

        session = run(scenario())
    finally:
        server.stop()

    assert session.error is not None
    assert session.error_status == 429
    assert session.error["code"] == "subscription_sharing_usage_limit_exceeded"
    assert "abc" not in json.dumps(session.error)
    assert len(server.frames) == 1  # quota errors never retry


def test_connection_close_mid_stream_raises_stream_error(tmp_path, monkeypatch):
    server = FakeResponsesServer(
        lambda frame, index, srv: ([{"type": "response.created", "response": {"id": "resp-1"}}], True)
    )
    server.start()
    try:
        async def scenario():
            transport, _, _ = build_transport(tmp_path, server, monkeypatch)
            body = {"model": "gpt-6.1-sol", "store": False, "input": [message_item("hi", "user")]}
            report = {}
            session = await transport.begin(
                body=body, conversation_key="h:test",
                fingerprint=RequestTelemetry(settings_like(), body, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                report=report,
            )
            events = []
            error = None
            try:
                async for event in session.stream():
                    events.append(event)
            except WsStreamError as exc:
                error = exc
            await transport.aclose()
            return events, error, report

        events, error, report = run(scenario())
    finally:
        server.stop()

    assert [event["type"] for event in events] == ["response.created"]
    assert error is not None
    assert report["terminal_event"] is None


def test_close_before_first_event_reconnects_and_replays_full_context(tmp_path, monkeypatch):
    def responder(frame, index, srv):
        if len(srv.frames) == 2:
            return None  # drop the socket before answering the second request
        return completed_events(f"resp-{len(srv.frames)}", [message_item("ok")])

    server = FakeResponsesServer(responder)
    server.start()
    first_input = [message_item("hello", "user")]
    second_input = [*first_input, message_item("ok"), message_item("more", "user")]
    try:
        async def scenario():
            transport, _, _ = build_transport(tmp_path, server, monkeypatch)
            body_one = {"model": "gpt-6.1-sol", "store": False, "input": first_input}
            session = await transport.begin(
                body=body_one, conversation_key="h:test",
                fingerprint=RequestTelemetry(settings_like(), body_one, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                report={},
            )
            [event async for event in session.stream()]

            body_two = {"model": "gpt-6.1-sol", "store": False, "input": second_input}
            report = {}
            session = await transport.begin(
                body=body_two, conversation_key="h:test",
                fingerprint=RequestTelemetry(settings_like(), body_two, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                report=report,
            )
            [event async for event in session.stream()]
            await transport.aclose()
            return report

        report = run(scenario())
    finally:
        server.stop()

    assert report["mode"] == "full"
    assert report["fallback_reason"] == "reconnected"
    assert report["previous_response_used"] is False
    assert report["reconnects"] == 1
    assert server.connection_count == 2
    assert "previous_response_id" not in server.frames[-1]
    assert server.frames[-1]["input"] == second_input


def test_connection_limit_error_reconnects_once(tmp_path, monkeypatch):
    def responder(frame, index, srv):
        if srv.connection_count == 1:
            return [
                {
                    "type": "error",
                    "status": 400,
                    "error": {
                        "type": "invalid_request_error",
                        "code": "websocket_connection_limit_reached",
                        "message": "connection limit reached (60 minutes)",
                    },
                }
            ]
        return completed_events("resp-2", [message_item("ok")])

    server = FakeResponsesServer(responder)
    server.start()
    try:
        async def scenario():
            transport, _, _ = build_transport(tmp_path, server, monkeypatch)
            body = {"model": "gpt-6.1-sol", "store": False, "input": [message_item("hi", "user")]}
            report = {}
            session = await transport.begin(
                body=body, conversation_key="h:test",
                fingerprint=RequestTelemetry(settings_like(), body, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                report=report,
            )
            events = [event async for event in session.stream()]
            await transport.aclose()
            return report, events

        report, events = run(scenario())
    finally:
        server.stop()

    assert report["reconnects"] == 1
    assert events[-1]["type"] == "response.completed"
    assert report["response_id"] == "resp-2"
    assert server.connection_count == 2


def test_reconnection_invalidates_lane_and_next_round_is_full_context(tmp_path, monkeypatch):
    def responder(frame, index, srv):
        return completed_events(f"resp-{srv.connection_count}-{index}", [message_item("ok")])

    server = FakeResponsesServer(responder)
    server.start()
    try:
        async def scenario():
            transport, _, _ = build_transport(tmp_path, server, monkeypatch)
            first_input = [message_item("hello", "user")]
            body_one = {"model": "gpt-6.1-sol", "store": False, "input": first_input}
            session = await transport.begin(
                body=body_one, conversation_key="h:test",
                fingerprint=RequestTelemetry(settings_like(), body_one, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                report={},
            )
            [event async for event in session.stream()]

            # Force a reconnect (token rotation) the way the credential store
            # would after an OAuth refresh.
            await transport.reconnect(transport._connection, {})

            second_input = [*first_input, message_item("ok"), message_item("more", "user")]
            body_two = {"model": "gpt-6.1-sol", "store": False, "input": second_input}
            report = {}
            session = await transport.begin(
                body=body_two, conversation_key="h:test",
                fingerprint=RequestTelemetry(settings_like(), body_two, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                report=report,
            )
            [event async for event in session.stream()]
            await transport.aclose()
            return report

        report = run(scenario())
    finally:
        server.stop()

    assert report["mode"] == "full"
    assert report["fallback_reason"] == "connection_changed"
    assert "previous_response_id" not in server.frames[-1]


def test_token_rotation_reconnects_before_the_next_request(tmp_path, monkeypatch):
    server = FakeResponsesServer(lambda frame, index, srv: completed_events("resp-1", [message_item("ok")]))
    server.start()
    try:
        async def scenario():
            store = StubStore()
            transport, _, _ = build_transport(tmp_path, server, monkeypatch, store=store)
            body = {"model": "gpt-6.1-sol", "store": False, "input": [message_item("hi", "user")]}
            for _ in range(2):
                session = await transport.begin(
                    body=body, conversation_key="h:test",
                    fingerprint=RequestTelemetry(settings_like(), body, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                    report={},
                )
                [event async for event in session.stream()]
                if store.token == "chatgpt-access-token":
                    store.token = "rotated-access-token"
            await transport.aclose()

        run(scenario())
    finally:
        server.stop()

    assert server.connection_count == 2


def test_two_conversations_keep_separate_lanes(tmp_path, monkeypatch):
    server = FakeResponsesServer(lambda frame, index, srv: completed_events(f"resp-{index}", [message_item("ok")]))
    server.start()
    try:
        async def scenario():
            transport, _, _ = build_transport(tmp_path, server, monkeypatch)
            bodies = []
            for key in ("h:alpha", "h:beta"):
                body = {"model": "gpt-6.1-sol", "store": False, "input": [message_item(f"{key} hello", "user")]}
                session = await transport.begin(
                    body=body, conversation_key=key,
                    fingerprint=RequestTelemetry(settings_like(), body, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                    report={},
                )
                [event async for event in session.stream()]
                bodies.append(body)
            reports = []
            for key, body in zip(("h:alpha", "h:beta"), bodies):
                extended = {"model": "gpt-6.1-sol", "store": False, "input": [*body["input"], message_item("ok"), message_item(f"{key} more", "user")]}
                report = {}
                session = await transport.begin(
                    body=extended, conversation_key=key,
                    fingerprint=RequestTelemetry(settings_like(), extended, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                    report=report,
                )
                [event async for event in session.stream()]
                reports.append(report)
            await transport.aclose()
            return reports

        reports = run(scenario())
    finally:
        server.stop()

    assert [report["mode"] for report in reports] == ["continuation", "continuation"]
    first_keyed = server.frames[2]["previous_response_id"]
    second_keyed = server.frames[3]["previous_response_id"]
    assert first_keyed != second_keyed
    assert server.frames[2]["input"] == [message_item("h:alpha more", "user")]
    assert server.frames[3]["input"] == [message_item("h:beta more", "user")]


def test_auto_correlation_continues_only_proven_extensions(tmp_path, monkeypatch):
    server = FakeResponsesServer(lambda frame, index, srv: completed_events(f"resp-{index}", [message_item("ok")]))
    server.start()
    try:
        async def scenario():
            transport, _, _ = build_transport(tmp_path, server, monkeypatch)
            first = {"model": "gpt-6.1-sol", "store": False, "input": [message_item("audit", "user")]}
            session = await transport.begin(
                body=first, conversation_key=None,
                fingerprint=RequestTelemetry(settings_like(), first, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                report={},
            )
            [event async for event in session.stream()]

            # A different conversation with unrelated content must not inherit
            # the first chain.
            unrelated = {"model": "gpt-6.1-sol", "store": False, "input": [message_item("totally different", "user")]}
            report_unrelated = {}
            session = await transport.begin(
                body=unrelated, conversation_key=None,
                fingerprint=RequestTelemetry(settings_like(), unrelated, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                report=report_unrelated,
            )
            [event async for event in session.stream()]

            # A proven extension of the first request continues its chain.
            extended = {"model": "gpt-6.1-sol", "store": False, "input": [*first["input"], message_item("ok"), message_item("more", "user")]}
            report_extended = {}
            session = await transport.begin(
                body=extended, conversation_key=None,
                fingerprint=RequestTelemetry(settings_like(), extended, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                report=report_extended,
            )
            [event async for event in session.stream()]
            await transport.aclose()
            return report_unrelated, report_extended

        report_unrelated, report_extended = run(scenario())
    finally:
        server.stop()

    assert report_unrelated["mode"] == "full"
    assert report_unrelated["fallback_reason"] == "no_lane"
    assert report_extended["mode"] == "continuation"
    assert server.frames[2]["previous_response_id"] == "resp-0"


def test_concurrent_requests_serialize_on_the_single_connection(tmp_path, monkeypatch):
    order: list[str] = []

    async def responder(frame, index, srv):
        order.append(f"start-{frame['input'][0]['content'][0]['text']}")
        await asyncio.sleep(0.05)
        order.append(f"end-{frame['input'][0]['content'][0]['text']}")
        return completed_events(f"resp-{index}", [message_item("ok")])

    def sync_responder(frame, index, srv):
        return None

    server = FakeResponsesServer(sync_responder)
    server.responder = lambda frame, index, srv: None  # replaced below
    server.start()

    async def async_responder(frame, index, srv):
        return await responder(frame, index, srv)

    server.responder = lambda frame, index, srv: [  # synchronous contract
        {"type": "response.created", "response": {"id": f"resp-{index}"}},
        {"type": "response.output_item.done", "output_index": 0, "item": message_item("ok")},
        {"type": "response.completed", "response": {"id": f"resp-{index}", "status": "completed", "output": []}},
    ]
    try:
        async def scenario():
            transport, _, _ = build_transport(tmp_path, server, monkeypatch)
            bodies = [
                {"model": "gpt-6.1-sol", "store": False, "input": [message_item("one", "user")]},
                {"model": "gpt-6.1-sol", "store": False, "input": [message_item("two", "user")]},
            ]

            async def send(body):
                session = await transport.begin(
                    body=body, conversation_key=f"h:{body['input'][0]['content'][0]['text']}",
                    fingerprint=RequestTelemetry(settings_like(), body, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                    report={},
                )
                return [event async for event in session.stream()]

            await asyncio.gather(send(bodies[0]), send(bodies[1]))
            await transport.aclose()

        run(scenario())
    finally:
        server.stop()

    assert len(server.frames) == 2
    assert {frame["input"][0]["content"][0]["text"] for frame in server.frames} == {"one", "two"}


def test_establish_failure_is_reported_as_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr("app.ws_transport.WS_URL", "ws://127.0.0.1:1/v1/responses")

    async def scenario():
        transport = WebSocketTransport(ws_settings(tmp_path), StubStore())
        body = {"model": "gpt-6.1-sol", "store": False, "input": [message_item("hi", "user")]}
        try:
            await transport.begin(
                body=body, conversation_key="h:test",
                fingerprint=RequestTelemetry(settings_like(), body, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                report={},
            )
        except WsUnavailable as exc:
            return str(exc)
        return None

    reason = run(scenario())
    assert reason is not None
    assert reason.startswith("websocket_")


def test_lane_state_holds_no_conversation_content(tmp_path, monkeypatch):
    server = FakeResponsesServer(lambda frame, index, srv: completed_events("resp-1", [message_item("secret answer")]))
    server.start()
    try:
        async def scenario():
            transport, _, _ = build_transport(tmp_path, server, monkeypatch)
            body = {"model": "gpt-6.1-sol", "store": False, "input": [message_item("secret question", "user")]}
            session = await transport.begin(
                body=body, conversation_key="h:test",
                fingerprint=RequestTelemetry(settings_like(), body, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                report={},
            )
            [event async for event in session.stream()]
            lane = transport.lanes["h:test"]
            await transport.aclose()
            return lane

        lane = run(scenario())
    finally:
        server.stop()

    rendered = repr(lane)
    assert "secret question" not in rendered
    assert "secret answer" not in rendered
    assert lane.response_id == "resp-1"
    assert lane.item_count == 1
    assert all(len(digest) == 16 for digest in lane.item_hashes)


def test_queued_request_is_planned_against_the_fresh_lane_state(tmp_path, monkeypatch):
    """A request that waits for the connection lock must not reuse a stale plan."""
    def responder(frame, index, srv):
        return completed_events(f"resp-{index}", [message_item("ok")])

    server = FakeResponsesServer(responder)
    server.start()
    first_input = [message_item("one", "user")]
    second_input = [*first_input, message_item("ok"), message_item("two", "user")]
    third_input = [*second_input, message_item("ok"), message_item("three", "user")]
    try:
        async def scenario():
            transport, _, _ = build_transport(tmp_path, server, monkeypatch)

            async def run(body):
                session = await transport.begin(
                    body=body, conversation_key="h:test",
                    fingerprint=RequestTelemetry(settings_like(), body, cache_key_source="disabled", recent=RecentRequests()).fingerprint,
                    report={},
                )
                return [event async for event in session.stream()]

            await run({"model": "gpt-6.1-sol", "store": False, "input": first_input})

            # Start the second request, then queue a third one on the same
            # conversation while the second still owns the connection lock. The
            # queued request must be planned from the lane state the second
            # request leaves behind, so it transmits only its newest item.
            second = asyncio.create_task(run({"model": "gpt-6.1-sol", "store": False, "input": second_input}))
            await asyncio.sleep(0.2)
            third = asyncio.create_task(run({"model": "gpt-6.1-sol", "store": False, "input": third_input}))
            await asyncio.gather(second, third)
            await transport.aclose()

        run(scenario())
    finally:
        server.stop()

    assert len(server.frames) == 3
    assert server.frames[1]["input"] == [message_item("two", "user")]
    assert server.frames[2]["previous_response_id"] == "resp-1"
    assert server.frames[2]["input"] == [message_item("three", "user")]
