"""The event stream Ranger emits while it works.

Amendment A: the core is a library. Its one entry point yields these events
as an async stream. The terminal prints them, Tier 3 will speak them, Tier 5
will file them, Tier 7 will animate them. Every event is JSON-serialisable so
the websocket in Tier 7 is a transport change and not a refactor.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, ClassVar


class State(str, Enum):
    """What Ranger is doing right now."""

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    AWAITING_CONFIRMATION = "awaiting_confirmation"


@dataclass(frozen=True)
class Event:
    kind: ClassVar[str] = "event"

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key, value in payload.items():
            if isinstance(value, Enum):
                payload[key] = value.value
        payload["kind"] = self.kind
        return payload


@dataclass(frozen=True)
class StateChanged(Event):
    """Drives the status dot in Tier 7 and the mic light in Tier 3."""

    kind: ClassVar[str] = "state"
    state: State


@dataclass(frozen=True)
class TextDelta(Event):
    """A chunk of the spoken reply, as it streams."""

    kind: ClassVar[str] = "text"
    text: str


@dataclass(frozen=True)
class ToolCalled(Event):
    kind: ClassVar[str] = "tool_called"
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolFinished(Event):
    kind: ClassVar[str] = "tool_finished"
    name: str
    ok: bool
    summary: str


@dataclass(frozen=True)
class ConfirmationRequested(Event):
    """Tier 6 gate. No blanket approvals, no remembered yes.

    Declared in Tier 1 so every caller is built against a core that can pause
    and wait. Nothing raises it yet.
    """

    kind: ClassVar[str] = "confirmation"
    action: str
    detail: str
    token: str


@dataclass(frozen=True)
class Notice(Event):
    """Something the operator should know about but that is not the reply.

    level is one of: info, warn, alert.
    """

    kind: ClassVar[str] = "notice"
    level: str
    message: str


@dataclass(frozen=True)
class TurnComplete(Event):
    kind: ClassVar[str] = "turn_complete"
    reply: str
    tools_used: tuple[str, ...] = ()
    stop_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
