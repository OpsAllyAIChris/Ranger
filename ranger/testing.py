"""Things that let the real code be verified without the real world.

A provider that never calls the network, and the smallest websocket client
that can hold a conversation. Lets the core be verified end to end without an
API key and without spending anything. Tier 1's verification leans on this;
every later tier should too.

The client lives here rather than in a test module because two suites need it,
and one test module importing another is what broke collection under bare
pytest at Tier 1.
"""

from __future__ import annotations

import base64
import json
import os
import socket
from typing import Any, AsyncIterator

from .provider import Completion, ProviderEvent, TextChunk, ToolRequest
from .wsframe import TEXT, accept_key, client_frame, read_message


class ScriptedProvider:
    """Replays a list of scripted responses, one per model round."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def stream(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        self.calls.append({"system": system, "messages": [*messages], "tools": tools})

        if not self.script:
            yield TextChunk("")
            yield Completion(stop_reason="end_turn", text="", content=[])
            return

        step = self.script.pop(0)
        text = step.get("text", "")
        requests = [
            ToolRequest(id=f"toolu_{index}", name=call["name"], input=call.get("input", {}))
            for index, call in enumerate(step.get("tools", []))
        ]

        for chunk in _chunks(text):
            yield TextChunk(chunk)

        content: list[dict[str, Any]] = []
        if text:
            content.append({"type": "text", "text": text})
        for request in requests:
            content.append(
                {
                    "type": "tool_use",
                    "id": request.id,
                    "name": request.name,
                    "input": request.input,
                }
            )

        yield Completion(
            stop_reason="tool_use" if requests else "end_turn",
            text=text,
            tool_requests=tuple(requests),
            content=content,
            usage={"input_tokens": 10, "output_tokens": len(text) // 4},
        )


def _chunks(text: str, size: int = 12) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] if text else []


class WebSocketClient:
    """The smallest websocket client that can hold a conversation."""

    def __init__(self, port: int, origin: str | None = None) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.file = self.sock.makefile("rb")
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
        )
        if origin is not None:
            request += f"Origin: {origin}\r\n"
        self.sock.sendall((request + "\r\n").encode())

        self.status = self.file.readline().decode().strip()
        self.headers: dict[str, str] = {}
        while True:
            line = self.file.readline().decode().strip()
            if not line:
                break
            name, _, value = line.partition(":")
            self.headers[name.strip().lower()] = value.strip()
        self.expected_accept = accept_key(key)

    @property
    def upgraded(self) -> bool:
        return self.status.startswith("HTTP/1.1 101")

    def send(self, payload: dict) -> None:
        self.sock.sendall(client_frame(TEXT, json.dumps(payload).encode()))

    def next(self) -> dict | None:
        # expect_mask=False: this is the client's side, and a server never masks.
        message = read_message(self.file, expect_mask=False)
        return None if message is None else json.loads(message)

    def wait_for(self, kind: str, limit: int = 60) -> dict:
        """The next event of this kind, skipping whatever else arrives first."""
        for _ in range(limit):
            event = self.next()
            if event is None:
                break
            if event.get("kind") == kind:
                return event
        raise AssertionError(f"never saw {kind!r}")

    def until(self, kind: str, limit: int = 60) -> list[dict]:
        """Collect events up to and including the first of `kind`."""
        seen: list[dict] = []
        for _ in range(limit):
            event = self.next()
            if event is None:
                break
            seen.append(event)
            if event.get("kind") == kind:
                return seen
        raise AssertionError(f"never saw {kind!r}, got {[e.get('kind') for e in seen]}")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
