"""Reading back what was filed, looking for lines that lost their date.

The failure this exists to find was real, and it was not invention. Asked to
file "we are waiting on their reply", Jarvis filed a true fact from months
earlier -- a 3,000 MOQ counter -- in the present tense, with no date, reading as
the current state of the account.

**That is worse than a wrong fact, because it is credible and it compounds.**
The note sits below the marker, is read every day, and becomes the input to
everything concluded later. Six months on, something reasons about that
account's position from a line that was already history when it was written.

The tool that writes them is fixed. This is for the ones already on disk.

It reports and never edits. Delete-never holds here as everywhere: a correction
is a new dated entry that supersedes, written by a person who knows what was
actually true, not a rewrite of what was said.

## What it looks for, and what it deliberately does not

Two shapes, both cheap and both checkable:

- **A figure with no date in its own line.** A quantity came from somewhere,
  and somewhere had a date. This is the shape that caught the real one.
- **A present-tense claim about state with no date.** "is waiting", "are
  discussing", "wants", "owes". Present tense is fine for what was observed
  when the line was written; the entry's own date says when that was. It is not
  fine for something retrieved, and the two are indistinguishable from outside
  -- so this flags rather than concludes, and says which it cannot tell apart.

It does not try to judge whether a claim is true, or whether the operator meant
it. It finds lines a person should look at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: An entry heading below the marker: `### 2026-09-09 | note | source`.
ENTRY = re.compile(r"^###\s+(?P<when>\d{4}-\d{2}-\d{2})\s*\|\s*(?P<note>.*?)\s*\|\s*(?P<source>.*?)\s*$")

#: A dated context line, which is the shape that is *not* a problem.
DATED_CONTEXT = re.compile(r"^-\s+(?P<when>[^:]{2,40}):\s+(?P<what>.+)$")

#: Verbs that assert something about the present. Deliberately a short list of
#: the ones that carry account state, not every verb in English: a long list
#: would flag every line and be ignored inside a week.
PRESENT_CLAIMS = re.compile(
    r"\b("
    r"is|are|isn't|aren't|remains?|stands?|sits?|waits?|waiting|pending|"
    r"wants?|needs?|owes?|expects?|holds?|has|have|"
    r"currently|still|now|ongoing|in (?:their|our|play|court)"
    r")\b",
    re.I,
)

#: A date named inside the text itself, which is what makes a retrieved fact
#: safe to have written down.
INLINE_DATE = re.compile(
    r"\b("
    r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|"
    r"january|february|march|april|june|july|august|september|october|november|december|"
    r"q[1-4]|fy\d{2,4}|\d{4}"
    r")\b",
    re.I,
)


@dataclass
class Finding:
    """One line worth looking at, and why."""

    account: str
    when: str
    text: str
    reason: str
    figures: tuple[str, ...] = ()

    def line(self) -> str:
        head = f"{self.account}  {self.when}"
        return f"{head}\n    {self.text[:150]}\n    {self.reason}"


@dataclass
class Audit:
    """What a sweep found, and what it looked at."""

    findings: list[Finding] = field(default_factory=list)
    entries: int = 0
    accounts: int = 0
    unreadable: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.entries:
            return "nothing has been filed below a marker yet"
        return (
            f"{self.entries} entries across {self.accounts} accounts, "
            f"{len(self.findings)} worth looking at"
        )


def figures_in(text: str) -> list[str]:
    from .toolset import _figures_in

    return _figures_in(text)


#: The source an entry carries when nothing else was given: Jarvis's own words.
#: An entry attributed to a person -- "Chris", "call", "email", "meeting" -- is
#: something somebody observed and said, and the entry's own date covers it.
UNATTRIBUTED = "jarvis"


def check(note: str, source: str = UNATTRIBUTED) -> tuple[str, tuple[str, ...]]:
    """("" if it is fine, otherwise why), and the figures in it.

    **Attribution is what separates the two present-tense cases**, and getting
    this wrong in either direction makes the audit worthless. "Waiting on their
    reply", sourced to the operator, is a person saying what is true now and
    the entry's date covers it: flagging that is noise, and an audit that
    flags every line is one nobody reads twice. The same sentence sourced to
    Jarvis is Jarvis asserting the state of an account, which is the thing that
    was wrong.

    A figure with no date is flagged whoever said it, because a quantity came
    from somewhere and somewhere had a date.
    """
    figures = tuple(figures_in(note))
    dated = bool(INLINE_DATE.search(note))
    attributed = " ".join(str(source or "").split()).casefold() not in ("", UNATTRIBUTED)

    if figures and not dated:
        return (
            "carries a figure and no date: a quantity came from somewhere, and "
            "somewhere had a date",
            figures,
        )
    if PRESENT_CLAIMS.search(note) and not dated and not attributed:
        return (
            "Jarvis's own words, in the present tense, with no date. If it was "
            "observed when it was written the entry's date covers it; if it was "
            "retrieved, it is being read as the state of the account today",
            figures,
        )
    return "", figures


def audit_notes(vault: Any, config: Any) -> Audit:
    """Every entry below every marker, checked. **Reads only.**"""
    from .marker import MarkerError, split_bytes

    report = Audit()
    root = config.vault.accounts
    if not root.is_dir():
        return report

    for path in sorted(root.glob("*.md")):
        try:
            data = path.read_bytes()
            _, below = split_bytes(data)
        except (MarkerError, OSError) as exc:
            report.unreadable.append(f"{path.name}: {exc}")
            continue

        text = below.decode("utf-8", errors="replace")
        seen_entry = False
        for line in text.splitlines():
            stripped = line.strip()
            found = ENTRY.match(stripped)
            if not found:
                # A dated context line is the shape that is fine by
                # construction, and anything else here is the operator's own
                # prose, which is not Jarvis's to audit.
                continue
            seen_entry = True
            report.entries += 1
            reason, figures = check(found.group("note"), found.group("source"))
            if reason:
                report.findings.append(
                    Finding(
                        account=path.stem,
                        when=found.group("when"),
                        text=found.group("note"),
                        reason=reason,
                        figures=figures,
                    )
                )
        if seen_entry:
            report.accounts += 1
    return report
