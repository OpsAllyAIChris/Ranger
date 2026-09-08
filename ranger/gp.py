"""Gross profit, entered by hand and computed in Python.

The first dashlet, and the one that sets the rule for all of them: **Python
computes the number and the model reads it.** A dashlet showing a figure a
language model arrived at is worse than no dashlet, because the operator will
act on it. Nothing in this file talks to a provider, and a test asserts that.

Three decisions worth stating, because they shape how the folder reads.

**A correction is a new entry, not an edit.** Delete-never holds here as
everywhere, so a figure that turns out wrong is superseded rather than changed.
Each entry is its own create-only note, and reading takes the newest entry for
each period. The folder is therefore a record of what was believed and when,
which is more useful than a file that only ever shows the current answer.

**Absence is never zero.** A month with no entry reads as "not entered", and
the panel says so. Rendering a missing figure as 0 is the one failure mode that
gets acted on, because a zero looks like a fact.

**Money is Decimal.** Never float. A gross profit figure that is off by a cent
because of binary rounding is a figure that stops being trusted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

FOLDER = "gp"

#: `2026-09`. The period a figure belongs to, which is not the day it was
#: entered: a September figure often arrives in October.
PERIOD = re.compile(r"^(?P<year>\d{4})-(?P<month>0[1-9]|1[0-2])$")

_FIELD = re.compile(r"^(?P<key>[a-z_]+):[ \t]*(?P<value>.*?)[ \t]*$", re.MULTILINE)
_FRONT = re.compile(r"\A---\n(?P<body>.*?)\n---\n", re.DOTALL)
#: Anything that is not a digit, a minus or a decimal point. Strips "$", commas
#: and stray spaces so the operator can paste a figure as they received it.
_NOT_MONEY = re.compile(r"[^\d.\-]")
#: The `(2)` on a filename when two entries land in the same second. It is the
#: write order, and it is the only thing that separates them: `recorded` is
#: stored to the second, so two entries a moment apart carry the same stamp.
_COLLISION = re.compile(r" \((?P<n>\d+)\)$")


class BadEntry(ValueError):
    """The figure or the period could not be read."""


def parse_amount(raw: Any) -> Decimal:
    """A money figure from whatever the operator typed.

    "$48,250.00", "48250", "48,250" all mean the same thing. A figure that
    cannot be read is refused rather than guessed at: entering the wrong number
    silently is the failure this whole module is arranged against.
    """
    text = _NOT_MONEY.sub("", str(raw or "").strip())
    if not text or text in ("-", ".", "-."):
        raise BadEntry(f"{raw!r} is not a number")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise BadEntry(f"{raw!r} is not a number") from exc


def parse_period(raw: Any, *, today: date | None = None) -> str:
    """`2026-09`, or today's month when nothing was said."""
    text = str(raw or "").strip()
    if not text:
        return (today or date.today()).strftime("%Y-%m")
    if not PERIOD.match(text):
        raise BadEntry(f"{raw!r} is not a month. Write it as 2026-09")
    return text


@dataclass(frozen=True)
class Entry:
    """One figure, as it was entered. Never edited afterwards."""

    period: str
    amount: Decimal
    recorded: datetime
    note: str = ""
    path: Path | None = None

    @property
    def year(self) -> str:
        return self.period[:4]

    def render(self) -> str:
        lines = [
            "---",
            f"period: {self.period}",
            f"amount: {self.amount}",
            f"recorded: {self.recorded.isoformat(timespec='seconds')}",
            "---",
            "",
            f"# Gross profit for {self.period}",
            "",
            f"{money(self.amount)}",
        ]
        if self.note:
            lines += ["", self.note.strip()]
        return "\n".join(lines) + "\n"


def money(amount: Decimal, symbol: str = "$") -> str:
    """A figure a person reads, with thousands separators and two decimals."""
    quantised = amount.quantize(Decimal("0.01"))
    sign = "-" if quantised < 0 else ""
    return f"{sign}{symbol}{abs(quantised):,.2f}"


def parse_entry(text: str, path: Path | None = None) -> Entry | None:
    """One entry note, or None if it is not one. A stray file is not an error."""
    front = _FRONT.match(text)
    if not front:
        return None
    fields = {m.group("key"): m.group("value") for m in _FIELD.finditer(front.group("body"))}
    if "period" not in fields or "amount" not in fields:
        return None
    try:
        period = parse_period(fields["period"])
        amount = parse_amount(fields["amount"])
    except BadEntry:
        return None
    try:
        recorded = datetime.fromisoformat(fields.get("recorded", ""))
    except ValueError:
        recorded = datetime.min
    body = text[front.end():]
    note = "\n".join(
        line for line in body.splitlines()
        if line.strip() and not line.startswith("#") and not line.strip().startswith("$")
    ).strip()
    return Entry(period=period, amount=amount, recorded=recorded, note=note, path=path)


def order(entry: Entry) -> tuple[datetime, int]:
    """Write order. `recorded` first, then the collision counter.

    Sorting ties by the filename looked reasonable and was wrong, which the
    tests did not catch because every fixture corrected a figure on a different
    day. A person correcting a figure does it seconds after noticing, so both
    entries carry the same second, and `"... 213149 (2).md"` sorts *before*
    `"... 213149.md"` -- a space is lower than a dot. The correction lost to the
    figure it was correcting, silently, in the year-to-date total.

    The counter is the write order: `write` only adds `(2)` because `(1)` was
    already on disk. So that is what this sorts by.
    """
    if entry.path is None:
        return (entry.recorded, 1)
    found = _COLLISION.search(entry.path.stem)
    return (entry.recorded, int(found.group("n")) if found else 1)


@dataclass
class Ledger:
    """Every entry ever made, and what the current answer is.

    Current means newest by `recorded`, per period. Two entries for the same
    month is a correction, and the later one wins; the earlier one stays on
    disk, because what was believed on the day is worth keeping.
    """

    entries: list[Entry] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)

    def current(self) -> dict[str, Entry]:
        latest: dict[str, Entry] = {}
        for entry in sorted(self.entries, key=order):
            latest[entry.period] = entry
        return latest

    def corrections(self, period: str) -> int:
        """How many times this period has been superseded."""
        return max(0, len([e for e in self.entries if e.period == period]) - 1)

    def for_year(self, year: str) -> list[Entry]:
        return [e for e in self.current().values() if e.year == year]

    def newest(self) -> Entry | None:
        current = list(self.current().values())
        return max(current, key=order) if current else None


def read(vault: Any, folder: Path) -> Ledger:
    """Every entry note in the folder. A file that is not one is skipped."""
    ledger = Ledger()
    if not folder.is_dir():
        return ledger
    for note in sorted(folder.glob("*.md")):
        try:
            text = vault.read_text(note)
        except Exception as exc:
            ledger.unreadable.append(f"{note.name}: {exc}")
            continue
        entry = parse_entry(text, note)
        if entry is not None:
            ledger.entries.append(entry)
    return ledger


def write(vault: Any, folder: Path, entry: Entry) -> Path:
    """One create-only note per entry. Never an edit.

    Named by the period and the moment it was recorded, so the folder sorts by
    what the figure is *for* and a correction sits beside what it corrects
    rather than replacing it.
    """
    stem = f"{entry.period} entered {entry.recorded.strftime('%Y-%m-%d %H%M%S')}"
    target = folder / f"{stem}.md"
    counter = 2
    while target.exists():
        target = folder / f"{stem} ({counter}).md"
        counter += 1
    return vault.write_new(target, entry.render())


# -- what the panel shows ---------------------------------------------------


@dataclass(frozen=True)
class Totals:
    """What Python computed. The model reads this and never recomputes it."""

    year: str
    month: str
    ytd: Decimal | None
    mtd: Decimal | None
    months_counted: int
    as_of: datetime | None
    #: The last completed year's total, so the first of January does not look
    #: like the dashlet broke.
    last_year: str = ""
    last_year_total: Decimal | None = None
    corrections: int = 0

    @property
    def empty(self) -> bool:
        return self.as_of is None

    def days_old(self, today: date) -> int | None:
        return None if self.as_of is None else (today - self.as_of.date()).days


def totals(ledger: Ledger, today: date, *, year_starts_month: int = 1) -> Totals:
    """Year to date and month to date, from the current entry for each month.

    `year_starts_month` is here because a fiscal year that does not start in
    January is ordinary, and guessing wrong would put the number quietly in the
    wrong bucket rather than failing.
    """
    current = ledger.current()
    month = today.strftime("%Y-%m")

    year_start = date(today.year if today.month >= year_starts_month else today.year - 1,
                      year_starts_month, 1)
    label = str(year_start.year) if year_starts_month == 1 else f"FY{year_start.year}"

    def within(entry: Entry, start: date) -> bool:
        stamp = date(int(entry.period[:4]), int(entry.period[5:]), 1)
        return start <= stamp <= date(today.year, today.month, 1)

    this_year = [e for e in current.values() if within(e, year_start)]
    ytd = sum((e.amount for e in this_year), Decimal(0)) if this_year else None

    previous_start = date(year_start.year - 1, year_starts_month, 1)
    previous = [
        e for e in current.values()
        if previous_start <= date(int(e.period[:4]), int(e.period[5:]), 1) < year_start
    ]
    newest = ledger.newest()

    return Totals(
        year=label,
        month=month,
        ytd=ytd,
        mtd=current[month].amount if month in current else None,
        months_counted=len(this_year),
        as_of=newest.recorded if newest else None,
        last_year=str(previous_start.year) if year_starts_month == 1 else f"FY{previous_start.year}",
        last_year_total=sum((e.amount for e in previous), Decimal(0)) if previous else None,
        corrections=ledger.corrections(month),
    )


# -- the one way in ---------------------------------------------------------


def folder_for(config: Any) -> Path:
    """`Ranger/gp`. Under Ranger's own folder, so the vault's write wall covers it."""
    return config.vault.ranger / FOLDER


def record(
    config: Any,
    vault: Any,
    amount: Any,
    *,
    period: Any = "",
    note: str = "",
    now: datetime | None = None,
) -> Entry:
    """Enter a figure. The only write path, used by the panel and the CLI alike.

    One implementation, so "entered in the panel" and "entered at the terminal"
    cannot mean two different things on disk. Create-only: a second figure for
    the same month is a correction, which is a new note beside the old one, not
    an edit of it.

    Nothing about this is an agent action. The operator types a number they
    were given; the model is not in the path and cannot be, which is the point
    of the whole module.
    """
    now = now or datetime.now()
    entry = Entry(
        period=parse_period(period, today=now.date()),
        amount=parse_amount(amount),
        recorded=now.replace(microsecond=0),
        note=(note or "").strip(),
    )
    path = write(vault, folder_for(config), entry)
    return Entry(
        period=entry.period,
        amount=entry.amount,
        recorded=entry.recorded,
        note=entry.note,
        path=path,
    )


def ledger_for(config: Any, vault: Any) -> Ledger:
    return read(vault, folder_for(config))


def summary(config: Any, vault: Any, today: date) -> tuple[Ledger, Totals]:
    """What the panel, the CLI and any future caller all read. Computed once, here."""
    ledger = ledger_for(config, vault)
    return ledger, totals(
        ledger, today, year_starts_month=config.gp.year_starts_month
    )


# -- what the panel draws ---------------------------------------------------


def reading(config: Any, vault: Any, *, today: date, now: datetime | None = None):
    """The GP dashlet. A Python-computed read; nothing here asks a model anything.

    The empty case is the one worth reading twice. When nothing has been
    entered the reading has no value at all, and the panel prints "no GP
    entered yet". It never prints the currency symbol and a nought, because a
    zero looks like a figure that was measured rather than one that is missing,
    and the operator would act on it.

    Notes attached to entries deliberately do not appear here. They are the
    operator's own scribbles, they can contain anything pasted from anywhere,
    and the dashlet's job is the number.
    """
    from .dashlets import Reading
    from .dates import human_datetime

    ledger, total = summary(config, vault, today)
    symbol = config.gp.currency
    stale_after = config.gp.stale_after_days
    age = total.days_old(today)

    detail_parts: list[str] = []
    if total.mtd is not None:
        detail_parts.append(f"{total.month} {money(total.mtd, symbol)}")
    else:
        detail_parts.append(f"{total.month} not entered")
    if total.months_counted:
        months = "month" if total.months_counted == 1 else "months"
        detail_parts.append(f"{total.months_counted} {months} counted")
    if total.last_year_total is not None:
        detail_parts.append(f"{total.last_year} {money(total.last_year_total, symbol)}")
    if total.corrections:
        corrections = "correction" if total.corrections == 1 else "corrections"
        detail_parts.append(f"{total.corrections} {corrections} this month")
    if ledger.unreadable:
        detail_parts.append(f"{len(ledger.unreadable)} unreadable")

    if total.ytd is None:
        # No figure for this year. Say which state it is: nothing has ever been
        # entered, or the year turned over and this one has not started yet.
        empty = (
            "no GP entered yet"
            if total.empty
            else f"nothing entered for {total.year} yet"
        )
        return Reading(
            key="gp",
            title=f"Gross Profit {total.year}",
            value="",
            detail=" · ".join(detail_parts),
            as_of=human_datetime(total.as_of) if total.as_of else "",
            age_days=age,
            stale=False,
            empty=empty,
            extra={"period": total.month, "currency": symbol},
        )

    return Reading(
        key="gp",
        title=f"Gross Profit {total.year}",
        value=money(total.ytd, symbol),
        detail=" · ".join(detail_parts),
        as_of=human_datetime(total.as_of) if total.as_of else "",
        age_days=age,
        stale=age is not None and age > stale_after,
        empty="",
        extra={"period": total.month, "currency": symbol},
    )


# -- out of the vault -------------------------------------------------------


def export_spec(config: Any, vault: Any, today: date):
    """The ledger as a document spec. Values only, no formulas.

    The first thing item D gets used for, and deliberately the simplest: a
    workbook of numbers Python has already added up. **No formulas**, so the
    preview's "formulas are not shown" caveat cannot bite on the first document
    the operator opens -- what the preview shows is the whole of what is in the
    file.

    Superseded entries are in their own sheet rather than dropped. A figure
    that was corrected is part of the record, and a spreadsheet that quietly
    omitted it would be the one place in this repository where something got
    thrown away.
    """
    from . import documents as docs

    ledger, total = summary(config, vault, today)
    symbol = config.gp.currency
    current = ledger.current()

    months = docs.table(
        ["Period", "Gross profit", "Recorded"],
        [
            [period, money(entry.amount, symbol),
             entry.recorded.strftime("%Y-%m-%d %H:%M")]
            for period, entry in sorted(current.items())
        ],
        name="By month",
    )

    figures = [["Year", total.year],
               ["Year to date",
                money(total.ytd, symbol) if total.ytd is not None else "nothing entered"],
               ["Months entered", str(total.months_counted)],
               ["This month", total.month],
               ["Month to date",
                money(total.mtd, symbol) if total.mtd is not None else "not entered"]]
    if total.last_year_total is not None:
        figures.append([f"{total.last_year} total", money(total.last_year_total, symbol)])
    if total.as_of:
        figures.append(["Newest entry", total.as_of.strftime("%Y-%m-%d %H:%M")])
    summary_table = docs.table([], figures, name="Summary")

    blocks = [summary_table, months]

    superseded = [e for e in ledger.entries if e.path not in {c.path for c in current.values()}]
    if superseded:
        blocks.append(
            docs.table(
                ["Period", "Gross profit", "Recorded"],
                [[e.period, money(e.amount, symbol), e.recorded.strftime("%Y-%m-%d %H:%M")]
                 for e in sorted(superseded, key=lambda e: (e.period, order(e)))],
                name="Superseded",
            )
        )

    return docs.Spec(
        title=f"Gross profit {total.year}",
        subtitle=(
            "Every figure entered by hand and added up in Python. "
            "Values only: nothing in this file is a formula."
        ),
        blocks=tuple(blocks),
        source=docs.provenance("Ranger/gp"),
    )


def export(config: Any, vault: Any, today: date, kind: str = "xlsx"):
    """Write the ledger into the drafts folder, through the document seam."""
    from . import documents as docs

    return docs.generate(
        vault,
        config.vault.drafts,
        export_spec(config, vault, today),
        kind,
        today=today,
        page_size=config.documents.page_size,
    )
