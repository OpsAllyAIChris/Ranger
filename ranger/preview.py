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
KINDS = ("docx", "xlsx", "pdf", "md")

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

#: The label the preview chrome shows. The .docx one is the important string in
#: this file: it is the difference between a preview and a claim about Word.
CAVEATS = {
    "pdf": "The real PDF, rendered by the browser. Exact.",
    "md": (
        "The draft itself, read off disk. The words are exact; the styling is "
        "this window's."
    ),
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
    #: Where they are, as (row, column) zero-based. The preview does not use
    #: this; the import does, so it can refuse a gross profit column that holds
    #: formulas rather than importing a cached value that was true whenever the
    #: file was last opened by something that calculates.
    formula_cells: set = field(default_factory=set)

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
    #: Markdown only: the front matter, as written. Provenance, not content --
    #: it is drawn as a line above the draft rather than as part of it, because
    #: "status: draft, not sent" pasted into an email would be a bad day.
    front: dict[str, str] = field(default_factory=dict)
    #: Markdown only: the file's own text, for copying the source out.
    source: str = ""
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
            "front": self.front,
            "source": self.source,
            "plain": self.as_plain(),
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


    def as_plain(self) -> str:
        """The same content with the markup taken off, for pasting elsewhere.

        **Not `as_text()`.** That one puts markdown back on -- `#` for
        headings, `-` for bullets -- because its reader is a model that
        benefits from the structure. This one is for an Outlook window, where a
        heading is a line and `**bold**` is two asterisks somebody has to
        delete by hand.

        Built from the same blocks the preview draws, so what is copied is what
        was on the screen, and a table keeps its tabs so that pasting into Word
        or Excel lands in cells.
        """
        lines: list[str] = []
        previous = ""
        for block in self.blocks:
            kind = block["kind"]
            # A blank line between paragraphs, and between a run of bullets and
            # whatever follows it -- but not between two bullets, which are a
            # list and read as one.
            if lines and not (kind == "bullet" and previous == "bullet"):
                lines.append("")
            previous = kind
            if kind == "table":
                for row in block.get("rows", []):
                    lines.append("\t".join(row))
                continue
            if kind == "rule":
                lines.append("---")
                continue
            text = strip_markup(block.get("text", ""))
            lines.append("\u2022 " + text if kind == "bullet" else text)
        for sheet in self.sheets:
            if lines:
                lines.append("")
            lines.append(sheet.name)
            for row in sheet.rows:
                lines.append("\t".join(row.cells))
        # Collapse the runs of blank lines that headings and tables leave
        # behind, but keep single ones: paragraph breaks are the shape of an
        # email.
        out: list[str] = []
        for line in lines:
            if not line.strip() and out and not out[-1].strip():
                continue
            out.append(line.rstrip())
        return "\n".join(out).strip() + "\n"


#: Inline markdown, in the order it has to come off. Emphasis before bold would
#: leave a stray asterisk on every bold word.
_INLINE = (
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),      # image, keep the alt
    (re.compile(r"\[([^\]]+)\]\(([^)]*)\)"), r"\1 (\2)"),  # link, keep the target
    (re.compile(r"\*\*\*(.+?)\*\*\*"), r"\1"),
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),
    (re.compile(r"(?<!\w)_{2}(.+?)_{2}(?!\w)"), r"\1"),
    (re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)"), r"\1"),
    (re.compile(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)"), r"\1"),
    (re.compile(r"`([^`]+)`"), r"\1"),
    (re.compile(r"~~(.+?)~~"), r"\1"),
)


def strip_markup(text: str) -> str:
    """Markdown syntax off, the words left alone.

    Deliberately conservative. An underscore inside a word is a filename or a
    column header far more often than it is emphasis, and a lone asterisk in a
    sentence is usually a footnote mark. Taking off less than a full markdown
    parser would is the right error to make: a stray character in an email is
    a typo, and a mangled account name is a mistake in front of a customer.
    """
    out = text
    for pattern, replacement in _INLINE:
        out = pattern.sub(replacement, out)
    return out


def kind_of(path: Path) -> str:
    return path.suffix.lower().lstrip(".")


def previewable(path: Path) -> bool:
    return kind_of(path) in KINDS


def preview(path: Path, *, root: Path | None = None, max_blocks: int = MAX_BLOCKS,
            max_rows: int = MAX_ROWS, kind: str = "") -> Preview:
    """Read the file on disk. Never the content it was made from.

    `kind` overrides the extension, for a caller that has already looked inside
    the file. A dropped export named `.xls` is routinely a real .xlsx, an HTML
    table or an XML spreadsheet, and the name is the least reliable thing about
    it -- see `tabular.py`.
    """
    kind = kind or kind_of(path)
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
        if kind == "md":
            return _markdown(path, base, max_blocks)
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
                # Named, not `index`: the column index below reuses that name
                # for each cell, so a row number kept in it would be whatever
                # the previous cell's column was.
                row_index = sheet.total_rows
                sheet.total_rows += 1
                cells: list[str] = []
                for cell in row.findall(f"{S}c"):
                    formula = cell.find(f"{S}f")
                    if formula is not None:
                        sheet.formula_cells.add(
                            (row_index, _column_index(cell.get("r", "")))
                        )
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


# -- markdown ---------------------------------------------------------------
#
# The easy case, and the one that is read most: drafts are emails and notes and
# the operator wants to read one without opening Obsidian.
#
# Blocks come out in the same shape the .docx reader produces -- heading, text,
# bullet, table -- so the browser draws them with the same code and there is
# one renderer rather than two that drift.

#: A line at least this long is taken to have been wrapped rather than ended.
#: Below the narrowest wrap width anyone uses, above a sign-off or an address
#: line.
WRAPPED = 60

_MD_HEADING = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*#*$")
_MD_BULLET = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+(?P<text>.+)$")
_MD_RULE = re.compile(r"^\s*(?:[-*_]\s*){3,}$")
_MD_QUOTE = re.compile(r"^\s*>\s?(?P<text>.*)$")
_MD_ROW = re.compile(r"^\s*\|(?P<cells>.+)\|\s*$")
_MD_DIVIDER = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")


def front_matter(text: str) -> tuple[dict[str, str], str]:
    """The `---` block at the top, and the rest.

    Split off rather than rendered. It is provenance -- created, title,
    account, status -- and it belongs above the draft as a line about the file,
    not inside it where "status: draft, not sent" could be copied into an email.
    """
    match = _FRONT.match(text)
    if not match:
        return {}, text
    fields: dict[str, str] = {}
    for line in match.group("body").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            fields[key.strip()] = value.strip()
    return fields, text[match.end():]


_FRONT = re.compile(r"\A---\n(?P<body>.*?)\n---\n?", re.DOTALL)


def _markdown(path: Path, base: Preview, max_blocks: int) -> Preview:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Written on Windows by something other than Jarvis. Read it rather
        # than refusing it: a draft that cannot be read is worse than one shown
        # with a wrong dash.
        text = path.read_text(encoding="cp1252", errors="replace")

    base.front, body = front_matter(text)
    base.source = text
    blocks: list[dict[str, Any]] = []
    rows: list[list[str]] = []
    para: list[str] = []
    fenced = False

    def close_table() -> None:
        nonlocal rows
        if rows:
            blocks.append({"kind": "table", "rows": rows})
            rows = []

    def close_para() -> None:
        """Consecutive lines into one paragraph, except where the break meant
        something.

        Markdown says consecutive lines are one paragraph, and a draft
        hard-wrapped at eighty characters must not paste into Outlook with a
        break every eighty characters. But::

            Best,
            Chris

        is two lines on purpose, and joining them is just as wrong the other
        way. Markdown's own answer -- two trailing spaces -- is not something
        anybody types, and these drafts are written to be read as email.

        **So the previous line's length decides.** A line that ran to near the
        wrap width was wrapped; a short one ended because somebody ended it.
        A heuristic, and wrong sometimes: a short line in the middle of a
        wrapped paragraph keeps a break it did not ask for. That is the
        harmless direction -- a stray break in an email is a typo, a sign-off
        run onto one line is a draft that looks careless.
        """
        nonlocal para
        if not para:
            return
        text = para[0]
        for line in para[1:]:
            text += (" " if len(text.rsplit("\n", 1)[-1]) >= WRAPPED else "\n") + line
        blocks.append({"kind": "text", "text": text})
        para = []

    for line in body.splitlines():
        if line.strip().startswith("```"):
            close_para()
            close_table()
            fenced = not fenced
            continue
        if fenced:
            # Inside a fence the text is literal: kept verbatim, never joined
            # into a paragraph, and never has markup stripped off it.
            blocks.append({"kind": "text", "text": line, "code": True})
            continue

        row = _MD_ROW.match(line)
        if row and not _MD_DIVIDER.match(line):
            close_para()
            rows.append([cell.strip() for cell in row.group("cells").split("|")])
            continue
        if _MD_DIVIDER.match(line) and rows:
            continue
        close_table()

        if not line.strip():
            close_para()
            continue
        if _MD_RULE.match(line):
            close_para()
            blocks.append({"kind": "rule", "text": ""})
            continue
        heading = _MD_HEADING.match(line)
        if heading:
            close_para()
            blocks.append({
                "kind": "heading",
                "level": len(heading.group("hashes")),
                "text": heading.group("text"),
            })
            continue
        bullet = _MD_BULLET.match(line)
        if bullet:
            close_para()
            blocks.append({"kind": "bullet", "text": bullet.group("text")})
            continue
        quote = _MD_QUOTE.match(line)
        if quote:
            close_para()
            blocks.append({"kind": "text", "text": quote.group("text"), "quote": True})
            continue
        para.append(line.strip())
    close_para()
    close_table()

    base.total_blocks = len(blocks)
    base.truncated = len(blocks) > max_blocks
    base.blocks = blocks[:max_blocks]
    return base
