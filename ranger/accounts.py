"""Reading account notes.

The format is the operator's, documented in docs/vault-conventions.md. Only one
thing in a note is parsed strictly: the activity heading

    ### YYYY-MM-DD | activity_type | contact_name

with the contact omitted when unknown. Everything else is read loosely, because
notes are written by a person and by an exporter and neither owes this module a
schema.

Two rules that come from the shape of the real data:
  - next_action and next_action_date are null in most activities, so nothing
    here reads them. The activity date is the only signal.
  - a note can run to 25,000 characters, so recall returns a bounded digest and
    never the whole note.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# ### 2026-09-04 | Call | Rod Illes      (contact optional, trailing pipe ok)
_ACTIVITY_HEADING = re.compile(
    r"^###[ \t]+(\d{4}-\d{2}-\d{2})[ \t]*\|([^|\n]*)(?:\|([^\n]*))?[ \t]*$",
    re.MULTILINE,
)
# - **Status:** UNCONFIRMED
_METADATA_LINE = re.compile(r"^-[ \t]+\*\*(?P<label>[^*:]+):\*\*[ \t]*(?P<value>.*?)[ \t]*$", re.MULTILINE)
_SECTION = re.compile(r"^##[ \t]+(?P<title>.+?)[ \t]*$", re.MULTILINE)
_DATE_ONLY = re.compile(r"^###[ \t]+(\d{4}-\d{2}-\d{2})[ \t]*\|", re.MULTILINE)
_STATUS_ONLY = re.compile(r"^-[ \t]+\*\*Status:\*\*[ \t]*(.*?)[ \t]*$", re.MULTILINE | re.IGNORECASE)
_TIER_ONLY = re.compile(r"^-[ \t]+\*\*Tier:\*\*[ \t]*(.*?)[ \t]*$", re.MULTILINE | re.IGNORECASE)
# The Opportunities section, up to the next ## heading.
_OPPS_SECTION = re.compile(
    r"^##[ \t]+Opportunities[ \t]*$(?P<body>.*?)(?=^##[ \t]|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
# ### <name>, then a pipe-delimited meta line on the line directly after it:
#
#     ### Wexxar Case Sealers (5 Units)
#     Proposal | 40000 | Q1 2026 | 60 probability
#
# Stage, estimated value, expected close, probability. Any of the four may be
# absent, and then the line simply has fewer elements, so nothing past the
# first can be read by position.
_OPP_HEADING = re.compile(r"^###[ \t]+(?P<name>.+?)[ \t]*$", re.MULTILINE)
_OPP_STAGE_LABEL = re.compile(
    r"^-[ \t]+\*\*(?:Stage|Status):\*\*[ \t]*(?P<value>.*?)[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)

#: The first element is the stage only when there *is* one. When it is absent
#: the value, the close or the probability slides into first place, so each has
#: to be recognisable as not-a-stage. A stage is words; these are not.
_NOT_A_STAGE = re.compile(
    r"""^(?:
        [$£€]?\s*[\d,]+(?:\.\d+)?\s*(?:[kKmM])?      # 40000, $40,000, 40k
      | (?:FY)?\s*Q[1-4](?:\s*[/-]?\s*(?:FY)?\d{2,4})?  # Q1 2026, Q1
      | (?:FY)?\s*\d{4}\s*Q[1-4]                       # 2026 Q1
      | \d{4}-\d{2}(?:-\d{2})?                         # 2026-01, 2026-01-15
      | \d+\s*(?:%|percent|probability)                 # 60 probability, 60%
      | (?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s*\d{2,4}
    )$""",
    re.IGNORECASE | re.VERBOSE,
)


def stage_from_meta(line: str) -> str:
    """The stage off an opportunity's meta line, or "" if it has none."""
    first = line.split("|")[0].strip()
    if not first or _NOT_A_STAGE.match(first):
        return ""
    return first


def _to_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class Activity:
    date: date
    kind: str
    contact: str | None
    body: str = ""

    def heading(self) -> str:
        parts = [self.date.isoformat(), self.kind]
        if self.contact:
            parts.append(self.contact)
        return " | ".join(parts)


@dataclass(frozen=True)
class AccountNote:
    name: str
    path: Path | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    sections: dict[str, str] = field(default_factory=dict)
    activities: tuple[Activity, ...] = ()
    size: int = 0

    @property
    def status(self) -> str:
        return self.metadata.get("Status", "")

    @property
    def unconfirmed(self) -> bool:
        return self.status.strip().upper() == "UNCONFIRMED"

    @property
    def last_activity(self) -> date | None:
        return self.activities[0].date if self.activities else None

    @property
    def first_activity(self) -> date | None:
        return self.activities[-1].date if self.activities else None


@dataclass(frozen=True)
class NoteScan:
    """What the quiet check needs, without slicing any bodies out."""

    name: str
    path: Path
    status: str
    last_activity: date | None
    activity_count: int
    #: The metadata Tier line, verbatim. Nothing here knows what the operator's
    #: tier vocabulary is; ranking reads it against a configured order.
    tier: str = ""
    #: One entry per `###` under `## Opportunities`, holding whatever that
    #: opportunity's Stage or Status line said, or "" if it said nothing.
    opportunity_stages: tuple[str, ...] = ()

    @property
    def unconfirmed(self) -> bool:
        return self.status.strip().upper() == "UNCONFIRMED"

    @property
    def opportunities(self) -> int:
        return len(self.opportunity_stages)


def scan_note(text: str, name: str, path: Path) -> NoteScan:
    """Cheap pass for the quiet check: status and the newest activity date.

    Deliberately does not build Activity objects. The contract says the heading
    is the only thing this check may parse, and 69 notes of up to 25k characters
    is not worth parsing twice.
    """
    status_match = _STATUS_ONLY.search(text)
    tier_match = _TIER_ONLY.search(text)
    dates = [d for d in (_to_date(m.group(1)) for m in _DATE_ONLY.finditer(text)) if d]
    return NoteScan(
        name=name,
        path=path,
        status=status_match.group(1) if status_match else "",
        # Headings are newest first by convention, but max() does not depend on it.
        last_activity=max(dates) if dates else None,
        activity_count=len(dates),
        tier=tier_match.group(1) if tier_match else "",
        opportunity_stages=scan_opportunities(text),
    )


def scan_opportunities(text: str) -> tuple[str, ...]:
    """Every opportunity in the note, and the stage it declares.

    Deliberately returns the stage verbatim rather than a judgement about it.
    Whether "Closed Won" still counts as an opportunity is the operator's
    vocabulary, and this module does not get to invent one: `ranger accounts
    survey` reports what is actually in the vault and the config decides.
    """
    section = _OPPS_SECTION.search(text)
    if not section:
        return ()

    body = section.group("body")
    starts = [m.start() for m in _OPP_HEADING.finditer(body)]
    stages: list[str] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(body)
        block = body[start:end]
        lines = block.splitlines()
        stage = ""
        # The meta line is the first non-empty line after the heading, and it
        # is not a bullet. A labelled Stage field is honoured too, so a
        # hand-written opportunity is not silently stageless.
        for line in lines[1:]:
            stripped = line.strip()
            if not stripped:
                continue
            if not stripped.startswith(("-", "#")):
                stage = stage_from_meta(stripped)
            break
        if not stage:
            labelled = _OPP_STAGE_LABEL.search(block)
            stage = labelled.group("value") if labelled else ""
        stages.append(stage)
    return tuple(stages)


def merge_notes(name: str, notes: list["AccountNote"]) -> "AccountNote":
    """Several exported notes read as the one account they are.

    The CRM exports some accounts under more than one name, so recall would
    otherwise answer from half an account's history and sound confident about
    it. `aliases.md` says which names belong together; this puts them back.

    Metadata takes the canonical note's value and fills gaps from the others,
    because the canonical export is the one the operator considers real.
    Sections are concatenated with a heading each, since a Pain points section
    in one file and another in the second are both true. Activities are merged
    and re-sorted newest first, which is the only field where getting the order
    wrong would change an answer.
    """
    if not notes:
        return AccountNote(name=name)
    if len(notes) == 1:
        return notes[0]

    ordered = sorted(notes, key=lambda n: (n.name.casefold() != name.casefold(), n.name))
    primary = ordered[0]

    metadata: dict[str, str] = {}
    for note in ordered:
        for label, value in note.metadata.items():
            if value and not metadata.get(label):
                metadata[label] = value

    sections: dict[str, str] = {}
    for title in dict.fromkeys(t for note in ordered for t in note.sections):
        parts = [
            (note.sections.get(title) or "").strip()
            for note in ordered
            if (note.sections.get(title) or "").strip()
        ]
        if len(parts) == 1:
            sections[title] = parts[0]
        else:
            sections[title] = "\n\n".join(
                f"From {note.name}:\n{(note.sections.get(title) or '').strip()}"
                for note in ordered
                if (note.sections.get(title) or "").strip()
            )

    activities = tuple(
        sorted(
            (activity for note in ordered for activity in note.activities),
            key=lambda a: a.date,
            reverse=True,
        )
    )

    return AccountNote(
        name=name,
        path=primary.path,
        metadata=metadata,
        sections=sections,
        activities=activities,
        size=sum(note.size for note in ordered),
    )


def opportunity_shapes(text: str) -> list[tuple[tuple[str, ...], tuple[tuple[str, str], ...]]]:
    """The shape of every opportunity in a note, for the survey.

    Returns the pipe-delimited meta line split into segments, and the labelled
    fields under it. Shapes rather than content: this exists to find out where
    a stage lives when `- **Stage:**` turns out not to be it, and the answer
    has to come from the real notes because the export's schema is not
    documented anywhere this repository can see.
    """
    section = _OPPS_SECTION.search(text)
    if not section:
        return []

    body = section.group("body")
    starts = [m.start() for m in _OPP_HEADING.finditer(body)]
    shapes: list[tuple[tuple[str, ...], tuple[tuple[str, str], ...]]] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(body)
        block = body[start:end]
        lines = block.splitlines()[1:]  # past the ### heading itself

        meta: tuple[str, ...] = ()
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("-") or stripped.startswith("#"):
                break
            if "|" in stripped:
                meta = tuple(part.strip() for part in stripped.split("|"))
            break

        fields = tuple(
            (m.group("label").strip(), m.group("value").strip())
            for m in _METADATA_LINE.finditer(block)
        )
        shapes.append((meta, fields))
    return shapes


def parse_note(text: str, name: str, path: Path | None = None) -> AccountNote:
    """Full parse, for recall."""
    section_starts = [(m.start(), m.group("title")) for m in _SECTION.finditer(text)]

    head = text[: section_starts[0][0]] if section_starts else text
    metadata = {m.group("label").strip(): m.group("value").strip() for m in _METADATA_LINE.finditer(head)}

    sections: dict[str, str] = {}
    for index, (start, title) in enumerate(section_starts):
        end = section_starts[index + 1][0] if index + 1 < len(section_starts) else len(text)
        body = text[start:end]
        body = body[body.index("\n") + 1 :] if "\n" in body else ""
        sections[title.strip()] = body.strip()

    return AccountNote(
        name=name,
        path=path,
        metadata=metadata,
        sections=sections,
        activities=_all_activities(text, sections),
        size=len(text),
    )


def _all_activities(text: str, sections: dict[str, str]) -> tuple[Activity, ...]:
    """The CRM's activities and Ranger's filed ones, as one timeline.

    `scan_note` already counts both, because it scans headings across the whole
    note; this is the structured half catching up, so recall shows a call filed
    yesterday next to the export's own history rather than only in a section
    nobody reads.

    Split on the **marker**, not on the section heading. The heading is
    editable prose by design -- the operator reads these in Obsidian and may
    rename `## Ranger Context` -- and parsing by heading name would work until
    the first time they did.
    """
    from .marker import MarkerError, split

    activities = list(_parse_activities(sections.get("Activity", "")))
    try:
        _, below = split(text)
    except MarkerError:
        return tuple(activities)

    filed = _parse_activities(below)
    if not filed:
        return tuple(activities)

    merged = activities + [a for a in filed if a not in activities]
    merged.sort(key=lambda a: a.date, reverse=True)
    return tuple(merged)


def _parse_activities(section: str) -> tuple[Activity, ...]:
    matches = list(_ACTIVITY_HEADING.finditer(section))
    activities: list[Activity] = []
    for index, match in enumerate(matches):
        when = _to_date(match.group(1))
        if when is None:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(section)
        contact = (match.group(3) or "").strip()
        activities.append(
            Activity(
                date=when,
                kind=match.group(2).strip(),
                contact=contact or None,
                body=section[match.end() : end].strip(),
            )
        )
    # Newest first regardless of how the file was ordered.
    activities.sort(key=lambda a: a.date, reverse=True)
    return tuple(activities)


def scan_all(
    vault: "Vault", folder: Path, exclude_files: tuple[str, ...] = ()
) -> tuple[list[NoteScan], list[str]]:
    """Every account note, cheaply scanned. Returns what failed as well."""
    excluded = {name.lower() for name in exclude_files}
    scans: list[NoteScan] = []
    errors: list[str] = []
    for item in vault.list_markdown(folder):
        if item.path.name.lower() in excluded:
            continue
        try:
            scans.append(scan_note(vault.read_text(item.path), item.path.stem, item.path))
        except Exception as exc:
            errors.append(f"{item.relative}: {exc}")
    return scans, errors


# -- resolving a spoken name ------------------------------------------------


def _normalise(text: str) -> str:
    """Fold case, accents and punctuation so 'Illes' matches 'Illes Foods'."""
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = re.sub(r"[^\w\s]", " ", folded.lower())
    return re.sub(r"\s+", " ", folded).strip()


@dataclass(frozen=True)
class Resolution:
    """Never guesses. Either one match, or the operator gets asked."""

    match: str | None = None
    candidates: tuple[str, ...] = ()
    how: str = "none"  # exact | substring | close | ambiguous | none

    @property
    def ambiguous(self) -> bool:
        return self.how == "ambiguous"

    @property
    def found(self) -> bool:
        return self.match is not None


def resolve_account(query: str, names: list[str], cutoff: float = 0.6) -> Resolution:
    """Case-insensitive substring first, then close match.

    More than one hit is always a question for the operator, never a silent
    pick of the first.
    """
    wanted = _normalise(query)
    if not wanted:
        return Resolution()

    pairs = [(name, _normalise(name)) for name in names]

    exact = [name for name, norm in pairs if norm == wanted]
    if len(exact) == 1:
        return Resolution(match=exact[0], how="exact")
    if len(exact) > 1:
        return Resolution(candidates=tuple(sorted(exact)), how="ambiguous")

    substring = [name for name, norm in pairs if wanted in norm]
    if len(substring) == 1:
        return Resolution(match=substring[0], how="substring")
    if len(substring) > 1:
        return Resolution(candidates=tuple(sorted(substring)), how="ambiguous")

    # Fuzzy matching compares the fragment against each word of the name as
    # well as the whole thing. "Northwynd" scores 0.55 against "northwind
    # provisions" and 0.89 against "northwind", and the operator said the
    # fragment, not the filename.
    if len(wanted) < 3:
        return Resolution()

    scored = [(name, _similarity(wanted, norm)) for name, norm in pairs]
    best = max((score for _, score in scored), default=0.0)
    if best < cutoff:
        return Resolution()

    # Anything within a whisker of the best is a genuine rival, not a runner up.
    matched = sorted(name for name, score in scored if score >= max(cutoff, best - 0.05))
    if len(matched) == 1:
        return Resolution(match=matched[0], how="close")
    return Resolution(candidates=tuple(matched), how="ambiguous")


def _similarity(wanted: str, name: str) -> float:
    ratio = difflib.SequenceMatcher(None, wanted, name).ratio
    best = ratio()
    for word in name.split():
        best = max(best, difflib.SequenceMatcher(None, wanted, word).ratio())
    return best


# -- the bounded digest -----------------------------------------------------

#: Sections worth surfacing, in the order they are useful when answering.
DIGEST_SECTIONS = ("Pain points", "Target solution", "Notes")


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip() + " ..."


def render_digest(
    note: AccountNote,
    *,
    activities: int = 5,
    section_chars: int = 400,
    body_chars: int = 300,
    max_chars: int = 4000,
) -> str:
    """A digest, never the note.

    Notes reach 25,000 characters. Dropping one of those into the conversation
    would blow the turn out and bury the answer, so this returns the metadata,
    a clipped slice of the prose sections, and the most recent activities with
    their bodies clipped.
    """
    lines: list[str] = [f"# {note.name}"]

    if note.metadata:
        lines.append("")
        lines.extend(f"- **{label}:** {value}" for label, value in note.metadata.items())

    total = len(note.activities)
    if total:
        first, last = note.first_activity, note.last_activity
        lines += [
            "",
            f"{total} logged activit{'y' if total == 1 else 'ies'}, "
            f"{first} to {last}." if first else "",
        ]
    else:
        lines += ["", "No activity has ever been logged against this account."]

    for title in DIGEST_SECTIONS:
        body = note.sections.get(title)
        if body:
            lines += ["", f"## {title}", _clip(body, section_chars)]

    for title in ("Contacts", "Opportunities"):
        body = note.sections.get(title)
        if body:
            lines += ["", f"## {title}", _clip(body, section_chars)]

    if note.activities:
        shown = note.activities[:activities]
        lines += ["", f"## Activity, {len(shown)} most recent of {total}"]
        for item in shown:
            lines.append(f"### {item.heading()}")
            if item.body:
                lines.append(_clip(item.body, body_chars))
        if total > len(shown):
            lines.append(
                f"\n{total - len(shown)} older activities not shown. "
                "Ask for detail on this account to see more."
            )

    text = "\n".join(line for line in lines if line is not None)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0] + "\n\n[digest truncated]"
    return text


# -- what went quiet --------------------------------------------------------


@dataclass(frozen=True)
class QuietAccount:
    name: str
    last_activity: date
    days: int


@dataclass(frozen=True)
class QuietReport:
    threshold_days: int
    as_of: date
    lapsed: tuple[QuietAccount, ...] = ()
    never_touched: tuple[str, ...] = ()
    active: int = 0
    skipped_unconfirmed: tuple[str, ...] = ()

    @property
    def considered(self) -> int:
        return len(self.lapsed) + len(self.never_touched) + self.active


def quiet_report(
    scans: list[NoteScan],
    *,
    threshold_days: int,
    as_of: date,
    skip_statuses: tuple[str, ...] = ("UNCONFIRMED",),
) -> QuietReport:
    """Split accounts into lapsed, never touched, and fine.

    Three things the real data forces:
      - UNCONFIRMED notes are set aside. They came from the activity log rather
        than the accounts export and several are near-duplicates.
      - accounts with no activity at all are their own group. Mixed in with
        genuinely lapsed accounts they would sort to the top and drown them.
      - only the activity date counts. next_action_date is null in 94% of rows.
    """
    lapsed: list[QuietAccount] = []
    never: list[str] = []
    skipped: list[str] = []
    active = 0

    skip = {status.strip().upper() for status in skip_statuses}
    for scan in scans:
        if scan.status.strip().upper() in skip:
            skipped.append(scan.name)
            continue
        if scan.last_activity is None:
            never.append(scan.name)
            continue
        days = (as_of - scan.last_activity).days
        if days > threshold_days:
            lapsed.append(QuietAccount(scan.name, scan.last_activity, days))
        else:
            active += 1

    lapsed.sort(key=lambda a: (-a.days, a.name))
    return QuietReport(
        threshold_days=threshold_days,
        as_of=as_of,
        lapsed=tuple(lapsed),
        never_touched=tuple(sorted(never)),
        active=active,
        skipped_unconfirmed=tuple(sorted(skipped)),
    )
