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
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from .config import Config
from .dates import day_and_month
from .vault import Vault, VaultError

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

    def has_kind_on(self, kind: str, when: date) -> bool:
        """Has this check already produced a notice for that day.

        This is the whole scheduler. It is answered from the vault, so it
        survives a restart without a state file to keep in step.
        """
        return any(n.kind == kind and n.created.date() == when for n in self.all())

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


@runtime_checkable
class Check(Protocol):
    name: str
    #: Whether this may run between quiet_start_hour and quiet_end_hour.
    runs_in_quiet_hours: bool

    def due(self, now: datetime, inbox: Inbox) -> bool: ...

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

    def due(self, now: datetime, inbox: Inbox) -> bool:
        if now.time() < time(hour=self.hour):
            return False
        # Only today. Six missed mornings are not replayed on the seventh,
        # because a week-old list of what went quiet is not news.
        return not inbox.has_kind_on(self.name, now.date())

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


@dataclass
class TickReport:
    ran: tuple[str, ...] = ()
    surfaced: tuple[str, ...] = ()
    skipped_quiet: tuple[str, ...] = ()
    skipped_running: tuple[str, ...] = ()
    timed_out: tuple[str, ...] = ()


class Heartbeat:
    def __init__(
        self,
        config: Config,
        inbox: Inbox,
        checks: list[Check],
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.inbox = inbox
        self.checks = checks
        self.now = now or datetime.now
        #: Checks currently in flight. A slow check must not stack up behind
        #: itself when its next turn comes round.
        self._running: set[str] = set()

    def in_quiet_hours(self, when: datetime) -> bool:
        return self.config.schedule.in_quiet_hours(when.hour)

    async def tick(self) -> TickReport:
        now = self.now()
        quiet = self.in_quiet_hours(now)
        ran: list[str] = []
        surfaced: list[str] = []
        skipped_quiet: list[str] = []
        skipped_running: list[str] = []
        timed_out: list[str] = []

        for check in self.checks:
            if check.name in self._running:
                skipped_running.append(check.name)
                continue
            if not check.due(now, self.inbox):
                continue
            if quiet and not check.runs_in_quiet_hours:
                # Not dropped, just not now. It stays due, so it fires when the
                # quiet window ends.
                skipped_quiet.append(check.name)
                continue

            self._running.add(check.name)
            try:
                notice = await asyncio.wait_for(
                    check.run(), timeout=self.config.heartbeat.check_timeout_seconds
                )
            except asyncio.TimeoutError:
                timed_out.append(check.name)
                notice = Notice(
                    kind=check.name,
                    title=f"The {check.name} check timed out",
                    body=(
                        f"It ran for longer than "
                        f"{self.config.heartbeat.check_timeout_seconds} seconds and was "
                        "stopped. Nothing was changed. The loop is still running."
                    ),
                    created=now,
                )
            except Exception as exc:
                notice = Notice(
                    kind=check.name,
                    title=f"The {check.name} check failed",
                    body=f"{type(exc).__name__}: {exc}",
                    created=now,
                )
            finally:
                self._running.discard(check.name)

            ran.append(check.name)
            if notice is not None:
                try:
                    self.inbox.write(notice)
                    surfaced.append(check.name)
                except VaultError:
                    pass

        return TickReport(
            ran=tuple(ran),
            surfaced=tuple(surfaced),
            skipped_quiet=tuple(skipped_quiet),
            skipped_running=tuple(skipped_running),
            timed_out=tuple(timed_out),
        )

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
