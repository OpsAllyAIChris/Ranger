"""Numbers in what Jarvis says, checked against numbers Python computed.

The prompt says the model does not do arithmetic. This is what makes that a
property rather than an intention.

**The failure it exists for**: a model writing "$291,546" into a summary
sentence above a table. Every figure in the table is Python's; the one in the
paragraph is not; and the paragraph is the part that gets read out loud and
repeated to a customer. It looks exactly as right as the rest.

So after a turn, every figure-shaped number in the reply is looked for in what
the tools actually returned and in what the operator themselves said. Anything
left over is reported -- in the panel, and in the audit log. It is not rewritten
and the turn is not blocked: a wrong number that has been pointed at is
recoverable, and a silent edit of what Jarvis said would be worse than the
problem.

Deliberately narrow. Only things shaped like figures count: money, thousands
separators, decimals, percentages. "Three accounts" and "the 2026 export" are
not figures and flagging them would train the operator to ignore this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: What counts as a figure worth checking. Each of these is the shape of a
#: number somebody would act on.
_FIGURE = re.compile(
    r"""
    (?<![\w.])
    (?:
        [$£€]\s?\d[\d,]*(?:\.\d+)?     # money with a symbol
      | \d{1,3}(?:,\d{3})+(?:\.\d+)?   # thousands separators
      | \d+\.\d{2}\b                   # two decimal places
      | \d+(?:\.\d+)?\s?%              # a percentage
    )
    """,
    re.VERBOSE,
)

#: Everything that is not a digit, for comparing two ways of writing one
#: number: "$48,250.00", "48250", "48,250.0" are the same figure.
_LOOSE = re.compile(r"[^\d]")


@dataclass(frozen=True)
class Unverified:
    """One figure in the reply that nothing computed."""

    text: str
    #: The normalised digits, which is what the comparison ran on.
    digits: str


def normalise(text: str) -> str:
    """Digits only, trailing zeros after a decimal point dropped.

    "$48,250.00" and "48250" are the same figure written twice, and a check
    that called them different would fire on every correct answer.
    """
    cleaned = str(text or "").strip()
    if "." in cleaned:
        whole, _, part = cleaned.rpartition(".")
        part = part.rstrip("0")
        cleaned = whole + ("." + part if part else "")
    return _LOOSE.sub("", cleaned).lstrip("0") or "0"


def figures(text: str) -> list[str]:
    """Every figure-shaped number in a piece of text."""
    return [match.group(0).strip() for match in _FIGURE.finditer(str(text or ""))]


def unverified(reply: str, sources: list[str]) -> list[Unverified]:
    """Figures in the reply that appear in none of the sources.

    `sources` is what the tools returned this turn, plus what the operator
    said. A figure the operator quoted at Jarvis is not one Jarvis invented,
    and repeating it back is not the failure this looks for.
    """
    known = set()
    for source in sources:
        for found in _FIGURE.finditer(str(source or "")):
            known.add(normalise(found.group(0)))
        # Bare integers in tool output count too: a table cell holding 48250 is
        # the same figure as "$48,250.00" in the sentence about it.
        for bare in re.findall(r"\d[\d,]*(?:\.\d+)?", str(source or "")):
            known.add(normalise(bare))

    out: list[Unverified] = []
    seen: set[str] = set()
    for found in figures(reply):
        digits = normalise(found)
        if digits in known or digits in seen:
            continue
        seen.add(digits)
        out.append(Unverified(text=found, digits=digits))
    return out


def notice(found: list[Unverified]) -> str:
    """What the operator is told. Plain, and it names the figures."""
    if not found:
        return ""
    which = ", ".join(item.text for item in found[:4])
    more = f" and {len(found) - 4} more" if len(found) > 4 else ""
    return (
        f"Jarvis said {which}{more}, which no tool computed this turn. "
        "Treat those as unchecked."
    )
