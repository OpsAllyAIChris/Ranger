"""RFC 6455 framing, and nothing above it.

Deliberately not a dependency. Jarvis runs on two packages, both of which had
to be installed on a Python 3.14 machine where wheel availability has already
cost a round trip, and the part of the websocket protocol this project actually
uses is a text frame, a close and a ping. That is a readable amount of code,
and reading it is the point: `start-here.md` asks for a program small enough to
read whole.

What this module knows: bytes on a socket. It does not know what a turn is,
what Jarvis is, or what the messages mean. `bridge.py` is where meaning starts.

Frames from a browser are always masked and frames to it never are. That is not
a nicety in the spec, it is the spec, and a browser will drop the connection
over it.
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct
from dataclasses import dataclass
from typing import BinaryIO, Callable

#: From the spec. Not a secret and not configurable.
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

CONTINUATION = 0x0
TEXT = 0x1
BINARY = 0x2
CLOSE = 0x8
PING = 0x9
PONG = 0xA

#: A typed turn is a sentence. A megabyte is already absurd, and refusing early
#: means a confused or hostile client cannot make the server allocate for it.
MAX_MESSAGE_BYTES = 1 << 20

CLOSE_NORMAL = 1000
CLOSE_PROTOCOL_ERROR = 1002
CLOSE_UNSUPPORTED_DATA = 1003
CLOSE_TOO_BIG = 1009


class ProtocolError(Exception):
    """The peer broke the framing. The connection does not continue."""

    def __init__(self, message: str, code: int = CLOSE_PROTOCOL_ERROR) -> None:
        super().__init__(message)
        self.code = code


def accept_key(client_key: str) -> str:
    """The handshake answer. sha1 of the client's key and the fixed GUID."""
    digest = hashlib.sha1((client_key.strip() + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


@dataclass(frozen=True)
class Frame:
    opcode: int
    payload: bytes
    fin: bool = True


def _read_exact(source: BinaryIO, count: int) -> bytes | None:
    """Read exactly count bytes, or None if the peer went away mid-frame."""
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = source.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(source: BinaryIO, *, expect_mask: bool = True) -> Frame | None:
    """One frame off the wire. None means the peer closed the socket.

    Masking is directional and the spec is strict both ways: a client masks,
    a server never does. The default is the server's side of that, which is
    where Jarvis sits; `expect_mask=False` is the reader a client would use,
    and the tests are the only client in this repository.
    """
    header = _read_exact(source, 2)
    if header is None:
        return None
    first, second = header

    if first & 0x70:
        # Reserved bits. Nothing here negotiates an extension, so a peer
        # setting one is either confused or talking to the wrong server.
        raise ProtocolError("reserved bits set with no extension negotiated")

    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F

    if opcode in (CLOSE, PING, PONG):
        if not fin:
            raise ProtocolError("a control frame cannot be fragmented")
        if length > 125:
            raise ProtocolError("a control frame cannot exceed 125 bytes")

    if length == 126:
        extended = _read_exact(source, 2)
        if extended is None:
            return None
        (length,) = struct.unpack("!H", extended)
    elif length == 127:
        extended = _read_exact(source, 8)
        if extended is None:
            return None
        (length,) = struct.unpack("!Q", extended)

    if length > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"frame of {length} bytes is over the limit", CLOSE_TOO_BIG)

    if masked != expect_mask:
        # A browser always masks and a server never does. Either way round,
        # getting it wrong means something other than the expected peer is on
        # this socket, and the spec says to fail the connection rather than
        # carry on politely.
        raise ProtocolError(
            "a client frame must be masked" if expect_mask
            else "a server frame must not be masked"
        )

    mask = b""
    if masked:
        mask = _read_exact(source, 4)
        if mask is None:
            return None

    payload = _read_exact(source, length) if length else b""
    if payload is None:
        return None

    if mask:
        payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    return Frame(opcode=opcode, payload=payload, fin=fin)


def build_frame(opcode: int, payload: bytes = b"") -> bytes:
    """A server frame. Never masked, never fragmented."""
    length = len(payload)
    header = bytes([0x80 | opcode])
    if length < 126:
        header += bytes([length])
    elif length < (1 << 16):
        header += bytes([126]) + struct.pack("!H", length)
    else:
        header += bytes([127]) + struct.pack("!Q", length)
    return header + payload


def text_frame(message: str) -> bytes:
    return build_frame(TEXT, message.encode("utf-8"))


def close_frame(code: int = CLOSE_NORMAL, reason: str = "") -> bytes:
    return build_frame(CLOSE, struct.pack("!H", code) + reason.encode("utf-8")[:123])


def client_frame(opcode: int, payload: bytes = b"", *, fin: bool = True) -> bytes:
    """A masked frame, the direction a browser sends. Tests only.

    Here rather than in the test file because it is the exact inverse of
    read_frame, and the two staying in step is the point.
    """
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    length = len(payload)
    header = bytes([(0x80 if fin else 0x00) | opcode])
    if length < 126:
        header += bytes([0x80 | length])
    elif length < (1 << 16):
        header += bytes([0x80 | 126]) + struct.pack("!H", length)
    else:
        header += bytes([0x80 | 127]) + struct.pack("!Q", length)
    return header + mask + masked


def read_message(
    source: BinaryIO,
    on_control: Callable[[bytes], None] | None = None,
    *,
    expect_mask: bool = True,
) -> str | None:
    """One complete text message, reassembled across continuation frames.

    Returns None when the peer closes, cleanly or otherwise. A ping is answered
    through `on_control` rather than here, because this module does not own the
    socket it is reading from.
    """
    parts: list[bytes] = []
    total = 0
    expecting_continuation = False

    while True:
        frame = read_frame(source, expect_mask=expect_mask)
        if frame is None:
            return None

        if frame.opcode == CLOSE:
            return None
        if frame.opcode == PING:
            if on_control is not None:
                on_control(build_frame(PONG, frame.payload))
            continue
        if frame.opcode == PONG:
            continue

        if frame.opcode == BINARY:
            raise ProtocolError("binary frames are not accepted", CLOSE_UNSUPPORTED_DATA)

        if frame.opcode == TEXT:
            if expecting_continuation:
                raise ProtocolError("a new message started before the last one finished")
            expecting_continuation = True
        elif frame.opcode == CONTINUATION:
            if not expecting_continuation:
                raise ProtocolError("continuation frame with nothing to continue")
        else:
            raise ProtocolError(f"unknown opcode {frame.opcode}")

        parts.append(frame.payload)
        total += len(frame.payload)
        if total > MAX_MESSAGE_BYTES:
            raise ProtocolError("message is over the limit", CLOSE_TOO_BIG)

        if frame.fin:
            raw = b"".join(parts)
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                # The spec is specific about this: a text frame that is not
                # valid UTF-8 fails the connection rather than being repaired.
                raise ProtocolError(f"text frame is not valid UTF-8: {exc}") from exc
