"""Tier 7b: the websocket transport.

Two halves. The framing is tested against the spec's own worked example and
against the frames a browser actually sends. Above that, a real socket talks to
a real server running a scripted core, so the whole path from a typed turn to a
streamed reply is exercised without a browser and without an API key.

What no test here can prove is that a browser accepts the handshake. That was
checked against Chromium by hand, and the known-answer test below is the guard
that keeps it true: the accept key was wrong the first time, by one character in
the wrong half of a constant, and nothing but a browser or this vector catches
that.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import threading
from dataclasses import replace

import pytest

from ranger.wsframe import (
    BINARY,
    CLOSE,
    CLOSE_TOO_BIG,
    CONTINUATION,
    PING,
    PONG,
    TEXT,
    Frame,
    ProtocolError,
    accept_key,
    build_frame,
    client_frame,
    close_frame,
    read_frame,
    read_message,
    text_frame,
)


class Wire:
    """A file-like over fixed bytes, so framing can be read without a socket."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def read(self, count: int) -> bytes:
        chunk = self.data[self.pos : self.pos + count]
        self.pos += len(chunk)
        return chunk


# ---------------------------------------------------------------- framing


def test_the_handshake_matches_the_specs_worked_example():
    """RFC 6455's own key and answer.

    This is here because the constant was wrong first time and every unit test
    of my own devising still passed: they all agreed with each other and with
    nothing outside. Chromium refused the connection with "Incorrect
    Sec-WebSocket-Accept header value" and that was the only signal.
    """
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_the_handshake_ignores_surrounding_whitespace():
    assert accept_key(" dGhlIHNhbXBsZSBub25jZQ== ") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


@pytest.mark.parametrize("size", [0, 1, 125, 126, 127, 65535, 65536, 70000])
def test_a_server_frame_carries_its_own_length_at_every_boundary(size):
    """Three length encodings, and the seams between them are where it breaks."""
    payload = b"x" * size
    frame = build_frame(TEXT, payload)
    if size < 126:
        assert frame[1] == size
    elif size < 1 << 16:
        assert frame[1] == 126 and struct.unpack("!H", frame[2:4])[0] == size
    else:
        assert frame[1] == 127 and struct.unpack("!Q", frame[2:10])[0] == size
    assert frame.endswith(payload)
    assert frame[0] == 0x80 | TEXT  # fin, never fragmented


def test_a_server_frame_is_never_masked():
    """The spec's rule, and a browser drops the connection over it."""
    assert build_frame(TEXT, b"hello")[1] & 0x80 == 0


@pytest.mark.parametrize("size", [0, 5, 200, 70000])
def test_a_masked_client_frame_reads_back_as_what_was_sent(size):
    payload = os.urandom(size)
    frame = read_frame(Wire(client_frame(TEXT, payload)))
    assert frame == Frame(opcode=TEXT, payload=payload, fin=True)


def test_an_unmasked_client_frame_is_refused():
    """Only a browser should be on this socket, and a browser always masks."""
    with pytest.raises(ProtocolError, match="masked"):
        read_frame(Wire(build_frame(TEXT, b"hello")))


def test_reserved_bits_are_refused():
    raw = bytearray(client_frame(TEXT, b"hi"))
    raw[0] |= 0x40
    with pytest.raises(ProtocolError, match="reserved"):
        read_frame(Wire(bytes(raw)))


def test_a_message_split_across_frames_is_reassembled():
    wire = Wire(
        client_frame(TEXT, "where are we on ".encode(), fin=False)
        + client_frame(CONTINUATION, "Illes Foods".encode())
    )
    assert read_message(wire) == "where are we on Illes Foods"


def test_a_continuation_with_nothing_to_continue_is_refused():
    with pytest.raises(ProtocolError, match="nothing to continue"):
        read_message(Wire(client_frame(CONTINUATION, b"orphan")))


def test_a_ping_is_answered_and_does_not_end_the_message():
    sent: list[bytes] = []
    wire = Wire(client_frame(PING, b"beat") + client_frame(TEXT, b"still here"))
    assert read_message(wire, sent.append) == "still here"
    assert sent == [build_frame(PONG, b"beat")]


def test_a_close_frame_ends_the_message_stream():
    assert read_message(Wire(client_frame(CLOSE, b""))) is None


def test_a_severed_connection_ends_the_message_stream():
    assert read_message(Wire(b"")) is None
    assert read_frame(Wire(client_frame(TEXT, b"truncated")[:4])) is None


def test_binary_frames_are_refused():
    with pytest.raises(ProtocolError, match="binary"):
        read_message(Wire(client_frame(BINARY, b"\x00\x01")))


def test_text_that_is_not_utf8_fails_the_connection():
    """The spec is specific: it fails rather than being repaired."""
    with pytest.raises(ProtocolError, match="UTF-8"):
        read_message(Wire(client_frame(TEXT, b"\xff\xfe")))


def test_an_oversized_frame_is_refused_before_it_is_read():
    header = bytes([0x80 | TEXT, 0x80 | 127]) + struct.pack("!Q", 1 << 30)
    with pytest.raises(ProtocolError) as exc:
        read_frame(Wire(header))
    assert exc.value.code == CLOSE_TOO_BIG


def test_a_control_frame_cannot_be_fragmented_or_large():
    with pytest.raises(ProtocolError, match="fragmented"):
        read_frame(Wire(client_frame(PING, b"x", fin=False)))
    with pytest.raises(ProtocolError, match="125"):
        read_frame(Wire(client_frame(PING, b"x" * 200)))


def test_a_close_frame_carries_its_code():
    frame = read_frame(Wire(close_frame(1002, "bad")), expect_mask=False)
    assert struct.unpack("!H", frame.payload[:2])[0] == 1002


def test_a_masked_frame_from_a_server_is_refused():
    """The rule runs both ways, and a client that ignores it is talking to
    something that is not the server it thinks it is."""
    with pytest.raises(ProtocolError, match="must not be masked"):
        read_frame(Wire(client_frame(TEXT, b"hi")), expect_mask=False)


# ------------------------------------------------------- a real connection


class Client:
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


SCRIPT = [
    {"text": "Illes is quiet. Rod has not replied in three weeks."},
    {"tools": [{"name": "forget", "input": {"fact": "Chris prefers morning meetings"}}]},
    {"text": "I could not do that."},
]


@pytest.fixture
def served(config):
    """A real server on a real port, with a scripted core and no gate."""
    from ranger.audit import AuditLog
    from ranger.bridge import Session
    from ranger.core import Ranger
    from ranger.knowledge import KnowledgeLoader
    from ranger.server import build
    from ranger.testing import ScriptedProvider
    from ranger.toolset import build_registry
    from ranger.vault import Vault

    (config.vault.memory / "facts.md").write_text(
        "- 2026-09-01 | Chris prefers morning meetings\n", encoding="utf-8"
    )

    def factory(cfg, send):
        vault = Vault(cfg.vault)
        return Session(
            agent=Ranger(
                config=cfg,
                provider=ScriptedProvider(list(SCRIPT)),
                registry=build_registry(cfg, vault),
                vault=vault,
                knowledge_loader=KnowledgeLoader(vault, cfg.vault, cfg.knowledge),
                gate=None,  # no gate wired, so DenyingGate. Tier 6's rule.
                audit=AuditLog(vault, cfg.vault.log),
                origin="browser",
            ),
            send=send,
        )

    server = build(
        replace(config, server=replace(config.server, port=0)), session_factory=factory
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_browser_shaped_handshake_is_accepted(served):
    client = Client(served, origin=f"http://localhost:{served}")
    try:
        assert client.upgraded, client.status
        assert client.headers["upgrade"].lower() == "websocket"
        assert client.headers["sec-websocket-accept"] == client.expected_accept
    finally:
        client.close()


def test_the_first_thing_across_is_what_the_front_end_needs(served, config):
    client = Client(served)
    try:
        hello = client.next()
        assert hello["kind"] == "hello"
        assert hello["model"] == config.model.name
        assert hello["origin"] == "browser"
        assert hello["gate"] == "deny"
        assert "forget" in hello["gated"]
        assert client.next() == {"kind": "state", "state": "idle"}
    finally:
        client.close()


def test_a_turn_in_and_a_streamed_reply_out(served):
    client = Client(served)
    try:
        client.until("state")
        client.send({"type": "turn", "text": "where are we on Illes"})
        events = client.until("done")
        kinds = [event["kind"] for event in events]

        assert "text" in kinds, kinds
        assert kinds.index("state") < kinds.index("text")
        reply = "".join(e["text"] for e in events if e["kind"] == "text")
        assert reply == "Illes is quiet. Rod has not replied in three weeks."

        finished = next(e for e in events if e["kind"] == "turn_complete")
        assert finished["reply"] == reply
    finally:
        client.close()


def test_the_state_stream_crosses_the_socket(served):
    """What drives the status dot in 7c. It has to arrive, in order."""
    client = Client(served)
    try:
        client.until("state")
        client.send({"type": "turn", "text": "where are we on Illes"})
        states = [e["state"] for e in client.until("done") if e["kind"] == "state"]
        assert states[0] == "thinking"
        assert "speaking" in states
        assert states[-1] == "idle"
    finally:
        client.close()


def test_a_gated_action_arrives_as_a_confirmation_and_then_a_refusal(served, config):
    """The browser wired no gate, so it gets DenyingGate. Tier 6, unchanged.

    This is also the exact frame sequence 7c has to render: the state goes to
    awaiting_confirmation, the action arrives in words with a token, and the
    tool comes back not done.
    """
    client = Client(served)
    try:
        client.until("state")
        client.send({"type": "turn", "text": "where are we on Illes"})
        client.until("done")

        client.send({"type": "turn", "text": "forget that I prefer morning meetings"})
        events = client.until("done")

        states = [e["state"] for e in events if e["kind"] == "state"]
        assert "awaiting_confirmation" in states

        ask = next(e for e in events if e["kind"] == "confirmation")
        assert "Permanently remove from memory" in ask["action"]
        assert ask["detail"] == "forget"
        assert ask["token"]

        done = next(e for e in events if e["kind"] == "tool_finished")
        assert done["ok"] is False
    finally:
        client.close()

    # And the fact is still in memory, which is the only proof that matters.
    remaining = (config.vault.memory / "facts.md").read_text(encoding="utf-8")
    assert "morning meetings" in remaining


@pytest.mark.parametrize(
    "payload, expected",
    [
        ("not json at all", "not JSON"),
        ('{"type": "shout", "text": "hi"}', "unknown message type"),
        ('{"type": "turn", "text": "   "}', "empty turn"),
        ('["turn", "hello"]', "expected an object"),
    ],
)
def test_a_malformed_message_is_answered_rather_than_dropped(served, payload, expected):
    """A front end under development gets this wrong. A silent drop is the
    worst possible way to find that out."""
    client = Client(served)
    try:
        client.until("state")
        client.sock.sendall(client_frame(TEXT, payload.encode()))
        error = client.next()
        assert error["kind"] == "error"
        assert expected in error["message"]
    finally:
        client.close()


def test_a_socket_from_another_website_is_refused(served):
    """CORS does not apply to websockets.

    Without this check, any page the operator happens to have open could open a
    socket to Ranger and ask it about their accounts.
    """
    client = Client(served, origin="https://not-your-site.example")
    try:
        assert not client.upgraded
        assert "403" in client.status
    finally:
        client.close()


@pytest.mark.parametrize("origin", ["http://localhost:{port}", "http://127.0.0.1:{port}"])
def test_the_pages_own_origin_is_allowed(served, origin):
    client = Client(served, origin=origin.format(port=served))
    try:
        assert client.upgraded, client.status
    finally:
        client.close()


def test_a_request_that_is_not_an_upgrade_gets_an_error_not_a_socket(served):
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", served, timeout=5)
    conn.request("GET", "/ws")
    assert conn.getresponse().status == 400
    conn.close()


def test_the_plain_page_is_served_alongside_the_orb(served):
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", served, timeout=5)
    conn.request("GET", "/transport.html")
    response = conn.getresponse()
    body = response.read()
    assert response.status == 200
    assert b"Ranger transport" in body
    conn.close()


# --------------------------------------------------------------- the wiring


def test_a_browser_session_with_no_gate_wired_gets_the_one_that_refuses(config, monkeypatch):
    """Amendment A's fourth caller does not get to inherit the terminal's gate."""
    from ranger.bridge import build_session
    from ranger.gate import DenyingGate

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    session = build_session(config, lambda payload: None)
    assert isinstance(session.agent.gate, DenyingGate)
    assert session.agent.origin == "browser"


def test_the_terminal_still_gets_a_gate_that_can_ask(config, monkeypatch):
    from ranger.cli import _build_agent
    from ranger.gate import TerminalGate

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    assert isinstance(_build_agent(config).gate, TerminalGate)
