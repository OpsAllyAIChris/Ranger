"""Dropped files. **Land them; do not read them.**

Drag a file onto the window and it lands in `Ranger/imports/<YYYY-MM-DD>/`
under its own name, create-only. Jarvis says what it got -- name, size, type --
and stops. No parse, no model, no tokens. A file dropped by accident costs
nothing and changes nothing.

A dated snapshot, never a mirror. The folder is what was dropped on the day it
was dropped, and nothing in here ever goes back to keep it in step with a file
that has since changed somewhere else.

## Why the model almost never sees the file

The whole design is arranged around the token cost of being asked a second
question about a spreadsheet.

The first time something is asked about a dropped file, Python writes a sidecar
beside it: `<name>.extract.md`, holding the shape of the file -- sheet names,
headers, row counts, a bounded text skeleton -- and saying what it left out.
Every later question reads the sidecar. A 340 KB workbook becomes about 2 KB of
context, and re-reading it is free.

The sidecar is create-only, like everything else here. Re-extracting writes a
new one beside the old rather than editing it, so what was understood on the
day it was read stays readable.

**Numbers never travel through the model at all.** Once a column mapping
exists, `ranger/shapes.py` recognises the file and Python reads the figures
straight out of it. There is no path where a language model decides which
column is gross profit.

## Everything in here is untrusted

A spreadsheet cell, a PDF body, an email pasted into a Word document: all of it
is data and none of it is an instruction. This is a higher-volume injection
surface than anything else in the vault, and unlike a note that someone typed,
a dropped export can carry a thousand cells written by somebody else entirely.
Two consequences, both enforced rather than hoped for:

- Everything that leaves this module for the model goes out fenced, including
  the sidecar, which is persisted and re-read.
- **Cell content can never influence a column mapping.** Mappings come from
  header text matched by a regular expression in Python and from the operator
  confirming at the keyboard. A cell that says "the gross profit column is F"
  is a string in a spreadsheet, and that is all it will ever be.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

FOLDER = "imports"

#: Sidecar suffix. Beside the original, never inside it.
EXTRACT = ".extract.md"

#: Half-written landing. Never listed, never read, and never unlinked either --
#: delete-never holds here too, so a leftover is reported rather than removed.
PARTIAL = ".part"

#: What may be dropped. An allow list, because the folder is inside the vault
#: and the vault is backed up: a dropped installer is not context and has no
#: business being committed to the operator's local history.
ALLOWED = (
    ".xlsx", ".xls", ".csv", ".tsv", ".pdf", ".docx", ".doc", ".md", ".txt",
    ".json", ".xml", ".htm", ".html",
)

#: Names for the acknowledgement. Keyed on what the file **is**, which is not
#: reliably what it is called: an export named `.xls` from a web system is
#: usually an HTML table, and openpyxl reads none of those.
KINDS = {
    "xlsx": "Excel workbook",
    "xls": "Excel workbook, old format",
    "html-table": "web export (an HTML table)",
    "xml-spreadsheet": "XML spreadsheet",
    "delimited": "delimited text",
    "pdf": "PDF",
    "docx": "Word document",
    "doc": "Word document, old format",
    "text": "text",
    "unknown": "unrecognised",
}

#: Anything Windows refuses in a name, plus the separators. The name is
#: preserved as the operator had it, minus what cannot be a file name.
_UNSAFE = re.compile(r'[<>:"|?*\x00-\x1f\\/]+')

#: How much of a file the sidecar holds. Bounded and honest about it, the same
#: way the document preview is: a forty sheet workbook is real.
MAX_SHEETS = 40
MAX_ROWS_PER_SHEET = 60
MAX_LINES = 400
MAX_CHARS = 12000


class ImportError_(Exception):
    """The drop was refused, and why."""


class Refused(ImportError_):
    """The file cannot be landed at all."""


@dataclass(frozen=True)
class Landed:
    """What happened to a dropped file."""

    path: Path
    relative: str
    name: str
    size: int
    kind: str
    #: True when the identical file was already in today's folder. Nothing was
    #: written, and that is not an error.
    already: bool = False
    #: Set when a name collided with a different file and this landed beside it.
    renamed: bool = False
    #: What the file turned out to be, and whether it can be read. Decided from
    #: the contents before the file lands, so the drop can say so.
    format: str = ""
    tabular: bool = False
    #: What the operator is told at drop time about whether this can be read.
    note: str = ""

    def describe(self) -> str:
        size = f"{self.size / 1024:.0f} KB" if self.size >= 1024 else f"{self.size} bytes"
        if self.already:
            return f"{self.name} was already imported today, byte for byte. Nothing written."
        # The readability sentence is part of the acknowledgement, not a
        # footnote: what Jarvis will and will not be able to read is the thing
        # worth knowing at the moment a file lands.
        return f"{self.name}, {size}. {self.note or self.kind}"


def folder_for(config: Any, today: date | None = None) -> Path:
    """`Ranger/imports/2026-09-09`. Dated, so the folder is a history."""
    when = today or date.today()
    return config.vault.ranger / FOLDER / when.isoformat()


def root_for(config: Any) -> Path:
    return config.vault.ranger / FOLDER


def safe_name(name: str) -> str:
    """The operator's own file name, minus anything that is not a file name.

    The name arrives from a browser, so it is treated as a string from outside:
    separators and the characters Windows refuses are stripped, and what is
    left is a name in one folder. `..` cannot survive this, and neither can a
    drive letter.
    """
    cleaned = _UNSAFE.sub("", str(name or "")).strip().strip(".")
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        raise Refused("that file has no name Jarvis can use")
    if len(cleaned) > 120:
        stem, dot, suffix = cleaned.rpartition(".")
        cleaned = (stem[:110] + dot + suffix) if dot else cleaned[:120]
    return cleaned


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def land(
    vault: Any,
    config: Any,
    name: str,
    payload: bytes,
    *,
    today: date | None = None,
    max_bytes: int = 0,
) -> Landed:
    """Put a dropped file in today's folder. Create-only, and atomic.

    **A partial file never appears under the real name.** The bytes arrive
    complete -- a truncated upload never gets this far -- and they are written
    to a `.part` beside the target and renamed into place, so what the operator
    sees in the folder is the whole file or nothing. A crash mid-write leaves a
    `.part`, which is not listed, not read, and not deleted, because nothing
    here deletes.

    Dropping the same file twice in one day is not an error and does not make a
    second copy: identical bytes under the same name report `already` and write
    nothing. A *different* file with the same name lands beside it as
    `name (2).xlsx`, because it is a different file and losing either would be
    a delete.
    """
    from .tabular import readability

    clean = safe_name(name)
    suffix = Path(clean).suffix.lower()
    if suffix not in ALLOWED:
        raise Refused(
            f"Jarvis does not take {suffix or 'files with no extension'}. "
            f"It reads: {', '.join(ALLOWED)}"
        )
    if max_bytes and len(payload) > max_bytes:
        raise Refused(
            f"{clean} is {len(payload) / 1_048_576:.1f} MB, over the "
            f"{max_bytes / 1_048_576:.0f} MB limit for a dropped file"
        )
    if not payload:
        raise Refused(f"{clean} is empty")

    # **Decided before it lands, from the contents.** An extension is a claim
    # and this is the check. A file that lands and cannot be read is worse than
    # one that was refused: everything said about it afterwards is invention,
    # and the panel would list it as an import that can never be imported.
    can = readability(payload, clean)
    if not can.readable:
        raise Refused(f"{clean}: {can.why}")

    folder = folder_for(config, today)
    target = folder / clean
    incoming = digest(payload)

    if target.exists():
        if target.read_bytes() == payload:
            return Landed(
                path=target, relative=_relative(vault, target), name=clean,
                size=len(payload), kind=KINDS.get(can.format, can.format), already=True,
                format=can.format, tabular=can.tabular, note=can.why,
            )
        stem, counter = Path(clean).stem, 2
        while target.exists():
            target = folder / f"{stem} ({counter}){suffix}"
            if target.exists() and target.read_bytes() == payload:
                return Landed(
                    path=target, relative=_relative(vault, target), name=target.name,
                    size=len(payload), kind=KINDS.get(can.format, can.format),
                    already=True, format=can.format, tabular=can.tabular, note=can.why,
                )
            counter += 1

    folder.mkdir(parents=True, exist_ok=True)
    scratch = target.with_name(target.name + PARTIAL)
    scratch.write_bytes(payload)
    if digest(scratch.read_bytes()) != incoming:
        raise Refused(
            f"{clean} did not survive the write to disk. The partial is at "
            f"{scratch.name} and has been left there."
        )
    scratch.replace(target)
    return Landed(
        path=target, relative=_relative(vault, target), name=target.name,
        size=len(payload), kind=KINDS.get(can.format, can.format),
        renamed=target.name != clean, format=can.format, tabular=can.tabular,
        note=can.why,
    )


def _relative(vault: Any, path: Path) -> str:
    try:
        return path.relative_to(vault.config.root).as_posix()
    except Exception:
        return path.name


@dataclass(frozen=True)
class Import:
    """One dropped file, as the panel and the CLI list it."""

    path: Path
    relative: str
    name: str
    day: str
    size: int
    kind: str
    extracted: bool
    #: What it turned out to be when looked at, not what it is called.
    format: str = ""
    tabular: bool = False

    def line(self) -> str:
        return f"{self.day}  {self.name}  ({self.kind}, {max(1, self.size // 1024)} KB)"


def listing(vault: Any, config: Any) -> list[Import]:
    """Everything dropped, newest day first. Sidecars and partials excluded."""
    root = root_for(config)
    if not root.is_dir():
        return []
    found: list[Import] = []
    for day in sorted(root.iterdir(), reverse=True):
        if not day.is_dir():
            continue
        for path in sorted(day.iterdir()):
            if not path.is_file():
                continue
            if path.name.endswith(EXTRACT) or path.name.endswith(PARTIAL):
                continue
            if path.suffix.lower() not in ALLOWED:
                continue
            # The first bytes, not the name. Cheap, and it is the difference
            # between a panel row that offers an import and one that cannot.
            from .tabular import TABLE_FORMATS, sniff

            with path.open("rb") as handle:
                head = handle.read(16384)
            shape = sniff(head, path.name)
            found.append(
                Import(
                    path=path,
                    relative=_relative(vault, path),
                    name=path.name,
                    day=day.name,
                    size=path.stat().st_size,
                    kind=KINDS.get(shape, shape),
                    extracted=bool(list(day.glob(f"{path.name}*{EXTRACT}"))),
                    format=shape,
                    tabular=shape in TABLE_FORMATS,
                )
            )
    return found


def find(vault: Any, config: Any, query: str) -> tuple[Import | None, list[Import]]:
    """One dropped file by name, or the candidates when it is not one."""
    wanted = " ".join(str(query or "").split()).casefold()
    files = listing(vault, config)
    if not wanted:
        return None, files
    for item in files:
        if wanted in (item.name.casefold(), item.relative.casefold()):
            return item, []
    matches = [item for item in files if wanted in item.name.casefold()]
    if len(matches) == 1:
        return matches[0], []
    return None, matches


# -- the sidecar ------------------------------------------------------------


@dataclass
class Extract:
    """The bounded description of a file that the model actually reads."""

    path: Path
    relative: str
    text: str
    cut: list[str] = field(default_factory=list)


def extract_path(source: Path, when: datetime | None = None) -> Path:
    """`export.xlsx.extract.md`, and a stamped one if that already exists.

    Create-only, like everything else. A second extraction sits beside the
    first rather than replacing it: what was understood on the day is part of
    the record, and an extract that silently changed under a note referring to
    it would be worse than two files.
    """
    first = source.with_name(source.name + EXTRACT)
    if not first.exists():
        return first
    stamp = (when or datetime.now()).strftime("%Y-%m-%d %H%M%S")
    return source.with_name(f"{source.name} re-read {stamp}{EXTRACT}")


def existing_extract(source: Path) -> Path | None:
    """The newest sidecar for this file, or None."""
    found = sorted(
        source.parent.glob(f"{source.name}*{EXTRACT}"),
        key=lambda p: p.stat().st_mtime,
    )
    return found[-1] if found else None


def build_extract(
    source: Path,
    *,
    max_sheets: int = MAX_SHEETS,
    max_rows: int = MAX_ROWS_PER_SHEET,
    max_lines: int = MAX_LINES,
) -> Extract:
    """Read the file and write down its shape. **Python only, no model.**

    A workbook is described sheet by sheet: every sheet is *named* with its
    dimensions, always, because a fourteen sheet workbook where four sheets are
    listed is a file the operator will be wrong about. Only the first rows of
    each are quoted, and the cut is stated.
    """
    suffix = source.suffix.lower()
    cut: list[str] = []
    lines: list[str] = [
        f"# Extract of {source.name}",
        "",
        "Written by Jarvis from the file itself. This is a description of the "
        "file, not the file: it is bounded, and what was left out is named "
        "below. The original is beside it and is never changed.",
        "",
        f"- source: {source.name}",
        f"- size: {source.stat().st_size} bytes",
        f"- read: {datetime.now().isoformat(timespec='seconds')}",
        "",
    ]

    from .tabular import PDF, TABLE_FORMATS, sniff, tables as read_tables

    shape = sniff(source.read_bytes()[:16384], source.name)
    if shape in TABLE_FORMATS:
        sheets = read_tables(source)
        lines.append(f"- read as: {shape}")
        lines.append("")
        lines.append(f"## {len(sheets)} sheet(s)")
        lines.append("")
        for index, sheet in enumerate(sheets):
            lines.append(f"### {sheet.name} ({len(sheet.rows)} rows)")
            if index >= max_sheets:
                cut.append(f"the contents of sheet {sheet.name!r}")
                lines.append("")
                continue
            for row in sheet.rows[:max_rows]:
                lines.append("| " + " | ".join(row) + " |")
            if len(sheet.rows) > max_rows:
                cut.append(f"{len(sheet.rows) - max_rows} rows of {sheet.name!r}")
            lines.append("")
        if len(sheets) > max_sheets:
            cut.append(f"the contents of {len(sheets) - max_sheets} sheets")
    elif shape == PDF or suffix == ".pdf":
        lines.append("## Text")
        lines.append("")
        body, pages, whole = _pdf_text(source, max_lines)
        lines.insert(len(lines) - 2, f"- pages: {pages}")
        lines.extend(body)
        if not whole:
            cut.append("the rest of the pages")
    elif suffix == ".docx":
        from .preview import preview as render

        rendered = render(source)
        if rendered.error:
            lines.append(f"Could not be read: {rendered.error}")
        else:
            lines.append("## Text")
            lines.append("")
            body = rendered.as_text().splitlines()
            lines.extend(body[:max_lines])
            if len(body) > max_lines:
                cut.append(f"{len(body) - max_lines} of {len(body)} lines")
    else:
        body = source.read_text(encoding="utf-8", errors="replace").splitlines()
        lines.append("## Text")
        lines.append("")
        lines.extend(body[:max_lines])
        if len(body) > max_lines:
            cut.append(f"{len(body) - max_lines} of {len(body)} lines")

    text = "\n".join(lines)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
        cut.append(f"everything past {MAX_CHARS} characters")
    if cut:
        text += "\n\n## What was left out\n\n" + "\n".join(f"- {item}" for item in cut)
        text += "\n\nAsk for any of it by name and Jarvis will read the file again.\n"
    return Extract(path=source, relative=source.name, text=text + "\n", cut=cut)


def _pdf_text(source: Path, max_lines: int) -> tuple[list[str], Any, bool]:
    """A PDF's text, when there is a reader for it, and how many pages.

    Without pypdf this says how many pages there are and that it cannot read
    them, rather than describing a file it has not read. That distinction is
    the whole of this change: a landed file that nobody can read is worse than
    a refused one, and a vague answer about it is worse still.
    """
    from .preview import preview as render

    rendered = render(source)
    pages = rendered.pages or "an unknown number of"
    try:
        import pypdf
    except Exception:
        return (
            ["Jarvis has no PDF text reader installed, so this is the page count "
             "and nothing more. Install one with: pip install pypdf"],
            pages, True,
        )
    try:
        reader = pypdf.PdfReader(str(source))
        pages = len(reader.pages)
        body: list[str] = []
        for number, page in enumerate(reader.pages, start=1):
            body.append(f"[page {number}]")
            body.extend((page.extract_text() or "").splitlines())
        return body[:max_lines], pages, len(body) <= max_lines
    except Exception as exc:
        return ([f"Could not be read: {type(exc).__name__}: {exc}"], pages, True)


def extract(vault: Any, source: Path, *, when: datetime | None = None) -> Path:
    """Write the sidecar. Create-only, beside the original."""
    built = build_extract(source)
    return vault.write_new(extract_path(source, when), built.text)


def sidecar_text(vault: Any, source: Path) -> tuple[str, bool]:
    """(the sidecar's text, whether it had to be written now).

    The one entry point for "what is in that file". Reads the existing sidecar
    when there is one, which is the point of the whole arrangement: the second
    question about a workbook costs nothing.
    """
    found = existing_extract(source)
    if found is not None:
        return vault.read_text(found), False
    written = extract(vault, source)
    return vault.read_text(written), True


# -- reading tables, in Python, for numbers the model never sees ------------


#: One definition, in tabular.py, because the reader that produces these is
#: the thing that knows what a sheet is.
from .tabular import Table  # noqa: E402  (re-exported for callers)


def read_delimited(source: Path) -> list[list[str]]:
    from .tabular import tables

    found = tables(source)
    return found[0].rows if found else []


def read_workbook(source: Path) -> list[Table]:
    """Every sheet, whatever the file turns out to be. See `tabular.py`."""
    from .tabular import tables

    return tables(source)


def tables_of(source: Path) -> list[Table]:
    """The one way to get rows out of a dropped file.

    Keyed on what the file is rather than what it is called, because the export
    that started all of this was an HTML table with an `.xls` name and openpyxl
    reads none of those.
    """
    from .tabular import tables

    return tables(source)


#: Month names, both lengths, for a period column that says "Aug 2026".
_MONTHS = {
    name.casefold(): number
    for number, names in enumerate(
        [
            ("January", "Jan"), ("February", "Feb"), ("March", "Mar"),
            ("April", "Apr"), ("May", "May"), ("June", "Jun"),
            ("July", "Jul"), ("August", "Aug"), ("September", "Sep", "Sept"),
            ("October", "Oct"), ("November", "Nov"), ("December", "Dec"),
        ],
        start=1,
    )
    for name in names
}

_ISO = re.compile(r"^(\d{4})-(\d{1,2})(?:-\d{1,2})?$")
_SLASH = re.compile(r"^(\d{1,2})[/-](\d{4})$")
_DATE_SLASH = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$")
_NAMED = re.compile(r"^([A-Za-z]{3,9})[ \-/]+(\d{4})$")
_NAMED_FIRST = re.compile(r"^(\d{4})[ \-/]+([A-Za-z]{3,9})$")


def read_period(raw: Any) -> str:
    """`2026-08` from whatever an export calls August 2026, or "".

    Deliberately a fixed set of shapes rather than a date parser. A total row
    saying "Q3" or "FY2026" comes back empty and is reported as a row that was
    not read, which is how a totals line stops being imported as a month
    without anybody having to think about it.
    """
    text = " ".join(str(raw or "").split())
    if not text:
        return ""
    found = _ISO.match(text)
    if found:
        year, month = int(found.group(1)), int(found.group(2))
        return f"{year:04d}-{month:02d}" if 1 <= month <= 12 else ""
    found = _DATE_SLASH.match(text)
    if found:
        # Month first: these exports are American. A day-first file would map
        # every month to the day's number, which is why the operator confirms
        # the first import and sees the months it worked out.
        month = int(found.group(1))
        return f"{int(found.group(3)):04d}-{month:02d}" if 1 <= month <= 12 else ""
    found = _SLASH.match(text)
    if found:
        month = int(found.group(1))
        return f"{int(found.group(2)):04d}-{month:02d}" if 1 <= month <= 12 else ""
    for pattern, order in ((_NAMED, ("month", "year")), (_NAMED_FIRST, ("year", "month"))):
        found = pattern.match(text)
        if not found:
            continue
        parts = dict(zip(order, found.groups()))
        month = _MONTHS.get(str(parts["month"]).casefold())
        if month:
            return f"{int(parts['year']):04d}-{month:02d}"
    return ""


_ACCOUNTING = re.compile(r"^\((?P<inner>.+)\)$")


def read_amount(raw: Any) -> Any:
    """A `Decimal`, or None when the cell is not a figure.

    Accounting parentheses mean negative, which matters: a month of negative
    gross profit imported as positive is a number the operator would act on.
    """
    from decimal import Decimal, InvalidOperation

    text = " ".join(str(raw or "").split())
    if not text:
        return None
    negative = False
    found = _ACCOUNTING.match(text)
    if found:
        negative, text = True, found.group("inner")
    cleaned = re.sub(r"[^\d.\-]", "", text)
    if not cleaned or cleaned in ("-", ".", "-."):
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return -value if negative and value > 0 else value
