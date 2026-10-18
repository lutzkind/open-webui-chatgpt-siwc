"""Shared fakes for WebSocket transport tests.

``FakeResponsesServer`` runs a scripted Responses protocol server in a
background thread so both plain transport tests and ``TestClient``-driven
adapter tests (which run the app in their own event loop) can talk to it.
"""
from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable

from cryptography.fernet import Fernet
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from app.config import Settings

API_KEY = "test-adapter-key-" + "0" * 32
HOST_ID = "00000000-0000-4000-8000-000000000001"
ORIGIN = "https://open-webui.example.test"


class StubStore:
    def __init__(self, token: str = "chatgpt-access-token") -> None:
        self.token = token
        self.refreshes = 0

    def is_configured(self) -> bool:
        return True

    def load(self) -> dict:
        return {"access_token": self.token}

    async def access_token(self) -> str:
        return self.token


def settings_values(tmp_path, **overrides) -> dict:
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
    return values


def ws_settings(tmp_path, **overrides) -> Settings:
    values = settings_values(tmp_path, RESPONSES_TRANSPORT="websocket")
    values.update(
        {
            "WS_EVENT_TIMEOUT_SECONDS": 5.0,
            "WS_LOCK_TIMEOUT_SECONDS": 5.0,
            "WS_OPEN_TIMEOUT_SECONDS": 5.0,
        }
    )
    values.update(overrides)
    return Settings(**values)


def message_item(text: str, role: str = "assistant") -> dict:
    return {
        "type": "message",
        "role": role,
        "content": [{"type": "output_text" if role == "assistant" else "input_text", "text": text}],
    }


def function_call_item(call_id: str = "call-1", name: str = "lookup") -> dict:
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": "{}", "id": f"fc_{call_id}"}


def completed_events(response_id: str, items: list[dict], *, usage: dict | None = None) -> list[dict]:
    events: list[dict] = [
        {"type": "codex.response.metadata", "headers": {"x-codex-turn-state": "opaque"}},
        {"type": "response.created", "response": {"id": response_id, "status": "in_progress"}},
        {"type": "response.in_progress", "response": {"id": response_id, "status": "in_progress"}},
    ]
    for index, item in enumerate(items):
        events.append({"type": "response.output_item.done", "output_index": index, "item": item})
    events.append({"type": "response.metadata", "response_id": response_id, "metadata": {"moderation": "internal"}})
    events.append(
        {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "status": "completed",
                "output": [],
                "usage": usage or {"input_tokens": 10, "output_tokens": 4},
            },
        }
    )
    return events


class FakeResponsesServer:
    """Thread-backed WebSocket server speaking the Responses event protocol."""

    def __init__(self, responder: Callable[[dict, int, FakeResponsesServer], list[dict] | None]) -> None:
        self.responder = responder
        self.frames: list[dict] = []
        self.connection_count = 0
        self.port = 0
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/v1/responses"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(timeout=10)

    def _run(self) -> None:
        asyncio.run(self._serve())

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        async with serve(self._handle, "127.0.0.1", 0) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop.wait()

    async def _handle(self, ws) -> None:
        self.connection_count += 1
        index = 0
        try:
            while True:
                try:
                    raw = await ws.recv()
                except ConnectionClosed:
                    return
                frame = json.loads(raw)
                self.frames.append(frame)
                scripted = self.responder(frame, index, self)
                index += 1
                if scripted is None:
                    await ws.close()
                    return
                events, close_after = scripted if isinstance(scripted, tuple) else (scripted, False)
                for event in events:
                    await ws.send(json.dumps(event))
                if close_after:
                    await ws.close()
                    return
        except ConnectionClosed:
            return

    def stop(self) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout=5)
