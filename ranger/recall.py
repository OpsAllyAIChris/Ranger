"""Item K1. Reading back the log Ranger wrote.

Ranger has written `Ranger/log/<date>.md` since Tier 6 and has never been able
to read it. **The fourth instance of the same shape**: a write path with no read
path, after drafts, after the account marker, after GP. The pattern is worth
naming -- a thing Jarvis produces and cannot look at is a thing the operator has
to open Obsidian to check on its behalf.

## What this is not

It is not a memory, and it does not decide what happened. Python parses the
table rows, groups them by day and picks which ones to show; the model reads the
result out. Nothing here summarises, infers or reconciles. "Have I already
looked at Petmate" is answered by finding the rows that mention Petmate and
giving them with their dates, not by concluding anything about Petmate.

## Everything here is history and says so

Inherited from the time-blind writing fix, and it matters more here, because
**the log is nothing but old conclusions**. A three-week-old "waiting on their
reply" recalled as the state of today is the Telly problem with a longer fuse.

So every line this module produces carries its date, in the past tense, built
that way in Python rather than requested of the model:

    On 8 September you asked: where are we on Illes Foods

There is no shape in the output that can be read as the present.

## Untrusted, and more so than most

The log holds transcripts of what the operator said, and what the operator said
routinely includes pasted customer email and vendor text. Trust attaches to the
path bytes travelled, not to who wrote the file: these bytes came from outside,
went through the log, and are coming back. They go to the model fenced, exactly
as a dropped file does.

## Bounded, and it says what it left out

The log grows every day and returning it whole is a token disaster. A digest by
default, a single day in full on request, and a line naming what was not shown.
Silent truncation of a spreadsheet is bad; silent truncation of history is worse,
because the operator cannot tell the difference between "that did not happen"
and "that did not fit".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable

#: How many days a digest reaches back over when nothing is asked for.
DEFAULT_DAYS = 7

#: The most days one call will ever read, however many are asked for.
MAX_DAYS = 31

#: Entries shown per day in a digest, and in a single day asked for in full.
DIGEST_PER_DAY = 6
FULL_PER_DAY = 60

#: How much of one entry's detail is shown. The log itself caps a cell at 400.
DETAIL_CHARS = 200

#: The rows worth putting in a digest, in the order they are worth reading.
#: A digest that led with tool calls would bury what the operator actually
#: asked for under the machinery of answering it.
DIGEST_KINDS = ("turn", "confirmation", "tool", "reply", "error", "interrupted")

#: Rows that are noise in a digest: they are about Jarvis's own housekeeping
#: rather than about the work. Still returned when a day is asked for in full.
QUIET_KINDS = ("heartbeat", "voice", "speech interrupted", "vault snapshot")

_ROW = re.compile(
    r"^\|\s*(?P<time>\d{2}:\d{2}:\d{2})\s*\|\s*(?P<origin>[^|]*?)\s*\|"
    r"\s*(?P<kind>[^|]*?)\s*\|\s*(?P<detail>.*?)\s*\|\s*$"
)


@dataclass(frozen=True)
class Entry:
    """One logged row, with the day it belongs to attached.

    The day comes from the file name rather than from the row, because the row
    only carries a time. An entry that lost its date would be exactly the
    time-blind line this module exists to avoid producing.
    """

    day: date
    time: str
    origin: str
    kind: str
    detail: str

    @property
    def when(self) -> datetime:
        hour, minute, second = (int(part) for part in self.time.split(":"))
        return datetime.combine(self.day, datetime.min.time()).replace(
            hour=hour, minute=minute, second=second
        )


def parse_day(text: str, day: date) -> list[Entry]:
    """The rows of one log file. A line that is not a row is not an error."""
    found: list[Entry] = []
    for line in text.splitlines():
        match = _ROW.match(line)
        if not match:
            continue
        if match.group("time") == "time" or set(match.group("origin")) <= {"-", " "}:
            continue  # the header and its divider
        found.append(
            Entry(
                day=day,
                time=match.group("time"),
                origin=match.group("origin"),
                kind=match.group("kind"),
                detail=match.group("detail").replace("\\|", "|"),
            )
        )
    return found


def read_days(log: Any, days: Iterable[date]) -> list[Entry]:
    """Every entry across these days, oldest first."""
    found: list[Entry] = []
    for day in days:
        found.extend(parse_day(log.read(day), day))
    return sorted(found, key=lambda entry: entry.when)


def within(available: list[date], *, days: int = DEFAULT_DAYS,
           today: date | None = None) -> list[date]:
    """The logged days inside a window, most recent first.

    Days that exist, not days in the calendar: a week with three quiet days in
    it should not produce three "nothing logged" headings.
    """
    span = max(1, min(int(days or DEFAULT_DAYS), MAX_DAYS))
    if today is None:
        return sorted(available, reverse=True)[:span]
    earliest = today - timedelta(days=span - 1)
    return [day for day in sorted(available, reverse=True) if day >= earliest]


def matching(entries: list[Entry], about: str) -> list[Entry]:
    """Entries mentioning a word. A plain search, and nothing cleverer.

    Case-insensitive substring, because the thing being searched is a
    transcript and the operator asking "have I looked at Petmate" is asking
    whether the word appears, not for a judgement about Petmate.
    """
    needle = " ".join(str(about or "").split()).lower()
    if not needle:
        return list(entries)
    return [
        entry for entry in entries
        if needle in entry.detail.lower() or needle in entry.kind.lower()
    ]


def _said(day: date) -> str:
    """8 September. The date, in words, at the front of every line."""
    return f"{day.day} {day.strftime('%B')}"


def _trim(text: str, limit: int = DETAIL_CHARS) -> tuple[str, bool]:
    body = " ".join(str(text).split())
    if len(body) <= limit:
        return body, False
    return body[: limit - 1].rstrip() + "…", True


def line_for(entry: Entry) -> str:
    """One entry, in the past tense, with its date in front of it.

    **The tense is built here, not asked for.** A model told to use the past
    tense will mostly use the past tense; a line that has no present-tense form
    cannot be read as one.
    """
    detail, _ = _trim(entry.detail)
    when = f"On {_said(entry.day)} at {entry.time[:5]}"
    if entry.kind == "turn":
        return f"{when} you asked: {detail}"
    if entry.kind == "reply":
        return f"{when} Jarvis answered: {detail}"
    if entry.kind == "tool":
        return f"{when} Jarvis ran {detail}"
    if entry.kind == "confirmation":
        return f"{when} you were asked to confirm {detail}"
    if entry.kind == "error":
        return f"{when} something failed: {detail}"
    if entry.kind == "interrupted":
        return f"{when} a turn was interrupted: {detail}"
    return f"{when} {entry.kind}: {detail}"


@dataclass
class Recollection:
    """What was found, what was shown, and what was left out."""

    days: list[date] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    shown: int = 0
    total: int = 0
    #: Days that had entries but none that survived the filter.
    quiet: list[date] = field(default_factory=list)
    about: str = ""
    searched: int = 0

    @property
    def truncated(self) -> bool:
        return self.shown < self.total

    def render(self) -> str:
        if not self.total:
            return self._nothing()
        head = [
            "This is history, read out of Ranger's own log. Every line below "
            "already carries the date it happened on. Report it in the past "
            "tense and keep the dates on it: none of it describes today, and a "
            "three week old conclusion repeated as the state of an account now "
            "is the failure this is written to avoid.",
            "",
        ]
        body = list(self.lines)
        tail = [""]
        if self.truncated:
            tail.append(
                f"Showing {self.shown} of {self.total} matching entries across "
                f"{len(self.days)} day(s). {self.total - self.shown} were not "
                "shown. Ask for a single day to see it in full, and say that "
                "some were left out rather than implying this is everything."
            )
        else:
            tail.append(
                f"That is all {self.total} matching entr{'y' if self.total == 1 else 'ies'} "
                f"across {len(self.days)} day(s). Nothing was left out."
            )
        if self.quiet:
            tail.append(
                "Days with entries but nothing matching: "
                + ", ".join(_said(day) for day in self.quiet) + "."
            )
        return "\n".join(head + body + tail)

    def _nothing(self) -> str:
        """Absence, said as absence. Never an empty list the model fills in."""
        if self.about:
            where = (
                f"across {len(self.days)} logged day(s)" if self.days
                else "and there are no logged days in that window"
            )
            return (
                f"Nothing in the log mentions {self.about!r} {where}. That means "
                "it was not logged, which is not the same as it not having "
                "happened -- say so that way round. The log holds what Jarvis "
                "did and what was said to it, not what the operator did "
                "elsewhere."
            )
        return (
            "There is nothing logged in that window. Say that rather than "
            "reaching for what you remember of the conversation: an empty log "
            "is a fact about the log."
        )

    def summary(self) -> str:
        if not self.total:
            return "nothing logged"
        return (
            f"{self.shown} of {self.total} entries, "
            f"{len(self.days)} day(s)"
        )


def recollect(
    log: Any,
    *,
    days: int = DEFAULT_DAYS,
    day: date | None = None,
    about: str = "",
    today: date | None = None,
) -> Recollection:
    """The whole read, assembled in Python.

    One day asked for is that day in full. Otherwise a digest across the
    window, most recent first, with the noisier housekeeping rows dropped --
    they are still there when the day is asked for.
    """
    available = log.days()
    if day is not None:
        wanted = [day] if day in available else []
        per_day, kinds = FULL_PER_DAY, None
    else:
        wanted = within(available, days=days, today=today)
        per_day, kinds = DIGEST_PER_DAY, set(DIGEST_KINDS)

    found = Recollection(days=list(wanted), about=" ".join(str(about or "").split()))
    if not wanted:
        return found

    entries = read_days(log, wanted)
    found.searched = len(entries)
    kept = matching(entries, about)
    if kinds is not None and not about:
        # A search reaches everything. A digest does not, or the shape of a
        # week is buried under the machinery of answering it.
        kept = [entry for entry in kept if entry.kind in kinds]
    found.total = len(kept)

    by_day: dict[date, list[Entry]] = {}
    for entry in kept:
        by_day.setdefault(entry.day, []).append(entry)

    for one in sorted(wanted, reverse=True):
        entries_for_day = by_day.get(one, [])
        if not entries_for_day:
            found.quiet.append(one)
            continue
        # The most recent of each day first, so a long day shows how it ended
        # rather than how it opened.
        shown = sorted(entries_for_day, key=lambda e: e.when, reverse=True)[:per_day]
        for entry in sorted(shown, key=lambda e: e.when):
            found.lines.append(line_for(entry))
        found.shown += len(shown)
        if len(entries_for_day) > len(shown):
            found.lines.append(
                f"  ({len(entries_for_day) - len(shown)} more on "
                f"{_said(one)}, not shown)"
            )
    return found
