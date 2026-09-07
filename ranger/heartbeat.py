"""Tier 5. The loop that lets Ranger act without being spoken to.

Quiet by default: most checks produce nothing most days, and nothing here
interrupts. A notice is a markdown file in Ranger/inbox/ that the operator reads
in Obsidian when they choose to.

**The inbox is the schedule.** There is no separate state file to drift, get
lost, or need migrating. Whether the morning surface has run today is answered
by whether today's file exists. That one decision gives restart safety, catch-up
after a sleeping laptop, and no refire storm on boot, for free:

  - Restart at 09:00 having missed 07:00: today's file is missing, the hour has
    passed, so it runs now. Catch-up, not skip.
  - Restart at 09:00 having already run: the file is there, nothing happens.
  - Away for a week: only today is considered. Six missed mornings are not
    replayed, because a week-old list of what went quiet is not news.

Nothing here waits on a person. A check that needs an answer times out into
doing nothing and leaving a note, so the loop cannot deadlock on someone who is
asleep.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from .config import Config
from .dates import day_and_month
from .vault import Vault, VaultError

#: One obvious way to stop everything proactive at once. A file rather than a
#: config edit, so it can be flipped from Obsidian on a phone, and so the
#: reason is written down next to the switch. Conversation is unaffected: the
#: operator can still talk to Ranger with it engaged.
KILL_SWITCH_FILE = "paused.md"

STATUS_NEW = "new"
STATUS_DISMISSED = "dismissed"

_FRONT = re.compile(r"^---\n(?P<front>.*?)\n---\n", re.DOTALL)
_FIELD = re.compile(r"^(?P<key>[a-z_]+):[ \t]*(?P<value>.*?)[ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class Notice:
    """One thing worth the operator's attention, held until they see it."""

    kind: str
    title: str
    body: str
    created: datetime
    status: str = STATUS_NEW
    path: Path | None = None
    #: Written by --force. A forced run is a test, and a test must not consume
    #: the day's real run: the operator forced the morning check at 02:51 to
    #: exercise the inbox, and that silently cancelled the genuine 07:00
    #: surface, because the scheduler only asks whether a notice exists.
    forced: bool = False

    @property
    def dismissed(self) -> bool:
        return self.status == STATUS_DISMISSED

    def filename(self) -> str:
        return f"{self.created.date().isoformat()} {self.kind}.md"

    def render(self) -> str:
        return (
            "---\n"
            f"kind: {self.kind}\n"
            f"created: {self.created.isoformat(timespec='seconds')}\n"
            f"status: {self.status}\n"
            + (f"forced: true\n" if self.forced else "")
            +
            "---\n\n"
            f"# {self.title}\n\n"
            f"{self.body.strip()}\n"
        )


def parse_notice(text: str, path: Path) -> Notice | None:
    match = _FRONT.match(text)
    if not match:
        return None
    fields = {m.group("key"): m.group("value") for m in _FIELD.finditer(match.group("front"))}
    try:
        created = datetime.fromisoformat(fields.get("created", ""))
    except ValueError:
        created = datetime.min

    body = text[match.end() :]
    title = ""
    heading = re.match(r"^#[ \t]+(?P<title>.+)$", body.strip(), re.MULTILINE)
    if heading:
        title = heading.group("title").strip()
        body = body.strip()[heading.end() :].strip()

    return Notice(
        kind=fields.get("kind", "note"),
        title=title,
        body=body.strip(),
        created=created,
        status=fields.get("status", STATUS_NEW),
        forced=fields.get("forced", "").strip().lower() == "true",
        path=path,
    )


class Inbox:
    """Notices on disk. Held until dismissed, never delivered and forgotten."""

    def __init__(self, vault: Vault, folder: Path) -> None:
        self.vault = vault
        self.folder = folder

    def all(self) -> list[Notice]:
        found: list[Notice] = []
        for item in self.vault.list_markdown(self.folder):
            try:
                notice = parse_notice(self.vault.read_text(item.path), item.path)
            except VaultError:
                continue
            if notice is not None:
                found.append(notice)
        found.sort(key=lambda n: n.created, reverse=True)
        return found

    def pending(self) -> list[Notice]:
        return [n for n in self.all() if not n.dismissed]

    def has_kind_on(self, kind: str, when: date, *, count_forced: bool = False) -> bool:
        """Has this check already had its real run for that day.

        This is the whole scheduler. It is answered from the vault, so it
        survives a restart without a state file to keep in step.

        Forced runs do not count. Forcing is how a daily check gets verified
        without waiting a day, and if that satisfied the scheduler then testing
        the morning surface at 02:51 would cancel the 07:00 one, which is
        exactly what happened.
        """
        return any(
            n.kind == kind and n.created.date() == when and (count_forced or not n.forced)
            for n in self.all()
        )

    def write(self, notice: Notice) -> Path:
        target = self.folder / notice.filename()
        if target.exists():
            stem = target.stem
            counter = 2
            while target.exists():
                target = self.folder / f"{stem} {counter}.md"
                counter += 1
        return self.vault.write_new(target, notice.render())

    def dismiss(self, notice: Notice) -> Path:
        """Clear it in the vault, not just on screen.

        Rewrites Ranger's own file, at the operator's explicit command. Nothing
        is deleted: the vault has no delete path and is not getting one, so a
        dismissed notice stays readable as a record.
        """
        if notice.path is None:
            raise VaultError("that notice has no file behind it")
        updated = Notice(
            kind=notice.kind,
            title=notice.title,
            body=notice.body,
            created=notice.created,
            status=STATUS_DISMISSED,
            path=notice.path,
        )
        return self.vault.overwrite(notice.path, updated.render(), allow_overwrite=True)


# -- checks ----------------------------------------------------------------


@dataclass(frozen=True)
class Dueness:
    """Whether a check should run, and in plain words why not.

    A check that is not due yet and a check suppressed by quiet hours are
    different situations with different fixes, and "nothing due" said both. In
    the tier whose whole point is acting while nobody is watching, silence
    reading as broken is the failure mode to design against.
    """

    due: bool
    reason: str


@runtime_checkable
class Check(Protocol):
    name: str
    #: Whether this may run between quiet_start_hour and quiet_end_hour.
    runs_in_quiet_hours: bool

    def status(self, now: datetime, inbox: Inbox) -> Dueness: ...

    async def run(self) -> Notice | None: ...


@dataclass
class MorningSurface:
    """What is slipping, once a day, at the configured hour.

    Calls the existing what_went_quiet tool rather than reimplementing it, so
    the spoken answer and the morning file can never disagree.
    """

    registry: Any
    hour: int
    name: str = "morning"
    runs_in_quiet_hours: bool = False

    def status(self, now: datetime, inbox: Inbox) -> Dueness:
        # Only today. Six missed mornings are not replayed on the seventh,
        # because a week-old list of what went quiet is not news.
        if inbox.has_kind_on(self.name, now.date()):
            return Dueness(False, f"already ran today, next at {self.hour:02d}:00 tomorrow")
        if now.time() < time(hour=self.hour):
            return Dueness(False, f"not due until {self.hour:02d}:00 today")
        return Dueness(True, f"due since {self.hour:02d}:00")

    def due(self, now: datetime, inbox: Inbox) -> bool:
        return self.status(now, inbox).due

    async def run(self) -> Notice | None:
        result = await self.registry.run("what_went_quiet", {})
        if not result.ok:
            return Notice(
                kind=self.name,
                title="The morning check could not run",
                body=result.content,
                created=datetime.now(),
            )
        now = datetime.now()
        return Notice(
            kind=self.name,
            title=f"What went quiet, {day_and_month(now)}",
            body=result.content,
            created=now,
        )


# -- the loop --------------------------------------------------------------


#: Every way a check can end a tick. The operator should never have to guess
#: which one happened.
SURFACED = "surfaced"
NOTHING = "nothing"
NOT_DUE = "not_due"
HELD = "held"
ALREADY_RUNNING = "already_running"
TIMED_OUT = "timed_out"
FAILED = "failed"

_EXECUTED = frozenset({SURFACED, NOTHING, TIMED_OUT, FAILED})


@dataclass(frozen=True)
class CheckOutcome:
    name: str
    state: str
    detail: str = ""

    def render(self) -> str:
        return f"{self.name}: {self.detail}" if self.detail else f"{self.name}: {self.state}"


@dataclass(frozen=True)
class TickReport:
    outcomes: tuple[CheckOutcome, ...] = ()

    def _named(self, *states: str) -> tuple[str, ...]:
        return tuple(o.name for o in self.outcomes if o.state in states)

    @property
    def ran(self) -> tuple[str, ...]:
        return self._named(*_EXECUTED)

    @property
    def surfaced(self) -> tuple[str, ...]:
        return self._named(SURFACED)

    @property
    def skipped_quiet(self) -> tuple[str, ...]:
        return self._named(HELD)

    @property
    def skipped_running(self) -> tuple[str, ...]:
        return self._named(ALREADY_RUNNING)

    @property
    def timed_out(self) -> tuple[str, ...]:
        return self._named(TIMED_OUT)


@dataclass(frozen=True)
class KillSwitch:
    """Proactive behaviour, on or off, held in the vault."""

    vault: Any
    path: Path

    def engaged(self) -> bool:
        try:
            text = self.vault.read_text(self.path)
        except Exception:
            return False
        return "paused: true" in text.lower()

    def set(self, paused: bool, note: str = "") -> Path:
        body = (
            "---\n"
            f"paused: {'true' if paused else 'false'}\n"
            f"changed: {datetime.now().isoformat(timespec='seconds')}\n"
            "---\n\n"
            "# Ranger, proactive behaviour\n\n"
            + (
                "Paused. The heartbeat will not run any checks and will surface nothing.\n"
                "You can still talk to Ranger normally; only the background loop is off.\n\n"
                "Resume with `ranger resume`, or change paused to false above.\n"
                if paused
                else "Running. The heartbeat is active.\n\nPause with `ranger pause`.\n"
            )
            + (f"\n{note}\n" if note else "")
        )
        return self.vault.overwrite(self.path, body, allow_overwrite=True)


class Heartbeat:
    def __init__(
        self,
        config: Config,
        inbox: Inbox,
        checks: list[Check],
        *,
        now: Callable[[], datetime] | None = None,
        kill_switch: KillSwitch | None = None,
        audit: Any = None,
    ) -> None:
        self.config = config
        self.inbox = inbox
        self.checks = checks
        self.now = now or datetime.now
        self.kill_switch = kill_switch
        self.audit = audit
        #: Checks currently in flight. A slow check must not stack up behind
        #: itself when its next turn comes round.
        self._running: set[str] = set()

    def in_quiet_hours(self, when: datetime) -> bool:
        return self.config.schedule.in_quiet_hours(when.hour)

    def quiet_ends_at(self, now: datetime) -> datetime:
        """When the quiet window the operator is currently inside will end."""
        end = self.config.schedule.quiet_end_hour
        candidate = now.replace(hour=end, minute=0, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    async def tick(self, force: tuple[str, ...] = ()) -> TickReport:
        """One pass. Every check ends with a stated outcome, never silence."""
        now = self.now()
        if self.kill_switch is not None and self.kill_switch.engaged():
            # One switch, everything proactive off, nothing torn down.
            return TickReport(
                outcomes=tuple(
                    CheckOutcome(c.name, NOT_DUE, "paused: the kill switch is engaged")
                    for c in self.checks
                )
            )
        quiet = self.in_quiet_hours(now)
        forced = {name.strip().lower() for name in force}
        force_all = "all" in forced
        outcomes: list[CheckOutcome] = []

        for check in self.checks:
            compelled = force_all or check.name.lower() in forced

            if check.name in self._running:
                outcomes.append(
                    CheckOutcome(check.name, ALREADY_RUNNING,
                                 "still running from the last pass, skipped rather than stacked")
                )
                continue

            dueness = check.status(now, self.inbox)
            if not dueness.due and not compelled:
                outcomes.append(CheckOutcome(check.name, NOT_DUE, dueness.reason))
                continue

            if quiet and not check.runs_in_quiet_hours and not compelled:
                # Not dropped, just not now. It stays due, so it fires when the
                # quiet window ends.
                until = self.quiet_ends_at(now)
                outcomes.append(
                    CheckOutcome(check.name, HELD,
                                 f"held by quiet hours, will run after {until:%H:%M}")
                )
                continue

            self._running.add(check.name)
            state = SURFACED
            detail = ""
            try:
                notice = await asyncio.wait_for(
                    check.run(), timeout=self.config.heartbeat.check_timeout_seconds
                )
            except asyncio.TimeoutError:
                state = TIMED_OUT
                seconds = self.config.heartbeat.check_timeout_seconds
                detail = f"ran longer than {seconds}s and was stopped, nothing was changed"
                notice = Notice(
                    kind=check.name,
                    title=f"The {check.name} check timed out",
                    body=(
                        f"It ran for longer than {seconds} seconds and was stopped. "
                        "Nothing was changed. The loop is still running."
                    ),
                    created=now,
                )
            except Exception as exc:
                state = FAILED
                detail = f"{type(exc).__name__}: {exc}"
                notice = Notice(
                    kind=check.name,
                    title=f"The {check.name} check failed",
                    body=f"{type(exc).__name__}: {exc}",
                    created=now,
                )
            finally:
                self._running.discard(check.name)

            if notice is None:
                outcomes.append(
                    CheckOutcome(check.name, NOTHING, "ran, nothing worth surfacing")
                )
                continue

            if compelled:
                notice = replace(notice, forced=True)
            try:
                path = self.inbox.write(notice)
            except VaultError as exc:
                outcomes.append(CheckOutcome(check.name, FAILED, f"could not write the notice: {exc}"))
                continue

            if state == SURFACED:
                detail = f"surfaced to {path.name}"
                if compelled:
                    detail += " (forced, so the scheduled run still stands)"
            else:
                detail = f"{detail}, noted in {path.name}"
            outcomes.append(CheckOutcome(check.name, state, detail))

        if self.audit is not None:
            for outcome in outcomes:
                if outcome.state in _EXECUTED:
                    try:
                        self.audit.write("heartbeat", outcome.render(), origin="heartbeat")
                    except Exception:
                        pass

        return TickReport(outcomes=tuple(outcomes))

    async def run(self, stop: Any = None) -> None:
        interval = self.config.heartbeat.interval_seconds
        while True:
            await self.tick()
            if stop is not None and stop.is_set():
                return
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return


def build_checks(config: Config, registry: Any) -> list[Check]:
    """Tier 5 has one check. Adding a second is one entry here."""
    return [MorningSurface(registry=registry, hour=config.schedule.morning_hour)]
