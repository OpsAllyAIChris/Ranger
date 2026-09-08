"""Item D. Generating .docx, .xlsx and .pdf into the drafts folder.

**A generated document is a draft that happens not to be text.** It lands in
`Ranger/drafts/` beside the markdown drafts, it lists with them, it clears with
them, and like them it is never sent and never overwritten. Regenerating
produces a new file beside the old one, because delete-never holds here as
everywhere else in this repository.

Ungated, for the same reason drafting is ungated: nothing leaves the machine.
The preview is the review.

One content model, three writers. A `Spec` is a title and a list of blocks --
headings, paragraphs, bullets, tables -- and each writer renders those blocks
in its own idiom. Tables become sheets in a workbook and stay tables in the
other two. Having one model is what makes `ranger gp export` and a model-written
proposal go through the same code, and it is why there is one place to fix a
rendering bug rather than three.

## The libraries are not the validation

The consumers are Word, Excel and a PDF reader, and none of them can be run
here. `python-docx` reading back a file `python-docx` wrote proves that
python-docx is self-consistent, which was never in doubt. So the tests unzip
the OOXML and assert on the parts, and `ranger/preview.py` -- which parses the
same files with nothing but `zipfile` and `ElementTree` -- is the independent
implementation the round trip goes through. **The operator opening it in the
real application is the only green light, once per template.**

## Untrusted content, and three specific holes

Text in a generated document routinely comes from an account note or a pasted
customer email. Three things about that are handled here rather than hoped
about, and each has a test:

- **Excel formula injection.** A cell whose value starts with `=`, `+`, `-` or
  `@` is a formula to Excel, and openpyxl will happily write one. A note
  containing `=HYPERLINK(...)` would become a live formula in a workbook the
  operator sends to a customer. Every cell here is written as text, explicitly,
  with its data type forced.
- **reportlab markup.** `Paragraph` accepts a small HTML-like markup including
  `<img src="...">`, which reads a file off disk at render time. Every string
  is escaped before it reaches reportlab.
- **Word field codes.** Nothing here writes `w:fldChar` or `w:instrText`, and
  a test asserts the generated XML has none, so pasted text cannot arrive as a
  DDE or link field.

Instruction-shaped language is scanned for and reported to the operator at
generation time. It is not edited out: this module never silently changes what
it was asked to write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .drafts import slugify

#: What can be generated. The extension is the name, because that is what the
#: operator says: "make it a docx".
KINDS = ("docx", "xlsx", "pdf")

#: Which import each kind needs, and what to install when it is missing. Named
#: here so the failure is a sentence rather than an ImportError.
REQUIRES: dict[str, tuple[str, str]] = {
    "docx": ("docx", "python-docx"),
    "xlsx": ("openpyxl", "openpyxl"),
    "pdf": ("reportlab", "reportlab"),
}

#: Excel reads a leading one of these as the start of a formula. Values that
#: begin with one are still written verbatim -- they are just written as text.
FORMULA_LEADS = ("=", "+", "-", "@")

#: A hard ceiling on a single table, so a runaway loop cannot try to write a
#: million rows into a document nobody asked for.
MAX_ROWS = 5000


class DocumentError(Exception):
    """The document could not be written, and why."""


class MissingLibrary(DocumentError):
    """The writer for this format is not installed."""


@dataclass(frozen=True)
class Block:
    """One piece of a document, in the idiom of none of the three formats."""

    kind: str  # heading | text | bullet | table
    text: str = ""
    level: int = 1
    header: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()
    #: Sheet name when this table becomes a worksheet. Ignored elsewhere.
    name: str = ""


@dataclass(frozen=True)
class Spec:
    """What to write, independent of what it will be written as."""

    title: str
    blocks: tuple[Block, ...] = ()
    subtitle: str = ""
    #: Where the content came from, printed in the document itself. A document
    #: that cannot say where its numbers came from is a document nobody can
    #: check.
    source: str = ""

    def tables(self) -> list[Block]:
        return [b for b in self.blocks if b.kind == "table"]


@dataclass(frozen=True)
class Generated:
    """What landed on disk."""

    path: Path
    relative: str
    kind: str
    title: str
    size: int
    #: Instruction-shaped passages found in the content, for the operator.
    findings: tuple[str, ...] = ()
    #: Cells that Excel would have read as a formula and that were written as
    #: text instead. Counted and reported rather than silently handled: the
    #: operator should know a customer's spreadsheet had `=` in it.
    formulas_as_text: int = 0


def heading(text: str, level: int = 1) -> Block:
    return Block(kind="heading", text=text, level=level)


def text(body: str) -> Block:
    return Block(kind="text", text=body)


def bullet(body: str) -> Block:
    return Block(kind="bullet", text=body)


def table(header: Iterable[Any], rows: Iterable[Iterable[Any]], name: str = "") -> Block:
    cleaned = tuple(tuple(_cell(value) for value in row) for row in rows)
    if len(cleaned) > MAX_ROWS:
        raise DocumentError(
            f"a table of {len(cleaned)} rows is past the {MAX_ROWS} row ceiling"
        )
    return Block(
        kind="table",
        header=tuple(_cell(value) for value in header),
        rows=cleaned,
        name=name,
    )


def _cell(value: Any) -> str:
    """Every cell is a string. Excel decides types from what it is given, and
    what it is given here is text, always."""
    if value is None:
        return ""
    return str(value)


def available(kind: str) -> bool:
    """Whether this format can be written on this machine right now."""
    module, _ = REQUIRES.get(kind, ("", ""))
    if not module:
        return False
    try:
        __import__(module)
    except Exception:
        return False
    return True


def missing() -> list[str]:
    return [kind for kind in KINDS if not available(kind)]


def _require(kind: str):
    module, package = REQUIRES[kind]
    try:
        return __import__(module)
    except Exception as exc:
        raise MissingLibrary(
            f"writing a .{kind} needs {package}, which is not installed. "
            f"Install it with: pip install {package}"
        ) from exc


def formula_shaped(spec: Spec) -> int:
    """Cells Excel would evaluate if they were not forced to text."""
    return sum(
        1
        for block in spec.blocks
        if block.kind == "table"
        for row in (block.header,) + block.rows
        for value in row
        if value.startswith(FORMULA_LEADS)
    )


def scan_spec(spec: Spec) -> list[str]:
    """Instruction-shaped language anywhere in the content.

    Reported, never removed. This module does not edit what it was asked to
    write; it tells the operator what is in it.
    """
    from .untrusted import scan

    body = "\n".join(
        [spec.title, spec.subtitle]
        + [block.text for block in spec.blocks]
        + [
            " ".join(row)
            for block in spec.blocks
            for row in (block.header,) + block.rows
        ]
    )
    return [f"{finding.label}: {finding.excerpt}" for finding in scan(body)]


def filename(title: str, kind: str, today: date, folder: Path) -> Path:
    """`2026-09-08-q4-proposal.docx`, never landing on an existing file.

    A regenerated document is a new file. Nothing here overwrites, so the
    version that was sent to a customer is still the version on disk.
    """
    if kind not in KINDS:
        raise DocumentError(f"{kind!r} is not one of: {', '.join(KINDS)}")
    stem = f"{today.isoformat()}-{slugify(title)}"
    target = folder / f"{stem}.{kind}"
    counter = 2
    while target.exists():
        target = folder / f"{stem}-{counter}.{kind}"
        counter += 1
    return target


def generate(
    vault: Any,
    folder: Path,
    spec: Spec,
    kind: str,
    *,
    today: date | None = None,
    page_size: str = "A4",
) -> Generated:
    """Write the document. The one way in, for every caller.

    The file is composed in memory and then written through the vault, so a
    writer that raises half way leaves nothing behind: there is no partially
    written document to mistake for a finished one.
    """
    kind = str(kind or "").strip().lower().lstrip(".")
    if kind not in KINDS:
        raise DocumentError(f"{kind!r} is not one of: {', '.join(KINDS)}")
    if not spec.title.strip():
        raise DocumentError("a document needs a title; it becomes the file name")

    writer = {"docx": _docx_bytes, "xlsx": _xlsx_bytes, "pdf": _pdf_bytes}[kind]
    payload = writer(spec, page_size=page_size)

    target = filename(spec.title, kind, today or date.today(), folder)
    written = vault.write_new_bytes(target, payload)
    return Generated(
        path=written,
        relative=written.relative_to(vault.config.root).as_posix(),
        kind=kind,
        title=spec.title,
        size=len(payload),
        findings=tuple(scan_spec(spec)),
        formulas_as_text=formula_shaped(spec),
    )


# -- the three writers ------------------------------------------------------


def _docx_bytes(spec: Spec, *, page_size: str = "A4") -> bytes:
    """Word. Headings, paragraphs, bullets and grid tables, and nothing clever.

    Deliberately plain. Every feature used here is one the preview parser can
    read back, so what the operator sees before opening Word is built from the
    same parts Word will read.
    """
    import io

    _require("docx")
    from docx import Document as WordDocument
    from docx.shared import Pt

    document = WordDocument()
    if page_size.upper() == "A4":
        from docx.shared import Mm

        for section in document.sections:
            section.page_width = Mm(210)
            section.page_height = Mm(297)

    document.add_heading(spec.title, level=0)
    if spec.subtitle:
        para = document.add_paragraph(spec.subtitle)
        para.runs[0].font.size = Pt(12)

    for block in spec.blocks:
        if block.kind == "heading":
            document.add_heading(block.text, level=max(1, min(4, block.level)))
        elif block.kind == "bullet":
            document.add_paragraph(block.text, style="List Bullet")
        elif block.kind == "table":
            # A table with no header is a legitimate shape: a two column list of
            # label and value has nothing to put in a header row, and an empty
            # one would print as a blank band across the top.
            columns = len(block.header) or (len(block.rows[0]) if block.rows else 1)
            grid = document.add_table(rows=1 if block.header else 0, cols=max(1, columns))
            grid.style = "Table Grid"
            for index, name in enumerate(block.header):
                cell = grid.rows[0].cells[index]
                cell.text = name
                for run in cell.paragraphs[0].runs:
                    run.bold = True
            for row in block.rows:
                cells = grid.add_row().cells
                for index, value in enumerate(row[: len(cells)]):
                    cells[index].text = value
        else:
            document.add_paragraph(block.text)

    if spec.source:
        document.add_paragraph("")
        note = document.add_paragraph(spec.source)
        note.runs[0].font.size = Pt(8)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _xlsx_bytes(spec: Spec, *, page_size: str = "A4") -> bytes:
    """Excel. **Values only, and every cell is text.**

    Two rules, and the second is a security control rather than a preference.

    Values only, because a formula is invisible in the preview and the preview
    is the review. If a total is wanted, it is computed in Python and written
    as a number.

    Every cell is written with its data type forced to string, because openpyxl
    turns any value starting with `=`, `+`, `-` or `@` into a live formula.
    Content in these documents comes from account notes and pasted email; a
    workbook that evaluates what a customer wrote is not something to send back
    to that customer.
    """
    import io

    _require("xlsx")
    from openpyxl import Workbook
    from openpyxl.styles import Font

    workbook = Workbook()
    sheet = workbook.active
    prose = [b for b in spec.blocks if b.kind != "table"]
    tables = spec.tables()

    def put(target, row: int, column: int, value: str, *, bold: bool = False) -> None:
        cell = target.cell(row=row, column=column)
        cell.value = value
        # The whole point. Assigning "=SUM(A1)" makes openpyxl write a formula;
        # forcing the type writes the characters instead. Excel shows the text
        # and evaluates nothing.
        cell.data_type = "s"
        if bold:
            cell.font = Font(bold=True)

    sheet.title = (_sheet_name(spec.title) or "Document")[:31]
    line = 1
    put(sheet, line, 1, spec.title, bold=True)
    line += 1
    if spec.subtitle:
        put(sheet, line, 1, spec.subtitle)
        line += 1
    for block in prose:
        line += 1
        prefix = "- " if block.kind == "bullet" else ""
        put(sheet, line, 1, f"{prefix}{block.text}", bold=block.kind == "heading")
    if spec.source:
        line += 2
        put(sheet, line, 1, spec.source)

    for index, block in enumerate(tables, start=1):
        name = _sheet_name(block.name or block.text or f"Table {index}")
        target = workbook.create_sheet(title=name[:31] or f"Table {index}")
        for column, value in enumerate(block.header, start=1):
            put(target, 1, column, value, bold=True)
        for offset, row in enumerate(block.rows, start=2 if block.header else 1):
            for column, value in enumerate(row, start=1):
                put(target, offset, column, value)
        widths: dict[int, int] = {}
        for row in (block.header,) + block.rows:
            for column, value in enumerate(row, start=1):
                widths[column] = max(widths.get(column, 10), min(60, len(value) + 2))
        for column, width in widths.items():
            target.column_dimensions[chr(64 + column) if column <= 26 else "A"].width = width

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


_SHEET_BAD = re.compile(r"[\\/*?:\[\]]")


def _sheet_name(text: str) -> str:
    """Excel refuses \\ / * ? : [ ] in a sheet name, and refuses over 31 chars."""
    return _SHEET_BAD.sub(" ", " ".join(str(text).split())).strip()[:31]


def _pdf_bytes(spec: Spec, *, page_size: str = "A4") -> bytes:
    """A PDF, through reportlab's platypus flowables.

    **Every string is escaped on the way in.** `Paragraph` parses a small
    HTML-like markup, and that markup includes `<img src="...">`, which reads a
    file from disk when the page renders. Text in these documents comes from
    customer email. Escaping is not tidiness here, it is the control.
    """
    import io
    from xml.sax.saxutils import escape

    _require("pdf")
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        ListFlowable,
        ListItem,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=7,
                           textColor=colors.HexColor("#666666"))
    cellstyle = ParagraphStyle("cell", parent=styles["Normal"], fontSize=9, leading=11)

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4 if page_size.upper() == "A4" else LETTER,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title=spec.title,
    )

    flow: list[Any] = [Paragraph(escape(spec.title), styles["Title"])]
    if spec.subtitle:
        flow.append(Paragraph(escape(spec.subtitle), styles["Italic"]))
    flow.append(Spacer(1, 6))

    for block in spec.blocks:
        if block.kind == "heading":
            level = max(1, min(4, block.level))
            flow.append(Paragraph(escape(block.text), styles[f"Heading{level}"]))
        elif block.kind == "bullet":
            flow.append(
                ListFlowable(
                    [ListItem(Paragraph(escape(block.text), styles["Normal"]))],
                    bulletType="bullet", start="circle", leftIndent=14,
                )
            )
        elif block.kind == "table":
            data = [[Paragraph(escape(name), cellstyle) for name in block.header]] \
                if block.header else []
            data += [[Paragraph(escape(value), cellstyle) for value in row]
                     for row in block.rows]
            if not data or not data[0]:
                continue
            grid = Table(data, repeatRows=1 if block.header else 0, hAlign="LEFT")
            grid.setStyle(
                TableStyle([
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B0B0B0")),
                    ("BACKGROUND", (0, 0), (-1, 0 if block.header else -1),
                     colors.HexColor("#EEEEEE") if block.header else colors.white),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ])
            )
            flow.append(grid)
            flow.append(Spacer(1, 8))
        else:
            flow.append(Paragraph(escape(block.text), styles["BodyText"]))

    if spec.source:
        flow.append(Spacer(1, 14))
        flow.append(Paragraph(escape(spec.source), small))

    document.build(flow)
    return buffer.getvalue()


def provenance(what: str, when: datetime | None = None) -> str:
    """The line printed at the foot of every generated document."""
    from .naming import ASSISTANT

    stamp = (when or datetime.now()).strftime("%Y-%m-%d %H:%M")
    return f"Generated by {ASSISTANT} from {what}, {stamp}. Check it before it goes out."
