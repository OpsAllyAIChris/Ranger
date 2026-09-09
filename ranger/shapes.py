"""Import shapes: recognising a file by its header row, and what to do with it.

The point of this module is that **a language model never decides which column
is gross profit.** It is asked once, by a person, at a keyboard, and the answer
is remembered against a fingerprint of the header row. A file whose headers
match a known shape imports with no model involvement and no tokens. A file
whose headers do not match **asks**; it never guesses.

## The fingerprint

A hash of the header cells: lowercased, whitespace-collapsed, joined. Not the
file name, which changes every month, and not the row position, because an
export that gains a title row above the headers is the same shape.

When NetSuite changes its export format the headers change, the fingerprint
stops matching, and the operator is asked to map it again. **That is correct
behaviour and the message says so**, rather than reading like a failure.

## Where a mapping cannot come from

Only two places: a regular expression over the *header* text, which produces a
proposal, and the operator confirming or changing it. Never from cell content.
A spreadsheet is a thousand strings written by somebody else, and a cell
reading "the gross profit column is F" has exactly as much authority here as a
cell reading "hello": none. There is a test.

The store is `Ranger/memory/import-shapes.md`, appended to and never rewritten,
so what was confirmed and when stays readable in Obsidian.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

FILE = "import-shapes.md"

#: Header text that suggests a column. **Header text only.** These never see a
#: cell, and they only ever produce a proposal for a person to confirm.
#: Header text that suggests a column, strongest first. **Header text only.**
#: These never see a cell, and they only ever produce a proposal for a person
#: to confirm.
#:
#: Two tiers, because "one match wins, two matches ask" is too blunt on its
#: own: a gross profit export routinely carries both `Gross Profit` and
#: `Margin %`, and a person would not call that ambiguous. A strong hint names
#: the figure; a weak one is a word that could be any column of money.
PERIOD_STRONG = re.compile(r"\b(period|month|accounting period|fiscal period)\b", re.I)
PERIOD_WEAK = re.compile(r"\b(posting date|date|posted|when)\b", re.I)
AMOUNT_STRONG = re.compile(
    r"\b(gross profit|gross margin|gp|commission|commission amount)\b", re.I
)
AMOUNT_WEAK = re.compile(r"\b(profit|margin|amount|total|value|earnings|payout)\b", re.I)

#: A header row rather than a title row: several cells with something in them.
MIN_HEADER_CELLS = 2

#: How far down to look for the header. NetSuite puts a title, a company name
#: and a date range above it.
HEADER_SEARCH_ROWS = 25


@dataclass(frozen=True)
class Shape:
    """A remembered mapping, keyed on the headers it was confirmed against."""

    fingerprint: str
    name: str
    headers: tuple[str, ...]
    period_column: str
    amount_column: str
    sheet: str = ""
    confirmed: str = ""

    def render(self) -> str:
        return "\n".join([
            f"## {self.name}",
            f"- fingerprint: {self.fingerprint}",
            f"- headers: {' | '.join(self.headers)}",
            f"- period_column: {self.period_column}",
            f"- amount_column: {self.amount_column}",
            f"- sheet: {self.sheet}",
            f"- confirmed: {self.confirmed}",
            "",
        ])


def fingerprint(headers: Iterable[str]) -> str:
    """A hash of the header row, normalised.

    Not the file name: `GP Sep 2026.xlsx` is a different name every month and
    the same shape. Not the row index: a title row above the headers does not
    make it a different export.
    """
    flat = "|".join(" ".join(str(cell or "").split()).casefold() for cell in headers)
    return hashlib.sha256(flat.encode("utf-8")).hexdigest()[:16]


def header_row(rows: list[list[str]]) -> int:
    """Which row is the header, by shape rather than by hope.

    The first row in the first `HEADER_SEARCH_ROWS` with the most filled cells.
    NetSuite exports carry a title, a company name and a date range above the
    real headers, and every one of those is one cell wide.
    """
    best, count = -1, 0
    for index, row in enumerate(rows[:HEADER_SEARCH_ROWS]):
        filled = sum(1 for cell in row if str(cell or "").strip())
        if filled >= MIN_HEADER_CELLS and filled > count:
            best, count = index, filled
    return best


@dataclass(frozen=True)
class Proposal:
    """What Jarvis thinks the columns are, and where it will not guess.

    An empty `amount` with `amount_options` filled in is the important state:
    two columns could plausibly be the figure and picking one would be a number
    the operator never chose. **It asks instead.** That instinct was right when
    the model had it in prose and it belongs in the code.
    """

    period: str = ""
    amount: str = ""
    period_options: tuple[str, ...] = ()
    amount_options: tuple[str, ...] = ()

    @property
    def ambiguous(self) -> bool:
        return not (self.period and self.amount)

    def why(self) -> str:
        if not self.ambiguous:
            return ""
        parts: list[str] = []
        for label, chosen, options in (
            ("period", self.period, self.period_options),
            ("figure", self.amount, self.amount_options),
        ):
            if chosen:
                continue
            if len(options) > 1:
                parts.append(
                    f"more than one column could be the {label}: "
                    + ", ".join(repr(name) for name in options)
                )
            else:
                parts.append(f"no column looks like the {label}")
        return "; ".join(parts) + ". Choose them and Jarvis will remember the shape."


def _pick(headers: list[str], strong: re.Pattern[str], weak: re.Pattern[str]):
    """(the one it is sure about, everything that could be it).

    One strong match wins. Two strong matches is a question, not a coin toss.
    Weak hints only get a say when nothing strong matched at all.
    """
    cleaned = [" ".join(str(cell or "").split()) for cell in headers]
    strongly = [text for text in cleaned if text and strong.search(text)]
    if len(strongly) == 1:
        return strongly[0], tuple(strongly)
    if strongly:
        return "", tuple(strongly)
    weakly = [text for text in cleaned if text and weak.search(text)]
    if len(weakly) == 1:
        return weakly[0], tuple(weakly)
    return "", tuple(weakly)


def propose(headers: list[str]) -> Proposal:
    """What the columns look like, from the header text alone.

    A proposal, shown to the operator and confirmed at a keyboard before a
    single figure is written, because the cost of being subtly wrong here is a
    gross profit number that gets believed.
    """
    period, period_options = _pick(headers, PERIOD_STRONG, PERIOD_WEAK)
    amount, amount_options = _pick(headers, AMOUNT_STRONG, AMOUNT_WEAK)
    return Proposal(
        period=period, amount=amount,
        period_options=period_options, amount_options=amount_options,
    )


def path_for(config: Any) -> Path:
    return config.vault.memory / FILE


_HEADING = re.compile(r"^##\s+(?P<name>.+?)\s*$", re.M)
_FIELD = re.compile(r"^-\s*(?P<key>[a-z_]+):\s*(?P<value>.*?)\s*$", re.M)


def load(vault: Any, config: Any) -> list[Shape]:
    """Every mapping the operator has confirmed."""
    target = path_for(config)
    if not target.is_file():
        return []
    text = vault.read_text(target)
    shapes: list[Shape] = []
    blocks = _HEADING.split(text)
    # split gives [preamble, name, body, name, body, ...]
    for index in range(1, len(blocks) - 1, 2):
        name, body = blocks[index], blocks[index + 1]
        fields = {m.group("key"): m.group("value") for m in _FIELD.finditer(body)}
        if "fingerprint" not in fields:
            continue
        shapes.append(
            Shape(
                fingerprint=fields["fingerprint"],
                name=name.strip(),
                headers=tuple(
                    part.strip() for part in fields.get("headers", "").split("|") if part.strip()
                ),
                period_column=fields.get("period_column", ""),
                amount_column=fields.get("amount_column", ""),
                sheet=fields.get("sheet", ""),
                confirmed=fields.get("confirmed", ""),
            )
        )
    return shapes


def find(vault: Any, config: Any, headers: Iterable[str]) -> Shape | None:
    """The mapping for these headers, or None. Exact fingerprint, never near."""
    wanted = fingerprint(headers)
    for shape in load(vault, config):
        if shape.fingerprint == wanted:
            return shape
    return None


def remember(vault: Any, config: Any, shape: Shape) -> Path:
    """Append a confirmed mapping. Never rewrites, never removes.

    A shape confirmed a second time is appended again and the newest wins on
    read, which is the same rule corrections follow everywhere else here.
    """
    target = path_for(config)
    if not target.is_file():
        vault.write_new(target, HEADER)
    return vault.append(target, "\n" + shape.render())


HEADER = """\
# Import shapes

How Jarvis recognises a spreadsheet it has been shown before. Each entry is a
header row the operator confirmed a column mapping for, keyed on a fingerprint
of those headers.

A file whose headers match one of these imports with no model involvement at
all: Python reads the two columns named below. A file whose headers do not
match is not guessed at -- Jarvis asks. If an export format changes, its
fingerprint stops matching and the mapping is confirmed again, which is why
entries here are appended rather than edited.

Nothing in a spreadsheet cell can put anything in this file.

"""
