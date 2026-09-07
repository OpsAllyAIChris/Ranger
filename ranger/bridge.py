"""The browser as the fourth caller of the core.

Amendment A, exactly: the browser sends a turn to the same `Ranger.turn()` the
terminal calls, and receives the same events the terminal prints. Nothing here
decides anything. It reads JSON off a socket, calls the core, and writes the
core's own events back as JSON. Every event was built JSON-serialisable in
Tier 1 for this moment, so this is a transport and not a refactor.

One connection is one conversation. Two browser tabs are two transcripts, the
same way two terminals would be, and they share the vault and the log.

Reading and running are concurrent, and that is not an optimisation. The gate
asks the browser and waits for the answer, so if the read loop stopped while a
turn was in flight the answer could never arrive and every confirmation would
time out. A turn runs as a task; the socket keeps being read the whole time.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Config
from .core import Ranger
from .events import State
from .gate import Gate, SocketGate

TURN = "turn"
DECISION = "decision"
PANEL = "panel"
DISMISS = "dismiss"
STOP = "stop"

#: How many tool calls the panel remembers. Enough to see what just happened,
#: not a second audit log: the real one is in the vault and is append only.
RECENT_TOOLS = 8


@dataclass
class Session:
    """One websocket connection, one conversation."""

    agent: Ranger
    send: Callable[[dict[str, Any]], None]
    busy: bool = False
    tools: list[dict[str, Any]] = field(default_factory=list)
    _turn: asyncio.Task | None = None

    # -- outbound ------------------------------------------------------

    def emit(self, kind: str, **fields: Any) -> None:
        self.send({"kind": kind, **fields})

    def hello(self) -> None:
        """What the front end needs before it can render anything."""
        registry = self.agent.registry
        self.emit(
            "hello",
            model=self.agent.config.model.name,
            origin=self.agent.origin,
            gate=self.agent.gate.name,
            tools=registry.names() if registry else [],
            gated=[tool.name for tool in (registry or []) if tool.confirm],
            vault=str(self.agent.config.vault.root),
        )
        self.emit("state", state=State.IDLE.value)
        self.push_panel()

    def push_panel(self) -> None:
        """The vault, as the panel draws it. Read fresh, never cached."""
        from .panel import snapshot

        try:
            view = snapshot(self.agent.config, self.agent.vault)
        except Exception as exc:  # a panel that cannot read must not kill a turn
            self.emit("error", message=f"could not read the vault: {exc}")
            return
        view["tools"] = list(self.tools)
        self.emit("panel", **view)

    # -- inbound -------------------------------------------------------

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
        if kind == TURN:
            return self._start_turn(message)
        if kind == DECISION:
            return self._decide(message)
        if kind == PANEL:
            return self.push_panel()
        if kind == DISMISS:
            return self._dismiss(message)
        if kind == STOP:
            if not self.stop():
                self.emit("error", message="nothing was running")
            return
        self.emit("error", message=f"unknown message type {kind!r}")

    def _start_turn(self, message: dict[str, Any]) -> None:
        text = str(message.get("text", "")).strip()
        if not text:
            self.emit("error", message="an empty turn has nothing to answer")
            return

        if self.busy:
            if not bool(message.get("interrupt", False)):
                self.emit("error", message="still working on the last one")
                return
            # Barge-in. The operator is talking over Ranger, so what Ranger was
            # saying stops mattering. The core keeps what it managed to say, so
            # "no, not that one" still has something to refer to.
            self.stop("interrupted")

        self.busy = True
        self._turn = asyncio.create_task(self._run(text))

    def stop(self, why: str = "stopped") -> bool:
        """Cut the turn in flight. False if there was nothing to cut."""
        if self._turn is None or self._turn.done():
            return False
        self._turn.cancel()
        self._turn = None
        self.busy = False
        self.emit("stopped", reason=why)
        self.emit("state", state=State.IDLE.value)
        return True

    def _decide(self, message: dict[str, Any]) -> None:
        """A click on a confirmation card.

        Only a `SocketGate` can be answered this way. If the connection is
        wired to any other gate, a decision message is meaningless and is
        refused rather than quietly ignored, because a front end sending one
        into a gate that cannot hear it would look exactly like an approval
        that did nothing.
        """
        gate = self.agent.gate
        if not isinstance(gate, SocketGate):
            self.emit("error", message=f"this connection's gate is {gate.name}, not a card")
            return
        token = str(message.get("token", ""))
        allowed = bool(message.get("allow", False))
        if not gate.decide(token, allowed):
            self.emit("error", message="nothing was waiting on that answer")

    def _dismiss(self, message: dict[str, Any]) -> None:
        from .panel import dismiss

        title = dismiss(self.agent.config, self.agent.vault, str(message.get("id", "")))
        if title is None:
            self.emit("error", message="that notice is not in the inbox, or is already dismissed")
        else:
            self.emit("dismissed", title=title)
        self.push_panel()

    # -- the turn ------------------------------------------------------

    async def _run(self, text: str) -> None:
        from .events import ToolFinished

        try:
            async for event in self.agent.turn(text):
                if isinstance(event, ToolFinished):
                    self._remember_tool(event)
                self.send(event.as_dict())
        except asyncio.CancelledError:
            # Barge-in, and the replacement turn is already starting. Emitting
            # idle or done here would tell the front end the new turn had
            # finished before it began.
            raise
        except Exception as exc:  # a failed turn is an event, not a dead socket
            self.emit("error", message=f"{type(exc).__name__}: {exc}")
            self._finish()
        else:
            self._finish()

    def _finish(self) -> None:
        self.busy = False
        self.emit("state", state=State.IDLE.value)
        self.push_panel()
        self.emit("done")

    def _remember_tool(self, event: Any) -> None:
        self.tools.insert(0, {"name": event.name, "ok": event.ok, "summary": event.summary})
        del self.tools[RECENT_TOOLS:]

    def close(self) -> None:
        """The socket went away. Nothing is left waiting and nothing is approved."""
        if isinstance(self.agent.gate, SocketGate):
            self.agent.gate.abandon()
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()


def build_session(
    config: Config,
    send: Callable[[dict[str, Any]], None],
    *,
    gate: Gate | None = None,
    origin: str = "browser",
) -> Session:
    """Wire a core for one connection.

    The browser gets a `SocketGate`: there is a person at a keyboard and a card
    to click, which is the same situation the terminal is in. What it does not
    get is a gate it can talk its way past. Nothing runs until the gate returns
    approved, and the gate only returns approved when a decision arrives
    carrying the token of the question that is actually open.
    """
    from .assembly import build_agent

    def emit(payload: dict[str, Any]) -> None:
        send(payload)

    agent = build_agent(config, gate=gate or SocketGate(emit), origin=origin)
    return Session(agent=agent, send=send)
