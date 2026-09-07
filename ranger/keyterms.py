"""Deriving the vocabulary hint list from the vault.

A hand-maintained list goes stale the moment an account is added. The account
filenames are the vocabulary that actually matters, so the list is built from
them.

Two things make this more than a list comprehension:

  - There are more distinctive words in 69 account names than Deepgram will
    take, so the list has to be ranked and cut. It is ranked by how likely the
    operator is to say the name soon, which is recency of activity first and
    volume of activity second.
  - Most tokens in a company name are worthless as hints. "Packaging" appears
    in a dozen names and a general model spells it correctly anyway. Slots go
    to the words that are actually unusual.

Rarity is measured against the operator's own vault rather than a fixed list,
so it stays right as the accounts change.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date

from .accounts import NoteScan

_TOKEN = re.compile(r"[A-Za-z][A-Za-z'&-]*")

#: Generic enough to be spelled right without help, however rare in one vault.
STOPWORDS = frozenset(
    """
    inc llc ltd co corp corporation company group holdings partners brands
    international national american usa the and of for
    packaging package packages paper plastics films film containers container
    foods food beverage dairy farms produce meats
    supply supplies distribution distributors logistics transport freight
    manufacturing industries industrial products solutions systems services
    technologies technology enterprises ventures capital
    north south east west northern southern eastern western central
    """.split()
)

#: Below this a token is too short to be worth a slot and too likely to collide.
MIN_TERM_LENGTH = 4


@dataclass(frozen=True)
class Candidate:
    term: str
    score: float
    source: str
    reason: str


@dataclass(frozen=True)
class KeytermPlan:
    terms: tuple[str, ...] = ()
    kept: tuple[Candidate, ...] = ()
    cut: tuple[Candidate, ...] = ()
    accounts_considered: int = 0
    skipped_unconfirmed: int = 0
    cap: int = 0

    @property
    def over_cap(self) -> bool:
        return bool(self.cut)


def tokens_of(name: str) -> list[str]:
    return _TOKEN.findall(name)


def distinctive_tokens(names: list[str]) -> dict[str, list[str]]:
    """Map each account name to the tokens in it worth spending a hint on.

    A token earns a slot when it is long enough, not a generic business word,
    and appears in only one account name. Document frequency does the heavy
    lifting: "Packaging" in twelve names disqualifies itself without anyone
    maintaining a list.
    """
    frequency: dict[str, int] = {}
    per_name: dict[str, list[str]] = {}
    for name in names:
        seen = {t.lower() for t in tokens_of(name)}
        per_name[name] = sorted(seen)
        for token in seen:
            frequency[token] = frequency.get(token, 0) + 1

    chosen: dict[str, list[str]] = {}
    for name in names:
        original = {t.lower(): t for t in tokens_of(name)}
        keep = [
            original[token]
            for token in per_name[name]
            if len(token) >= MIN_TERM_LENGTH
            and token not in STOPWORDS
            and frequency.get(token, 0) == 1
        ]
        # A name made entirely of common words still deserves one hint, so fall
        # back to the whole thing rather than sending nothing for it.
        chosen[name] = keep or [name]
    return chosen


def score_account(scan: NoteScan, as_of: date) -> tuple[float, str]:
    """How likely is the operator to say this name soon.

    Recency dominates. An account touched last week is one being worked; an
    account last touched in January is one they will mention rarely, if at all.
    """
    if scan.last_activity is None:
        return 0.05, "no activity ever logged"

    days = max(0, (as_of - scan.last_activity).days)
    recency = 1.0 / (1.0 + days / 60.0)
    volume = min(1.0, math.log10(1 + scan.activity_count) / 2.0)
    return 2.0 * recency + volume, f"last activity {days}d ago, {scan.activity_count} logged"


def derive_keyterms(
    scans: list[NoteScan],
    config_terms: tuple[str, ...] = (),
    *,
    cap: int,
    as_of: date,
    skip_statuses: tuple[str, ...] = ("UNCONFIRMED",),
) -> KeytermPlan:
    """Config terms first, then account names ranked by recency, then cut."""
    skip = {s.strip().upper() for s in skip_statuses}
    usable = [s for s in scans if s.status.strip().upper() not in skip]
    skipped = len(scans) - len(usable)

    kept: list[Candidate] = []
    seen: set[str] = set()

    # The operator's own industry list is small, always relevant, and never
    # competes with an account for a slot.
    for term in config_terms[:cap]:
        if term.lower() not in seen:
            seen.add(term.lower())
            kept.append(Candidate(term, math.inf, "ranger.toml", "industry vocabulary"))

    chosen = distinctive_tokens([s.name for s in usable])
    ranked: list[Candidate] = []
    for scan in usable:
        score, reason = score_account(scan, as_of)
        for term in chosen[scan.name]:
            if term.lower() in seen:
                continue
            seen.add(term.lower())
            ranked.append(Candidate(term, score, scan.name, reason))

    ranked.sort(key=lambda c: (-c.score, c.term.lower()))

    room = max(0, cap - len(kept))
    kept.extend(ranked[:room])
    cut = tuple(ranked[room:])

    return KeytermPlan(
        terms=tuple(c.term for c in kept),
        kept=tuple(kept),
        cut=cut,
        accounts_considered=len(usable),
        skipped_unconfirmed=skipped,
        cap=cap,
    )
