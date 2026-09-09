"""Item M. Running a turn with nobody at the other end.

Amendment A says there is one agent and several callers. There are four:
transcribed speech, a typed turn, the heartbeat, and the browser. **This is the
fifth, and the only one with no human in front of it.**

It is a caller and nothing more. It enters the same `Ranger.turn()`, with the
same tools, the same prompt, the same untrusted-content fencing and the same
audit log. There is no second agent path here, no lite version, and nothing in
this file decides anything an interactive turn would not.

## What is different, and it is only three things

**It cannot approve its own gate.** A headless run gets a gate that never says
yes: a consequential action stops, writes a notice into `Ranger/inbox`, and
waits for a keyboard. This is the spoken-yes rule carried one step further --
work running while the operator is in a meeting has *less* consent authority
than a voice turn, not more, because there is nobody to object.

**It is bounded.** Wall clock, turns and tool calls, all in config. A runaway
on this laptop during a customer call is the failure that gets Jarvis closed
for good and never opened again. **A bound hit is an outcome, not an
exception**: what the run had at the moment it stopped comes back with it, and
saying "I read four accounts and here is what I found" is worth more than a
timeout.

**It cannot start another one.** One level. A headless turn that could spawn
headless turns is a bound that multiplies, and the bounds have not been proven
on real work yet. The refusal is a guard in this module rather than a note in a
docstring.

## What it returns, and why that shape

A `Run`: what was asked, what came back, every tool call, and how it ended.
Serialisable, because the two things being built on top of this both consume
it -- jobs (M2) persist it and report progress from it, and continuity (K)
reads it to answer "where were we". A raw event stream would make both of them
implement collection; a bare string would throw away everything except the last
sentence. Callers that do want the events as they happen pass `on_event`, which
is what a job's progress file will be written from.

## What this is not

Not a sub-agent, not a specialist, not scheduled, not asynchronous. It is the
ability to run a turn without a human present. Everything else is built on it
later, and none of it is built here.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .config import Config

#: How a headless turn is marked everywhere it appears: the audit log, the
#: gate's notice, and the origin the core reports.
ORIGIN = "headless"

#: Outcomes. Every run ends as exactly one of these, including the ones that
#: went wrong: a run that reports nothing is a run nobody can act on.
COMPLETED = "completed"
BOUNDED = "bounded"
HELD = "held"
FAILED = "failed"
REFUSED = "refused"

#: How long past its own deadline a run is allowed before it is cut off mid
#: call. The soft check happens between events and keeps whatever was gathered;
#: this is the backstop for a provider that never answers at all, and the
#: provider's own timeout is the first line of that defence.
HARD_GRACE_SECONDS = 30.0

#: True while a headless run is in progress in this process. One level, and the
#: guard is here rather than in a docstring.
_running = False


@dataclass
class Step:
    """One tool call inside a headless run."""

    name: str
    ok: bool
    summary: str
    when: datetime = field(default_factory=datetime.now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "ok": self.ok, "summary": self.summary,
            "when": self.when.isoformat(timespec="seconds"),
        }


@dataclass
class Run:
    """What a headless turn did. The record every later caller reads.

    Deliberately complete rather than minimal. A job needs to persist it, a
    continuity file needs to say what was in flight, and the operator needs to
    be told what happened without a transcript in front of them.
    """

    prompt: str
    invoked_by: str = "unknown"
    outcome: str = COMPLETED
    text: str = ""
    steps: list[Step] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    #: Set when a bound stopped it, or when something failed.
    why: str = ""
    started: datetime = field(default_factory=datetime.now)
    finished: datetime | None = None
    turns: int = 0
    seconds: float = 0.0

    @property
    def partial(self) -> bool:
        """Did it stop early with work in hand? The useful middle case."""
        return self.outcome in (BOUNDED, FAILED) and bool(self.text or self.steps)

    @property
    def held(self) -> bool:
        """Did something stop and go to the inbox for a yes?"""
        return self.outcome == HELD or any(
            "held" in step.summary.casefold() for step in self.steps
        )

    def tools_used(self) -> list[str]:
        return [step.name for step in self.steps]

    def describe(self) -> str:
        """One line for the log and for a person reading it later."""
        head = {
            COMPLETED: "finished",
            BOUNDED: "stopped at a limit",
            HELD: "waiting on you",
            FAILED: "failed",
            REFUSED: "refused",
        }[self.outcome]
        parts = [f"{head} in {self.seconds:.1f}s", f"{len(self.steps)} tool call(s)"]
        if self.why:
            parts.append(self.why)
        return ", ".join(parts)

    def report(self) -> str:
        """What the run had when it stopped. **Never just "timed out".**

        A partial answer with the work that was done in it is worth something;
        a bare bound is worth nothing, and would send the operator back to do
        it all again by hand.
        """
        lines = [self.describe()]
        if self.steps:
            lines.append("")
            for step in self.steps:
                mark = "ok" if step.ok else "failed"
                lines.append(f"  {mark}  {step.name}: {step.summary}")
        if self.text.strip():
            lines += ["", self.text.strip()]
        elif self.outcome == BOUNDED:
            lines += ["", "It stopped before saying anything. The tool calls above are "
                          "what it had managed."]
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "invoked_by": self.invoked_by,
            "outcome": self.outcome,
            "text": self.text,
            "steps": [step.as_dict() for step in self.steps],
            "notices": list(self.notices),
            "why": self.why,
            "started": self.started.isoformat(timespec="seconds"),
            "finished": self.finished.isoformat(timespec="seconds") if self.finished else "",
            "turns": self.turns,
            "seconds": round(self.seconds, 2),
            "partial": self.partial,
            "held": self.held,
        }


def build_agent(
    config: Config,
    *,
    provider: Any = None,
    vault: Any = None,
    audit: Any = None,
    origin: str = ORIGIN,
):
    """A core with no way to say yes to itself.

    Everything here is the ordinary construction every other caller does. The
    one decision is the gate: `HoldingGate` writes the request into the inbox
    and returns *held*, which is not approval. There is deliberately no
    parameter to pass a gate that could approve -- a caller that wants one has
    a keyboard, and a keyboard is not what this is for.
    """
    from .audit import AuditLog
    from .core import Ranger
    from .gate import HoldingGate
    from .heartbeat import Inbox
    from .knowledge import KnowledgeLoader
    from .provider import build_provider
    from .toolset import build_registry
    from .vault import Vault

    from dataclasses import replace

    vault = vault or Vault(config.vault)
    audit = audit if audit is not None else AuditLog(vault, config.vault.log)

    # The standing context, on whatever budget the operator set for work nobody
    # is watching. Zero means the same as an interactive turn, which is the
    # default: this is one number in one place, not a different assembly.
    trimmed = config.headless.context_chars
    if trimmed:
        config = replace(config, context=replace(config.context, budget_chars=trimmed))

    return Ranger(
        config=config,
        provider=provider or build_provider(config),
        registry=build_registry(config, vault, audit=audit),
        vault=vault,
        knowledge_loader=KnowledgeLoader(vault, config.vault, config.knowledge),
        # **The one thing that is different, and the reason this file exists.**
        gate=HoldingGate(Inbox(vault, config.vault.inbox)),
        audit=audit,
        origin=origin,
    )


class Busy(Exception):
    """A headless run is already in progress. One level, and this is it."""


async def run(
    config: Config,
    prompt: str,
    *,
    invoked_by: str = "unknown",
    agent: Any = None,
    on_event: Callable[[Any], None] | None = None,
    max_seconds: float | None = None,
    max_turns: int | None = None,
    max_tools: int | None = None,
    audit: Any = None,
    vault: Any = None,
    provider: Any = None,
) -> Run:
    """Run one turn with nobody watching, and come back with what happened.

    The bounds are checked between events, so stopping keeps everything that
    had already been gathered. That is the whole point of doing it this way
    rather than wrapping the call in a timeout: a cancelled coroutine has no
    partial answer in it, and "I read four of six accounts" is the answer worth
    having.
    """
    global _running

    from .events import Notice, TextDelta, ToolFinished, TurnComplete

    limits = config.headless
    seconds = max_seconds if max_seconds is not None else limits.max_seconds
    turns_allowed = max_turns if max_turns is not None else limits.max_turns
    tools_allowed = max_tools if max_tools is not None else limits.max_tool_calls

    record = Run(prompt=prompt, invoked_by=invoked_by, started=datetime.now())

    if _running:
        # A headless turn that can start headless turns is a bound that
        # multiplies. One level until the bounds are proven on real work.
        record.outcome = REFUSED
        record.why = (
            "a headless run is already in progress, and one cannot start another"
        )
        record.finished = datetime.now()
        return record
    if not str(prompt or "").strip():
        record.outcome = REFUSED
        record.why = "there was nothing to do: the prompt was empty"
        record.finished = datetime.now()
        return record

    agent = agent or build_agent(config, provider=provider, vault=vault, audit=audit)
    log = getattr(agent, "audit", None)

    def note(kind: str, detail: str) -> None:
        if log is None:
            return
        try:
            log.write(kind, detail, origin=ORIGIN)
        except Exception:
            pass  # the log must never be able to stop a run

    note("headless started", f"{invoked_by}: {prompt[:160]}")
    started = time.monotonic()
    _running = True
    try:
        await _consume(
            agent, prompt, record, on_event,
            seconds=seconds, turns_allowed=turns_allowed, tools_allowed=tools_allowed,
            events=(Notice, TextDelta, ToolFinished, TurnComplete),
            started=started,
        )
    except asyncio.TimeoutError:
        record.outcome = BOUNDED
        record.why = (
            f"it stopped answering, so the run was cut off {seconds:.0f}s in "
            f"(plus {HARD_GRACE_SECONDS:.0f}s of grace)"
        )
    except asyncio.CancelledError:
        record.outcome = FAILED
        record.why = "the run was cancelled"
        raise
    except Exception as exc:
        record.outcome = FAILED
        record.why = f"{type(exc).__name__}: {exc}"
    finally:
        _running = False
        record.finished = datetime.now()
        record.seconds = time.monotonic() - started
        if record.held and record.outcome == COMPLETED:
            record.outcome = HELD
            record.why = record.why or "something needs your yes and is in your inbox"
        note(f"headless {record.outcome}", record.describe())

    return record


async def _consume(
    agent: Any,
    prompt: str,
    record: Run,
    on_event: Callable[[Any], None] | None,
    *,
    seconds: float,
    turns_allowed: int,
    tools_allowed: int,
    events: tuple,
    started: float,
) -> None:
    """Drive the turn, watching the bounds between events.

    The hard timeout is a backstop for a provider that never returns at all;
    everything else is caught by the checks in the loop, which is what leaves
    the partial work intact.
    """
    Notice, TextDelta, ToolFinished, TurnComplete = events

    async def drive() -> None:
        said: list[str] = []
        async for event in agent.turn(prompt):
            if on_event is not None:
                try:
                    on_event(event)
                except Exception:
                    pass  # a caller's progress file must not stop the work
            if isinstance(event, TextDelta):
                said.append(event.text)
            elif isinstance(event, ToolFinished):
                record.steps.append(Step(name=event.name, ok=event.ok,
                                         summary=event.summary))
            elif isinstance(event, Notice):
                record.notices.append(event.message)
            elif isinstance(event, TurnComplete):
                record.turns += 1
                if event.reply:
                    said = [event.reply]

            record.text = "".join(said).strip()

            elapsed = time.monotonic() - started
            if elapsed > seconds:
                record.outcome = BOUNDED
                record.why = (
                    f"it ran past {seconds:.0f} seconds, so it was stopped with "
                    f"{len(record.steps)} tool call(s) done"
                )
                break
            if len(record.steps) > tools_allowed:
                record.outcome = BOUNDED
                record.why = (
                    f"it made more than {tools_allowed} tool calls, so it was stopped"
                )
                break
            if record.turns > turns_allowed:
                record.outcome = BOUNDED
                record.why = f"it took more than {turns_allowed} turns, so it was stopped"
                break

    await asyncio.wait_for(drive(), timeout=seconds + HARD_GRACE_SECONDS)
