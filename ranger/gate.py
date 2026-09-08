"""The confirmation gate.

Between the model choosing a tool and the tool running, so it covers a typed
turn, a spoken turn and a heartbeat-initiated action with one mechanism. There
is no path around it: `core.turn` consults the gate before any tool flagged
`confirm`, and a tool that is not flagged never reaches it.

Three rules that do not bend:

  - **Per action.** A yes is consumed by the action that asked for it. Nothing
    is remembered, nothing generalises, and there is no "always allow". Saying
    yes to one send does not pre-authorise the next.
  - **Nothing waits forever.** Every gate answers inside a timeout. A heartbeat
    action nobody is there to approve resolves to held, and the loop keeps
    running.
  - **A spoken yes is not consent.** In voice mode the action is held and
    written to the inbox, to be approved at a keyboard. Transcription is good,
    not perfect, and "no, don't" is one mishearing away from "yes".

The gate keys on the tool's `confirm` flag and the action being described,
never on a list of tool names, so a tool added later is covered without editing
anything here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol, runtime_checkable

APPROVED = "approved"
DECLINED = "declined"
HELD = "held"


@dataclass(frozen=True)
class ConfirmationRequest:
    """What is about to happen, in words the operator can judge."""

    tool: str
    action: str
    payload: dict[str, Any]
    origin: str = "conversation"  # conversation | voice | heartbeat | browser
    #: The tool call this asks about. A gate that collects its answer from
    #: somewhere else, like a card in a browser, matches the answer to the
    #: question with this. Without it a click left over from a card the
    #: operator scrolled past could approve the next action instead.
    token: str = ""

    def render(self) -> str:
        return f"{self.action}"


@dataclass(frozen=True)
class Decision:
    outcome: str
    reason: str = ""

    @property
    def approved(self) -> bool:
        return self.outcome == APPROVED

    @property
    def held(self) -> bool:
        return self.outcome == HELD


@runtime_checkable
class Gate(Protocol):
    name: str

    async def ask(self, request: ConfirmationRequest) -> Decision: ...


class DenyingGate:
    """The default. Anything consequential is refused rather than assumed."""

    name = "deny"

    async def ask(self, request: ConfirmationRequest) -> Decision:
        return Decision(
            DECLINED,
            "no way to ask the operator from here, so nothing consequential runs",
        )


class TerminalGate:
    """Asks at the keyboard. Only ever used where a person is typing."""

    name = "terminal"

    def __init__(
        self,
        prompt: Callable[[str], str] | None = None,
        out: Any = None,
    ) -> None:
        self._prompt = prompt or (lambda text: input(text))
        self.out = out

    async def ask(self, request: ConfirmationRequest) -> Decision:
        lines = [
            "",
            "  Jarvis wants to do something that needs your yes:",
            f"    {request.render()}",
            "",
        ]
        if self.out is not None:
            for line in lines:
                print(line, file=self.out, flush=True)

        answer = await asyncio.to_thread(self._prompt, "  Allow this once? [y/N] ")
        if answer.strip().lower() in {"y", "yes"}:
            return Decision(APPROVED, "approved at the keyboard")
        return Decision(DECLINED, "declined at the keyboard")


class HoldingGate:
    """Writes it to the inbox and returns. Never blocks, never guesses.

    Used wherever there is no keyboard in front of the operator: voice mode,
    and anything the heartbeat starts. One mechanism for both, because they are
    the same situation.
    """

    name = "hold"

    def __init__(self, inbox: Any, now: Callable[[], datetime] | None = None) -> None:
        self.inbox = inbox
        self.now = now or datetime.now

    async def ask(self, request: ConfirmationRequest) -> Decision:
        from .heartbeat import Notice

        when = self.now()
        body = (
            f"Jarvis wanted to do this and stopped, because it needs your yes:\n\n"
            f"> {request.render()}\n\n"
            f"Started from: {request.origin}.\n"
            f"Tool: `{request.tool}`\n\n"
            "Nothing has been done. Ask again at the keyboard if you want it to go "
            "ahead. Dismiss this notice when you have dealt with it."
        )
        try:
            self.inbox.write(
                Notice(
                    kind="awaiting-confirmation",
                    title=f"Waiting on you: {request.action}",
                    body=body,
                    created=when,
                )
            )
        except Exception as exc:  # a full disk must not become a silent yes
            return Decision(DECLINED, f"held, but the notice could not be written: {exc}")
        return Decision(HELD, "held for approval at a keyboard, and put in your inbox")


class SocketGate:
    """Asks in the browser and waits for a click.

    The card is not the safety mechanism; this is. Nothing runs until `ask`
    returns approved, and `ask` only returns approved when a decision arrives
    carrying the same token as the question. A front end that never renders the
    card, or renders it and ignores it, gets a timeout and a held action. There
    is no path where not asking means yes.

    A click, never a transcript. In voice mode the operator's spoken words are
    a mishearing away from the opposite of what they meant, so 7d disables the
    microphone while a card is open rather than letting a spoken yes reach here.
    """

    name = "socket"

    def __init__(self, emit: Callable[[dict[str, Any]], None]) -> None:
        self.emit = emit
        self._pending: dict[str, asyncio.Future] = {}

    async def ask(self, request: ConfirmationRequest) -> Decision:
        loop = asyncio.get_running_loop()
        answer: asyncio.Future = loop.create_future()
        token = request.token or f"{id(request):x}"
        self._pending[token] = answer

        self.emit(
            {
                "kind": "confirm_open",
                "token": token,
                "action": request.action,
                "tool": request.tool,
                "origin": request.origin,
            }
        )
        try:
            allowed = await answer
        finally:
            self._pending.pop(token, None)
            self.emit({"kind": "confirm_closed", "token": token})

        if allowed:
            return Decision(APPROVED, "approved in the browser")
        return Decision(DECLINED, "declined in the browser")

    def decide(self, token: str, allowed: bool) -> bool:
        """Answer an open question. False if there was no such question.

        A token that is not pending is not an error worth breaking the
        connection over: it is a stale card, a double click, or an answer that
        arrived after the gate had already timed out. Ignoring it is correct,
        and saying so lets the front end stop showing a card nobody is waiting
        on.
        """
        answer = self._pending.get(token)
        if answer is None or answer.done():
            return False
        answer.set_result(bool(allowed))
        return True

    def abandon(self) -> None:
        """The socket went away. Nothing waits forever, and nothing is approved."""
        for answer in list(self._pending.values()):
            if not answer.done():
                answer.set_result(False)
        self._pending.clear()


class ScriptedGate:
    """Tests only. Answers from a list, and records what it was asked."""

    name = "scripted"

    def __init__(self, answers: list[str] | None = None) -> None:
        self.answers = list(answers or [])
        self.asked: list[ConfirmationRequest] = []

    async def ask(self, request: ConfirmationRequest) -> Decision:
        self.asked.append(request)
        outcome = self.answers.pop(0) if self.answers else DECLINED
        return Decision(outcome, f"scripted {outcome}")


async def ask_with_timeout(gate: Gate, request: ConfirmationRequest, seconds: float) -> Decision:
    """No gate may hang the caller.

    Belt and braces: HoldingGate returns immediately and TerminalGate is only
    ever used where somebody is typing, but a future gate that reaches out to a
    phone must not be able to deadlock the heartbeat on someone who is asleep.
    """
    try:
        return await asyncio.wait_for(gate.ask(request), timeout=seconds)
    except asyncio.TimeoutError:
        return Decision(
            HELD,
            f"nobody answered within {seconds:.0f}s, so nothing was done",
        )
