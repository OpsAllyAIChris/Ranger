"""Accounts the CRM believes are two things and the operator knows are one.

BWI Companies and BWI Company. TST Impreso and TST Impresso. Vytalogy and
Vitalogy. Six pairs, each splitting one account's history in half, which is
degrading answers today: ask about one and you get half the activity, and the
morning brief counts each half as a separate thin relationship.

**The vault stays a faithful copy of the CRM.** Merging the notes is not a fix,
because `build_vault.py` regenerates them from the export and both halves come
back on the next refresh. So the map lives in Ranger's own folder and is
applied at read time, which survives every regeneration and needs nothing from
the CRM.

It is applied in three places, and the second is the one that was easy to miss:

  - **Resolving a name.** Asking about "BWI Company" finds the canonical note.
  - **Scanning.** The quiet check and the morning brief see one account with
    the combined history, not two with half each. This changes the numbers the
    brief ranks on: an alias that merges two thin records into one substantial
    one can move an account up the list. That is correct, and it is worth
    knowing before it looks like a bug.
  - **Recall.** Reading an account reads every note that belongs to it.

**Ranger proposes and the operator approves. There is no merge tool.** A wrong
merge is wrong in both accounts forever and shows up as an error in neither, so
nothing here can be reached by the model: detection produces a suggestion, and
only a person editing this file or running `ranger alias` changes anything.

**A stale alias is loud.** If a canonical name stops existing after a CRM
refresh, that is said out loud rather than silently ignored, because the
symptom otherwise is an account quietly splitting back into two.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

#: `- BWI Company -> BWI Companies`, with anything after a # kept as a note.
_LINE = re.compile(
    r"^-\s*(?P<variant>[^#>]+?)\s*(?:->|→)\s*(?P<canonical>[^#]+?)\s*(?:#.*)?$"
)

HEADER = """\
# Aliases

Accounts the CRM exports under more than one name. The left name is folded into
the right one everywhere Ranger reads: resolving a spoken name, the quiet check
and the morning brief, and account recall.

    - BWI Company -> BWI Companies

This lives here rather than in the account notes because build_vault.py
regenerates those from the CRM and both halves would come back. The vault stays
a faithful copy of the export and Ranger stops treating one account as two.

Folding two accounts together adds their activity counts, so an account can
move up the morning brief once it stops being two thin records. That is the
point of it.

Ranger can suggest pairs with 'ranger alias suggest'. It never merges anything
on its own: a wrong merge is wrong in both accounts forever and shows up as an
error in neither.

"""


@dataclass(frozen=True)
class Alias:
    variant: str
    canonical: str
    note: str = ""


@dataclass(frozen=True)
class Aliases:
    """The map, and whatever was wrong with it."""

    pairs: tuple[Alias, ...] = ()
    #: Aliases whose canonical name is not an account any more. Said out loud
    #: rather than dropped: the symptom otherwise is a silent un-merge.
    stale: tuple[str, ...] = ()

    def canonical_for(self, name: str) -> str:
        wanted = name.strip().casefold()
        for alias in self.pairs:
            if alias.variant.casefold() == wanted:
                return alias.canonical
        return name

    def variants_of(self, canonical: str) -> tuple[str, ...]:
        wanted = canonical.strip().casefold()
        return tuple(
            alias.variant for alias in self.pairs if alias.canonical.casefold() == wanted
        )

    @property
    def empty(self) -> bool:
        return not self.pairs


def parse(text: str, known: Iterable[str] | None = None) -> Aliases:
    """Read the file. Hand-editable, so it forgives a lot and explains itself."""
    names = {name.casefold() for name in (known or ())}
    pairs: list[Alias] = []
    stale: list[str] = []

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped.startswith("-"):
            continue
        match = _LINE.match(stripped)
        if not match:
            continue
        variant = match.group("variant").strip()
        canonical = match.group("canonical").strip()
        if not variant or not canonical or variant.casefold() == canonical.casefold():
            continue
        if names and canonical.casefold() not in names:
            stale.append(f"{variant} -> {canonical}, but {canonical!r} is not an account")
            continue
        pairs.append(Alias(variant=variant, canonical=canonical))

    return Aliases(pairs=tuple(pairs), stale=tuple(stale))


def render(pairs: Iterable[Alias]) -> str:
    lines = sorted(f"- {a.variant} -> {a.canonical}" for a in pairs)
    return HEADER + "\n".join(lines) + "\n"


# -- folding the scans together --------------------------------------------


def fold(scans: list[Any], aliases: Aliases) -> list[Any]:
    """One NoteScan per real account, with the halves added together.

    Activity counts are summed and the newest activity wins, which is what
    makes the quiet check right: an account worked under one name last week is
    not quiet because the other name has been silent for a year.
    """
    from dataclasses import replace

    if aliases.empty:
        return scans

    by_name = {scan.name.casefold(): scan for scan in scans}
    merged: dict[str, Any] = {}
    order: list[str] = []

    for scan in scans:
        canonical = aliases.canonical_for(scan.name)
        if canonical.casefold() not in by_name:
            # The canonical note is gone. Keep the variant as itself rather
            # than folding it into nothing.
            canonical = scan.name

        key = canonical.casefold()
        if key not in merged:
            # Start from the canonical note if it is this one, so its status
            # and tier win over a variant's.
            merged[key] = replace(by_name.get(key, scan), name=canonical)
            order.append(key)
            if by_name.get(key) is scan:
                continue
            if key == scan.name.casefold():
                continue

        current = merged[key]
        if current is scan:
            continue

        dates = [d for d in (current.last_activity, scan.last_activity) if d]
        merged[key] = replace(
            current,
            last_activity=max(dates) if dates else None,
            activity_count=current.activity_count + scan.activity_count,
            opportunity_stages=current.opportunity_stages + scan.opportunity_stages,
            tier=current.tier or scan.tier,
        )

    return [merged[key] for key in order]


# -- suggesting pairs, never making them -----------------------------------


@dataclass(frozen=True)
class Suggestion:
    """A pair that looks like one account. For a person to judge."""

    left: str
    right: str
    score: float
    why: str

    def describe(self) -> str:
        return f"{self.left}  /  {self.right}   ({self.why}, {self.score:.2f})"


#: Words that carry no identity, so two names differing only in these are the
#: same company written twice. Not a general stopword list: these are the
#: company suffixes that actually appear in the operator's export.
NOISE = frozenset(
    """
    inc inc. llc llc. ltd ltd. co co. corp corp. company companies
    incorporated limited group holdings the and of
    """.split()
)


def _shape(name: str) -> str:
    words = [w for w in re.split(r"[^a-z0-9]+", name.casefold()) if w and w not in NOISE]
    return " ".join(words)


def suggest(names: Iterable[str], *, cutoff: float = 0.82) -> list[Suggestion]:
    """Pairs of account names that look like one account written twice.

    Three signals, all deliberately blunt. Names identical once company
    suffixes are removed, which catches BWI Company against BWI Companies. The
    same words in a different order, which catches Gabriel Ranch Beef against
    Gabriel Beef Ranch and which string similarity misses badly, because
    reordered words score low as strings. And names one or two characters
    apart, which catches Impreso against Impresso and Vytalogy against
    Vitalogy.

    It errs toward suggesting too much. A suggestion costs the operator a
    glance; a missed pair costs an account half its history.
    """
    unique = sorted(set(names))
    found: list[Suggestion] = []

    for index, left in enumerate(unique):
        for right in unique[index + 1 :]:
            left_shape, right_shape = _shape(left), _shape(right)
            if not left_shape or not right_shape:
                continue

            if left_shape == right_shape:
                found.append(Suggestion(left, right, 1.0, "the same once Inc and Co are removed"))
                continue

            if sorted(left_shape.split()) == sorted(right_shape.split()):
                found.append(Suggestion(left, right, 0.99, "the same words in a different order"))
                continue

            score = SequenceMatcher(None, left_shape, right_shape).ratio()
            if score >= cutoff:
                found.append(Suggestion(left, right, score, "nearly the same spelling"))

    found.sort(key=lambda s: (-s.score, s.left))
    return found


# -- the file ---------------------------------------------------------------


@dataclass
class AliasFile:
    """Read and written through the vault wall, like everything else."""

    vault: Any
    folder: Path
    filename: str = "aliases.md"

    @property
    def path(self) -> Path:
        return self.folder / self.filename

    def _read(self) -> str:
        try:
            return self.vault.read_text(self.path)
        except Exception:
            return ""

    def load(self, known: Iterable[str] | None = None) -> Aliases:
        return parse(self._read(), known)

    def add(self, variant: str, canonical: str) -> bool:
        """False if that variant is already mapped somewhere."""
        current = parse(self._read())
        if any(a.variant.casefold() == variant.strip().casefold() for a in current.pairs):
            return False
        pairs = [*current.pairs, Alias(variant.strip(), canonical.strip())]
        self.vault.overwrite(self.path, render(pairs), allow_overwrite=True)
        return True

    def remove(self, variant: str) -> bool:
        current = parse(self._read())
        kept = [a for a in current.pairs if a.variant.casefold() != variant.strip().casefold()]
        if len(kept) == len(current.pairs):
            return False
        self.vault.overwrite(self.path, render(kept), allow_overwrite=True)
        return True
