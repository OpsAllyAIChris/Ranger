"""Tier 5. The morning brief.

`accounts.quiet_after_days` decides what is *eligible*. This decides what the
operator actually reads, which is a different job and was not being done by
anything. With 58 accounts a plain threshold produced 41 lines sorted longest
first, topped by an account at 215 days: most of the book, ordered by how
thoroughly each one had been abandoned. That is the list least likely to be
acted on, and raising the threshold only moves which accounts are invisible.

So: a brief has a size, not a threshold. Five lines, every morning, whatever
the underlying numbers do, in three buckets.

**Slipping.** Crossed the threshold since the last brief. The only genuinely
new information in the report, because an account quiet 25, then 26, then 27
days is the same fact three times. Knowing this needs a record of what was
reported before, which is `quiet-seen.md`.

**Deals going cold.** Past the threshold with a live opportunity. A deal going
quiet is a different emergency from a relationship going quiet.

**One decision.** A single cold account, asked about rather than reported.
Capital equipment here runs a twelve to eighteen month cycle, so quiet since
March is mid-cycle and not dead: the operator judges case by case, and the
answer is remembered. This is what drains a backlog of forty instead of
reprinting it every morning.

Ranking inside a bucket is (tier, activity count, recency of the lapse), and it
is that order deliberately. Activity count is the cheapest available proxy for
"was this ever a real relationship": an account with two touches ever did not
lapse, it never started. Recency last, because a lapse of 25 days is a save and
a lapse of 80 is a decision, and the save is what a morning is for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .accounts import NoteScan
from .config import AccountsConfig, BriefConfig
from .dates import human_day

SLIPPING = "slipping"
DEAL = "deal"
DECISION = "decision"

# - 2026-09-07 | Illes Foods
_SEEN_LINE = re.compile(r"^-[ \t]+(?P<date>\d{4}-\d{2}-\d{2})[ \t]*\|[ \t]*(?P<name>.+?)[ \t]*$")
# - Gabriel Ranch Beef      (anything after a # is a note to self)
_DORMANT_LINE = re.compile(r"^-[ \t]+(?P<name>[^#]+?)[ \t]*(?:#.*)?$")

SEEN_HEADER = """\
# Quiet since

When Jarvis first reported each account as quiet. It uses this to tell a new
lapse from one it has already mentioned, so the morning brief can report a
change rather than repeating a state.

Jarvis maintains this. Deleting a line makes that account look newly quiet
tomorrow, which is a reasonable way to make it resurface.

"""

DORMANT_HEADER = """\
# Dormant

Accounts to stop surfacing in the morning brief. One per line. Add a note after
a # if you want to remember why.

This lives here rather than in the account note because the CRM export
regenerates those and would wipe it. Delete a line to bring an account back.

"""


@dataclass(frozen=True)
class Line:
    """One account, and why it is on the list."""

    account: str
    bucket: str
    days: int
    activities: int
    tier: str
    opportunities: int = 0

    def render(self) -> str:
        parts = [f"{self.account}"]
        if self.bucket == SLIPPING:
            parts.append(f"quiet {self.days} days, just crossed")
        elif self.bucket == DEAL:
            deal = "deal" if self.opportunities == 1 else f"{self.opportunities} deals"
            parts.append(f"{deal} open, quiet {self.days} days")
        else:
            parts.append(f"quiet {self.days} days")
        parts.append(f"{self.activities} activit{'y' if self.activities == 1 else 'ies'}")
        if self.tier:
            parts.append(self.tier)
        return ", ".join(parts)


@dataclass(frozen=True)
class Brief:
    as_of: date
    threshold_days: int
    slipping: tuple[Line, ...] = ()
    deals: tuple[Line, ...] = ()
    decision: Line | None = None
    #: Quiet, eligible, and not shown. The number the operator can ask for.
    withheld: int = 0
    never_touched: tuple[str, ...] = ()
    dormant: int = 0
    skipped: int = 0
    active: int = 0
    #: Nothing had been recorded yet, so every quiet account looked new. They
    #: are recorded rather than announced: "just crossed" must never be a lie.
    first_run: bool = False

    @property
    def empty(self) -> bool:
        return not self.slipping and not self.deals and self.decision is None

    def render(self) -> str:
        out: list[str] = [
            f"As of {human_day(self.as_of)}, quiet means no logged activity for "
            f"more than {self.threshold_days} days."
        ]

        if self.slipping:
            out.append("")
            out.append("Just went quiet")
            out += [f"- {line.render()}" for line in self.slipping]

        if self.deals:
            out.append("")
            out.append("Deals going cold")
            out += [f"- {line.render()}" for line in self.deals]

        if self.decision is not None:
            out.append("")
            out.append("One to decide")
            out.append(
                f"- {self.decision.render()}. Keep it in the morning list, or let it go?"
            )

        if self.empty:
            out.append("")
            out.append("Nothing new is slipping and no open deal has gone quiet.")

        if self.first_run:
            out.append("")
            out.append(
                "This is the first brief, so nothing can have just crossed. "
                "Everything quiet today is noted, and from tomorrow a new lapse "
                "shows up here on the morning it happens."
            )

        out.append("")
        tail = []
        if self.withheld:
            tail.append(f"{self.withheld} other quiet accounts, not shown")
        if self.never_touched:
            tail.append(f"{len(self.never_touched)} never touched")
        if self.dormant:
            tail.append(f"{self.dormant} dormant by your decision")
        if self.skipped:
            tail.append(f"{self.skipped} unconfirmed, set aside")
        tail.append(f"{self.active} still active")
        out.append(". ".join(tail) + ".")
        if self.withheld:
            out.append("Ask for the full list if you want it.")
        return "\n".join(out)


# -- the two small files ----------------------------------------------------


def read_seen(text: str) -> dict[str, date]:
    found: dict[str, date] = {}
    for raw in text.splitlines():
        match = _SEEN_LINE.match(raw)
        if not match:
            continue
        try:
            when = date.fromisoformat(match.group("date"))
        except ValueError:
            continue
        found[match.group("name")] = when
    return found


def render_seen(seen: dict[str, date]) -> str:
    lines = [f"- {when.isoformat()} | {name}" for name, when in sorted(seen.items())]
    return SEEN_HEADER + "\n".join(lines) + "\n"


def read_dormant(text: str) -> set[str]:
    """Names the operator has said to stop surfacing.

    Hand-editable, so it forgives a bullet with a trailing comment and it
    ignores the header prose. Matched case-insensitively against filenames,
    because the operator typing "gabriel ranch beef" meant the account.
    """
    names: set[str] = set()
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped.startswith("-"):
            continue
        match = _DORMANT_LINE.match(stripped)
        if not match:
            continue
        name = match.group("name").strip()
        if name:
            names.add(name.casefold())
    return names


def render_dormant(names: list[str]) -> str:
    return DORMANT_HEADER + "\n".join(f"- {name}" for name in sorted(names)) + "\n"


# -- building the brief -----------------------------------------------------


def _sort_key(scan: NoteScan, days: int, accounts: AccountsConfig):
    """Tier, then how much was ever there, then how recently it lapsed."""
    return (accounts.tier_rank(scan.tier), -scan.activity_count, days, scan.name)


def build(
    scans: list[NoteScan],
    *,
    accounts: AccountsConfig,
    brief: BriefConfig,
    as_of: date,
    seen: dict[str, date] | None = None,
    dormant: set[str] | None = None,
) -> tuple[Brief, dict[str, date]]:
    """The brief, and the seen record as it should be written back.

    Returned rather than written, so building a brief is a pure function and
    the caller decides whether this run counts. A forced check must not consume
    tomorrow's "just went quiet", the same way it must not consume the day's
    scheduled run.
    """
    seen = dict(seen or {})
    dormant = dormant or set()

    skip = {status.strip().upper() for status in accounts.skip_statuses}
    never: list[str] = []
    quiet: list[tuple[NoteScan, int]] = []
    skipped = 0
    active = 0
    asleep = 0

    for scan in scans:
        if scan.status.strip().upper() in skip:
            skipped += 1
            continue
        if scan.name.casefold() in dormant:
            asleep += 1
            continue
        if scan.last_activity is None:
            never.append(scan.name)
            continue
        days = (as_of - scan.last_activity).days
        if days > accounts.quiet_after_days:
            quiet.append((scan, days))
        else:
            active += 1
            # It has been worked since, so a future lapse is a new one.
            seen.pop(scan.name, None)

    # Nothing recorded yet means no history, not forty new lapses. They are
    # written down and reported as the ordinary backlog, because an account
    # quiet for 215 days did not just cross anything.
    first_run = not seen and bool(quiet)

    fresh: list[tuple[NoteScan, int]] = []
    for scan, days in quiet:
        if scan.name not in seen:
            seen[scan.name] = as_of
            if not first_run:
                fresh.append((scan, days))

    warm = [(scan, days) for scan, days in quiet if days <= brief.cold_after_days]
    cold = [(scan, days) for scan, days in quiet if days > brief.cold_after_days]

    def line(scan: NoteScan, days: int, bucket: str) -> Line:
        return Line(
            account=scan.name,
            bucket=bucket,
            days=days,
            activities=scan.activity_count,
            tier=scan.tier.strip(),
            opportunities=sum(1 for s in scan.opportunity_stages if accounts.is_open(s)),
        )

    # Slipping: crossed since the last brief, and still inside the save window.
    slipping_pool = sorted(
        [(s, d) for s, d in fresh if d <= brief.cold_after_days],
        key=lambda pair: _sort_key(pair[0], pair[1], accounts),
    )
    slipping = [line(s, d, SLIPPING) for s, d in slipping_pool[: brief.slipping_max]]
    shown = {item.account for item in slipping}

    deals_pool = sorted(
        [
            (s, d)
            for s, d in warm
            if s.name not in shown
            and any(accounts.is_open(stage) for stage in s.opportunity_stages)
        ],
        key=lambda pair: _sort_key(pair[0], pair[1], accounts),
    )
    deals = [line(s, d, DEAL) for s, d in deals_pool[: brief.deals_max]]
    shown |= {item.account for item in deals}

    # One line held back for the decision, so the brief never runs to its cap
    # and then silently drops the only thing that shrinks the backlog.
    room = brief.lines - (1 if brief.decision_prompt and cold else 0)
    trimmed: list[Line] = (slipping + deals)[: max(room, 0)]
    slipping = [item for item in trimmed if item.bucket == SLIPPING]
    deals = [item for item in trimmed if item.bucket == DEAL]
    shown = {item.account for item in trimmed}

    decision: Line | None = None
    if brief.decision_prompt and cold:
        # Deterministic rotation, so it works through the backlog evenly and a
        # second run on the same day asks about the same account.
        ordered = sorted(cold, key=lambda pair: (-pair[1], pair[0].name))
        scan, days = ordered[as_of.toordinal() % len(ordered)]
        decision = line(scan, days, DECISION)
        shown.add(scan.name)

    return (
        Brief(
            as_of=as_of,
            threshold_days=accounts.quiet_after_days,
            slipping=tuple(slipping),
            deals=tuple(deals),
            decision=decision,
            withheld=len(quiet) - len(shown),
            never_touched=tuple(sorted(never)),
            dormant=asleep,
            skipped=skipped,
            active=active,
            first_run=first_run,
        ),
        seen,
    )


# -- the vault side ---------------------------------------------------------


@dataclass
class BriefStore:
    """The two small files, read and written through the vault wall.

    Reading is safe from anywhere. Writing is only ever done by the morning
    surface, and never by a forced run or by the operator asking what went
    quiet: being told about a lapse on demand must not consume tomorrow's
    "just went quiet", or forcing a check to see how it looks would silently
    cost the operator the one bucket that carries news.
    """

    vault: Any
    folder: Path
    config: BriefConfig

    @property
    def seen_path(self) -> Path:
        return self.folder / self.config.seen_file

    @property
    def dormant_path(self) -> Path:
        return self.folder / self.config.dormant_file

    def _read(self, path: Path) -> str:
        try:
            return self.vault.read_text(path)
        except Exception:
            return ""

    def seen(self) -> dict[str, date]:
        return read_seen(self._read(self.seen_path))

    def dormant(self) -> set[str]:
        return read_dormant(self._read(self.dormant_path))

    def dormant_lines(self) -> list[str]:
        """As written, comments and capitalisation intact, for showing back."""
        return [
            line[1:].strip()
            for line in self._read(self.dormant_path).splitlines()
            if line.strip().startswith("-") and line[1:].strip()
        ]

    def write_seen(self, seen: dict[str, date]) -> None:
        self.vault.overwrite(self.seen_path, render_seen(seen), allow_overwrite=True)

    def sleep(self, name: str) -> bool:
        """Stop surfacing an account. False if it was already dormant."""
        names = read_dormant(self._read(self.dormant_path))
        if name.casefold() in names:
            return False
        self.vault.overwrite(
            self.dormant_path,
            render_dormant(self.dormant_lines() + [name]),
            allow_overwrite=True,
        )
        return True

    def wake(self, name: str) -> bool:
        """Bring an account back. False if it was not dormant."""
        current = self.dormant_lines()
        kept = [item for item in current if item.split("#")[0].strip().casefold() != name.casefold()]
        if len(kept) == len(current):
            return False
        self.vault.overwrite(self.dormant_path, render_dormant(kept), allow_overwrite=True)
        return True
