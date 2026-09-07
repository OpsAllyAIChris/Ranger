"""Tier 7b. The browser as the fourth caller of the core.

Amendment A, exactly: the browser sends a turn to the same `Ranger.turn()` the
terminal calls, and receives the same events the terminal prints. Nothing here
decides anything. It reads JSON off a socket, calls the core, and writes the
core's own events back as JSON. Every event was built JSON-serialisable in
Tier 1 for this moment, so this is a transport and not a refactor.

One connection is one conversation. Two browser tabs are two transcripts, the
same way two terminals would be, and they share the vault and the log.

The gate is `DenyingGate` and that is not an oversight. A front end that has not
wired a way to ask gets the gate that refuses, which is the rule from Tier 6
applied to a caller that cannot yet render a question. 7c replaces it with a
gate that asks in the browser. Until then a gated action produces the same
event a real gate would, followed by a refusal, which is precisely the shape 7c
has to render.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .config import Config
from .core import Ranger
from .events import Notice, StateChanged, State
from .gate import Gate

#: What the browser may send. Anything else is answered with a notice rather
#: than a disconnection, because a front end being developed will get this
#: wrong and a silent drop is the worst way to find out.
TURN = "turn"

Send = Callable[[str], Awaitable[None]] | Callable[[str], None]


@dataclass
class Session:
    """One websocket connection, one conversation."""

    agent: Ranger
    send: Callable[[dict[str, Any]], None]
    busy: bool = False

    def emit(self, kind: str, **fields: Any) -> None:
        self.send({"kind": kind, **fields})

    def hello(self) -> None:
        """What the front end needs to know before it can render anything."""
        self.emit(
            "hello",
            model=self.agent.config.model.name,
            origin=self.agent.origin,
            gate=self.agent.gate.name,
            tools=self.agent.registry.names() if self.agent.registry else [],
            gated=[tool.name for tool in (self.agent.registry or []) if tool.confirm],
        )
        self.emit("state", state=State.IDLE.value)

    async def handle(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.emit("error", message=f"that was not JSON: {exc}")
            return
        if not isinstance(message, dict):
            self.emit("error", message="expected an object")
            return

        kind = message.get("type")
        if kind != TURN:
            self.emit("error", message=f"unknown message type {kind!r}, expected {TURN!r}")
            return

        text = str(message.get("text", "")).strip()
        if not text:
            self.emit("error", message="an empty turn has nothing to answer")
            return

        if self.busy:
            # Sequential on purpose. Interrupting a turn is barge-in, which is
            # Tier 7d's problem and needs the core to support cancellation.
            self.emit("error", message="still working on the last one")
            return

        self.busy = True
        try:
            await self.run(text)
        finally:
            self.busy = False
            self.emit("state", state=State.IDLE.value)
            self.emit("done")

    async def run(self, text: str) -> None:
        try:
            async for event in self.agent.turn(text):
                self.send(event.as_dict())
        except Exception as exc:  # a failed turn is an event, not a dead socket
            self.emit("error", message=f"{type(exc).__name__}: {exc}")


def build_session(
    config: Config,
    send: Callable[[dict[str, Any]], None],
    *,
    gate: Gate | None = None,
    origin: str = "browser",
) -> Session:
    """Wire a core for one connection.

    `gate` defaults to nothing, and `build_agent` gives an unwired caller
    DenyingGate. That default is the Tier 6 rule, not a convenience.
    """
    from .assembly import build_agent

    agent = build_agent(config, gate=gate, origin=origin)
    return Session(agent=agent, send=send)
