"""Item D2. The in-window preview, rendered from the file on disk.

**Preview the artifact, not the intention.** Everything here starts by opening
the file that was written and reading what is actually in it. Nothing previews
the content the model produced before it became a document. A preview built
upstream of generation agrees with the export right up until it does not, and
the place that disagreement surfaces is in front of a customer.

That rule has a second use, which is why this module is stdlib only. The
generator writes .docx with python-docx and .xlsx with openpyxl. If those same
libraries read the file back, the round trip proves they are self-consistent
and nothing else. `zipfile` and `ElementTree` opening the OOXML parts directly
is an independent implementation, so the preview passing is evidence about the
file rather than about the library.

## What each preview honestly is

- **PDF renders the real PDF.** The browser's own viewer, the bytes on disk,
  every page. Exact. Nothing in this module renders it.
- **.docx is an approximation of Word's rendering**, and says so in the
  preview chrome. Paragraph text, headings, bullets and table cells, in order.
  Not fonts, not spacing, not page breaks, not floats. Do not read it as
  fidelity; read it as "the words are these, in this order".
- **.xlsx previews sheet values**, and says that formulas are not shown. The
  cached result of a formula is not read either: a file this repository wrote
  has no formulas in it at all, and one that came from elsewhere would be
  showing a stale cached value, which is worse than showing nothing.

## Untrusted, still

Text extracted here came out of a document assembled from account notes and
pasted email. It goes to the browser as data to display. When it goes anywhere
near the model -- `read_own_file` on a generated document uses this module --
it is fenced by the caller, the same as any other content the vault holds.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

#: What can be previewed, and how honest each one is.
KINDS = ("docx", "xlsx", "pdf")

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

#: The label the preview chrome shows. The .docx one is the important string in
#: this file: it is the difference between a preview and a claim about Word.
CAVEATS = {
    "pdf": "The real PDF, rendered by the browser. Exact.",
    "docx": (
        "Approximate. This is the text and tables read out of the file, not "
        "Word's rendering: fonts, spacing, page breaks and layout will differ."
    ),
    "xlsx": (
        "Sheet values only. Formulas are not shown, and neither are formats, "
        "column widths or charts."
    ),
}

#: How much of a long document is drawn. A forty page document is real, and so
#: is a browser that has to stay responsive during a customer call.
MAX_BLOCKS = 400
MAX_ROWS = 300
MAX_COLUMNS = 40
MAX_CHARS = 4000


class PreviewError(Exception):
    """The file could not be read as the kind it claims to be."""


@dataclass(frozen=True)
class Row:
    cells: tuple[str, ...] = ()

    def as_list(self) -> list[str]:
        return list(self.cells)


@dataclass
class Sheet:
    name: str
    rows: list[Row] = field(default_factory=list)
    #: How many rows the sheet actually has, before the ceiling above.
    total_rows: int = 0
    #: Cells that hold a formula. Counted, never evaluated, never shown.
    formulas: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "rows": [row.as_list() for row in self.rows],
            "total_rows": self.total_rows,
            "formulas": self.formulas,
            "truncated": len(self.rows) < self.total_rows,
        }


@dataclass
class Preview:
    """What the browser draws, and what it must say while drawing it."""

    kind: str
    name: str
    relative: str
    caveat: str
    #: docx: {"kind": heading|text|bullet|table, ...}
    blocks: list[dict[str, Any]] = field(default_factory=list)
    sheets: list[Sheet] = field(default_factory=list)
    #: PDF only. The browser fetches the file itself.
    pages: int | None = None
    total_blocks: int = 0
    truncated: bool = False
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "relative": self.relative,
            "caveat": self.caveat,
            "blocks": self.blocks,
            "sheets": [sheet.as_dict() for sheet in self.sheets],
            "pages": self.pages,
            "total_blocks": self.total_blocks,
            "truncated": self.truncated,
            "error": self.error,
        }

    def as_text(self) -> str:
        """The document as plain text, for a caller that has no screen.

        Used by `read_own_file` and by `ranger drafts preview`. Fenced as
        untrusted by whoever hands it to the model.
        """
        lines: list[str] = []
        for block in self.blocks:
            if block["kind"] == "heading":
                lines.append(("#" * max(1, block.get("level", 1))) + " " + block["text"])
            elif block["kind"] == "bullet":
                lines.append("- " + block["text"])
            elif block["kind"] == "table":
                for row in block.get("rows", []):
                    lines.append(" | ".join(row))
            elif block["text"]:
                lines.append(block["text"])
        for sheet in self.sheets:
            lines.append(f"## {sheet.name}")
            for row in sheet.rows:
                lines.append(" | ".join(row.cells))
            if sheet.formulas:
                lines.append(f"({sheet.formulas} formula cells, not shown)")
        return "\n".join(lines)


def kind_of(path: Path) -> str:
    return path.suffix.lower().lstrip(".")


def previewable(path: Path) -> bool:
    return kind_of(path) in KINDS


def preview(path: Path, *, root: Path | None = None, max_blocks: int = MAX_BLOCKS,
            max_rows: int = MAX_ROWS) -> Preview:
    """Read the file on disk. Never the content it was made from."""
    kind = kind_of(path)
    relative = path.relative_to(root).as_posix() if root else path.name
    if kind not in KINDS:
        raise PreviewError(f"{path.name} is not something Jarvis can preview")
    if not path.is_file():
        raise PreviewError(f"{path.name} is not on disk")

    base = Preview(kind=kind, name=path.name, relative=relative, caveat=CAVEATS[kind])
    try:
        if kind == "docx":
            return _docx(path, base, max_blocks)
        if kind == "xlsx":
            return _xlsx(path, base, max_rows)
        return _pdf(path, base)
    except PreviewError:
        raise
    except Exception as exc:
        # A file that cannot be parsed says so. It never renders as an empty
        # document, which would look like a document with nothing in it.
        base.error = f"{type(exc).__name__}: {exc}"
        return base


# -- .docx ------------------------------------------------------------------


def _text_of(node) -> str:
    """Every w:t under this node, in document order.

    Runs split on formatting boundaries and on spell-check state, so a single
    sentence is routinely four runs. Joining without a separator is what Word
    does, which is why a word is not silently split in half here.
    """
    return "".join(part.text or "" for part in node.iter(f"{W}t"))


def _style_of(paragraph) -> str:
    style = paragraph.find(f"{W}pPr/{W}pStyle")
    return (style.get(f"{W}val") or "") if style is not None else ""


def _docx(path: Path, base: Preview, max_blocks: int) -> Preview:
    with zipfile.ZipFile(path) as archive:
        try:
            xml = archive.read("word/document.xml")
        except KeyError as exc:
            raise PreviewError(
                f"{path.name} is a zip file but has no word/document.xml in it, "
                "so it is not a Word document"
            ) from exc
    body = ElementTree.fromstring(xml).find(f"{W}body")
    if body is None:
        raise PreviewError(f"{path.name} has no document body")

    blocks: list[dict[str, Any]] = []
    total = 0
    for child in body:
        tag = child.tag
        if tag == f"{W}p":
            content = _text_of(child).strip()
            if not content:
                continue
            total += 1
            if len(blocks) >= max_blocks:
                continue
            style = _style_of(child)
            if style.startswith("Heading") or style == "Title":
                level = 1 if style == "Title" else int(
                    re.sub(r"\D", "", style) or 1
                )
                blocks.append({"kind": "heading", "text": content[:MAX_CHARS], "level": level})
            elif "ListParagraph" in style or "ListBullet" in style:
                blocks.append({"kind": "bullet", "text": content[:MAX_CHARS]})
            else:
                blocks.append({"kind": "text", "text": content[:MAX_CHARS]})
        elif tag == f"{W}tbl":
            total += 1
            if len(blocks) >= max_blocks:
                continue
            rows = []
            for row in child.findall(f"{W}tr"):
                rows.append([_text_of(cell).strip() for cell in row.findall(f"{W}tc")])
            blocks.append({"kind": "table", "text": "", "rows": rows})

    base.blocks = blocks
    base.total_blocks = total
    base.truncated = total > len(blocks)
    return base


# -- .xlsx ------------------------------------------------------------------


_REF = re.compile(r"^([A-Z]+)(\d+)$")


def _column_index(ref: str) -> int:
    found = _REF.match(ref or "")
    if not found:
        return 0
    letters = found.group(1)
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        xml = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ElementTree.fromstring(xml)
    return ["".join(part.text or "" for part in item.iter(f"{S}t")) for item in root]


def _sheet_names(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """(name, target) in workbook order, resolved through the relationships.

    Sheet order in the workbook is not the order of the files in the zip, and
    `sheet1.xml` is not reliably the first sheet. Following the rels is the
    only way to label a sheet correctly, and a preview that labels the wrong
    sheet is a preview that will be believed.
    """
    book = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        node.get("Id"): node.get("Target", "")
        for node in rels
    }
    out: list[tuple[str, str]] = []
    for sheet in book.iter(f"{S}sheet"):
        target = targets.get(sheet.get(f"{R}id"), "")
        if target.startswith("/"):
            target = target[1:]
        elif not target.startswith("xl/"):
            target = f"xl/{target}"
        out.append((sheet.get("name", "sheet"), target))
    return out


def _xlsx(path: Path, base: Preview, max_rows: int) -> Preview:
    with zipfile.ZipFile(path) as archive:
        if "xl/workbook.xml" not in archive.namelist():
            raise PreviewError(
                f"{path.name} is a zip file but has no xl/workbook.xml in it, "
                "so it is not a workbook"
            )
        strings = _shared_strings(archive)
        for name, target in _sheet_names(archive):
            try:
                xml = archive.read(target)
            except KeyError:
                continue
            sheet = Sheet(name=name)
            root = ElementTree.fromstring(xml)
            for row in root.iter(f"{S}row"):
                sheet.total_rows += 1
                cells: list[str] = []
                for cell in row.findall(f"{S}c"):
                    formula = cell.find(f"{S}f")
                    if formula is not None:
                        # Counted and skipped. The cached result is not shown
                        # either: it is whatever was true when the file was
                        # last opened by something that calculates.
                        sheet.formulas += 1
                        value = ""
                    else:
                        value = _cell_value(cell, strings)
                    index = _column_index(cell.get("r", ""))
                    while len(cells) < index:
                        cells.append("")
                    cells.append(value)
                if len(sheet.rows) < max_rows:
                    sheet.rows.append(Row(tuple(cells[:MAX_COLUMNS])))
            base.sheets.append(sheet)
    base.total_blocks = sum(sheet.total_rows for sheet in base.sheets)
    base.truncated = any(len(sheet.rows) < sheet.total_rows for sheet in base.sheets)
    return base


def _cell_value(cell, strings: list[str]) -> str:
    kind = cell.get("t", "")
    if kind == "inlineStr":
        node = cell.find(f"{S}is")
        return "".join(part.text or "" for part in node.iter(f"{S}t")) if node is not None else ""
    value = cell.find(f"{S}v")
    if value is None:
        return ""
    raw = value.text or ""
    if kind == "s":
        try:
            return strings[int(raw)]
        except (ValueError, IndexError):
            return ""
    return raw


# -- .pdf -------------------------------------------------------------------

_PAGE_COUNT = re.compile(rb"/Type\s*/Page[^s]")


def _pdf(path: Path, base: Preview) -> Preview:
    """The browser renders it. This only reports how many pages there are.

    Counted off the bytes, and reported as unknown when the count cannot be
    trusted -- a PDF that uses cross-reference streams hides its page objects
    inside compressed object streams, and a wrong page count printed next to an
    exact rendering would be the one untrue thing on the screen.
    """
    payload = path.read_bytes()
    if not payload.startswith(b"%PDF-"):
        raise PreviewError(f"{path.name} does not start with a PDF header")
    found = len(_PAGE_COUNT.findall(payload))
    base.pages = found or None
    return base
