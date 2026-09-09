"""Reading a table out of whatever the file **actually is**.

A file called `.xls` is very often not one. The export that started this was
`APIMyCommissionStatementDetailRes....xls` from a web system, and web systems
overwhelmingly serve an HTML table with an `.xls` name because Excel opens it.
Others serve the XML Spreadsheet 2003 format, or a CSV, or a real .xlsx with
the wrong extension. openpyxl reads none of those, so the extract came back
empty and Jarvis described a file it had never read. **A file that lands and
cannot be read is worse than one that was refused**, because everything said
about it afterwards is invention.

So nothing here trusts an extension. Every dropped file is sniffed, and what it
turns out to be decides which reader runs:

| What is inside              | How it is read            |
| --------------------------- | ------------------------- |
| `PK\\x03\\x04` zip container   | the stdlib OOXML reader   |
| `\\xd0\\xcf\\x11\\xe0` OLE2       | xlrd, for real .xls       |
| `<html`, `<table`           | the stdlib HTML parser    |
| `<?xml` + `<Workbook`       | XML Spreadsheet 2003      |
| delimited text              | csv                       |

The answer to "can this be read" is worked out **before the file lands**, so
the drop says what will and will not work rather than discovering it later.

Nothing in here evaluates anything. Values come back as strings, formulas are
marked and never given their cached value, and no cell content ever decides
what any of this does.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

#: What a file turns out to be, once looked at rather than named.
XLSX = "xlsx"
XLS = "xls"
HTML_TABLE = "html-table"
XML_SPREADSHEET = "xml-spreadsheet"
DELIMITED = "delimited"
PDF = "pdf"
DOCX = "docx"
DOC = "doc"
TEXT = "text"
UNKNOWN = "unknown"

#: Every table format this can turn into rows.
TABLE_FORMATS = (XLSX, XLS, HTML_TABLE, XML_SPREADSHEET, DELIMITED)


@dataclass
class Table:
    """A sheet, as rows of strings. Formula cells are marked, never guessed."""

    name: str
    rows: list[list[str]] = field(default_factory=list)
    #: (row, column) of every cell that holds a formula. The cached value is
    #: not read: it is whatever was true when the file was last opened by
    #: something that calculates.
    formulas: set[tuple[int, int]] = field(default_factory=set)

    def header_at(self, index: int) -> list[str]:
        return self.rows[index] if 0 <= index < len(self.rows) else []


@dataclass(frozen=True)
class Readability:
    """Whether a file can be read, decided from its contents.

    `why` is written to be read out at drop time. When something cannot be
    read it says what would fix it, because "unsupported" on its own leaves the
    operator with a file and no next step.
    """

    format: str
    readable: bool
    tabular: bool
    why: str
    #: What is missing, when a library would fix it.
    install: str = ""

    def describe(self) -> str:
        return self.why


#: The first bytes of each container this can recognise.
_ZIP = b"PK\x03\x04"
_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_PDF = b"%PDF-"

_HTML_HINT = re.compile(rb"<\s*(html|table|tr|body|meta)\b", re.I)
_XML_HINT = re.compile(rb"<\?xml", re.I)
_WORKBOOK_HINT = re.compile(rb"<\s*Workbook\b", re.I)


def sniff(payload: bytes, name: str = "") -> str:
    """What this file actually is, from its contents. The name is a tiebreak."""
    head = payload[:4096]
    suffix = Path(name or "").suffix.lower()

    if payload.startswith(_ZIP):
        # A zip could be a workbook, a Word document, or neither.
        if b"xl/workbook.xml" in payload[:8192] or _looks_like(payload, "xl/workbook.xml"):
            return XLSX
        if _looks_like(payload, "word/document.xml"):
            return DOCX
        return UNKNOWN
    if payload.startswith(_OLE2):
        # The old Microsoft container. Which application wrote it is inside;
        # the extension is the only cheap clue and it is usually right here.
        return DOC if suffix == ".doc" else XLS
    if payload.startswith(_PDF):
        return PDF
    if _XML_HINT.search(head) and _WORKBOOK_HINT.search(payload[:16384]):
        return XML_SPREADSHEET
    if _HTML_HINT.search(head):
        return HTML_TABLE
    if suffix in (".csv", ".tsv"):
        return DELIMITED
    if suffix in (".md", ".txt", ".json", ".xml"):
        return TEXT
    # A .xls that is none of the above is very often a delimited file with the
    # wrong name, which is worth trying before giving up on it.
    if suffix in (".xls", ".xlsx") and _reads_as_delimited(payload):
        return DELIMITED
    return UNKNOWN


def _looks_like(payload: bytes, member: str) -> bool:
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            return member in archive.namelist()
    except Exception:
        return False


def _reads_as_delimited(payload: bytes) -> bool:
    try:
        text = payload[:8192].decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        return False
    lines = [line for line in text.splitlines() if line.strip()][:5]
    return len(lines) >= 2 and all(("," in line or "\t" in line) for line in lines)


def have(module: str) -> bool:
    try:
        __import__(module)
    except Exception:
        return False
    return True


def readability(payload: bytes, name: str = "") -> Readability:
    """Can this be read, and what does the operator need to know at drop time?"""
    kind = sniff(payload, name)
    shown = Path(name or "").suffix.lower() or "that file"

    if kind == XLSX:
        return Readability(kind, True, True, "Excel workbook. Jarvis can read it.")
    if kind == XLS:
        if have("xlrd"):
            return Readability(kind, True, True, "Old-format Excel. Jarvis can read it.")
        return Readability(
            kind, False, True,
            "This is a real old-format Excel file (.xls), and Jarvis has no reader "
            "for it. Install one with: pip install xlrd -- or open it in Excel and "
            "save it as .xlsx.",
            install="xlrd",
        )
    if kind == HTML_TABLE:
        return Readability(
            kind, True, True,
            f"A web export: an HTML table saved with an {shown} name. Jarvis can "
            "read it.",
        )
    if kind == XML_SPREADSHEET:
        return Readability(
            kind, True, True,
            f"An XML spreadsheet saved with an {shown} name. Jarvis can read it.",
        )
    if kind == DELIMITED:
        return Readability(kind, True, True, "Delimited text. Jarvis can read it.")
    if kind == DOCX:
        return Readability(kind, True, False, "Word document. Jarvis can read the text.")
    if kind == DOC:
        return Readability(
            kind, False, False,
            "This is an old-format Word document (.doc), which Jarvis cannot read. "
            "Open it in Word and save it as .docx.",
        )
    if kind == PDF:
        if have("pypdf"):
            return Readability(kind, True, False, "PDF. Jarvis can read the text.")
        return Readability(
            kind, True, False,
            "PDF. Jarvis can see how many pages it has but cannot read the text: "
            "install a reader with pip install pypdf.",
        )
    if kind == TEXT:
        return Readability(kind, True, False, "Text. Jarvis can read it.")
    return Readability(
        UNKNOWN, False, False,
        f"Jarvis looked inside and could not tell what {shown} is, so it would not "
        "be able to say anything true about it. Save it as .xlsx, .csv or .pdf.",
    )


# -- the readers ------------------------------------------------------------


def tables(path: Path, *, max_rows: int = 100_000) -> list[Table]:
    """Every sheet of whatever this file turns out to be. Values only."""
    payload = path.read_bytes()
    kind = sniff(payload, path.name)
    if kind == XLSX:
        return _from_xlsx(path, max_rows)
    if kind == XLS:
        return _from_xls(path)
    if kind == HTML_TABLE:
        return _from_html(payload)
    if kind == XML_SPREADSHEET:
        return _from_xml(payload)
    if kind == DELIMITED:
        return _from_delimited(payload, path)
    return []


def _from_xlsx(path: Path, max_rows: int) -> list[Table]:
    """The stdlib OOXML reader the document preview uses.

    Not openpyxl, deliberately: it is not the library that wrote the file, and
    a NetSuite export was written by neither.
    """
    from .preview import preview as render

    # `kind` is passed because this file may be named anything at all: the
    # sniff already decided it is a workbook, and preview's own extension
    # check would refuse a real .xlsx called `.xls`.
    rendered = render(path, max_rows=max_rows, kind="xlsx")
    out: list[Table] = []
    for sheet in rendered.sheets:
        table = Table(name=sheet.name)
        table.rows = [list(row.cells) for row in sheet.rows]
        table.formulas = set(getattr(sheet, "formula_cells", set()))
        out.append(table)
    return out


def _from_xls(path: Path) -> list[Table]:
    """Real old-format Excel, through xlrd.

    xlrd 2.x reads .xls and nothing else, which is exactly the gap here. A cell
    that holds a formula comes back as its cached value, so those cells are
    marked and blanked, the same as everywhere else: a cached value is whatever
    was true when the file was last calculated.
    """
    import xlrd

    book = xlrd.open_workbook(str(path), formatting_info=False)
    out: list[Table] = []
    for sheet in book.sheets():
        table = Table(name=sheet.name)
        for row_index in range(sheet.nrows):
            row: list[str] = []
            for column_index in range(sheet.ncols):
                cell = sheet.cell(row_index, column_index)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        parts = xlrd.xldate_as_tuple(cell.value, book.datemode)
                        row.append("%04d-%02d-%02d" % parts[:3])
                        continue
                    except Exception:
                        pass
                if cell.ctype == xlrd.XL_CELL_NUMBER:
                    value = cell.value
                    row.append(str(int(value)) if float(value).is_integer() else str(value))
                    continue
                if cell.ctype == xlrd.XL_CELL_EMPTY:
                    row.append("")
                    continue
                row.append(str(cell.value).strip())
            table.rows.append(row)
        out.append(table)
    return out


class _TableReader(HTMLParser):
    """Rows out of an HTML table, with the stdlib and nothing else.

    Deliberately blunt: it collects text per cell, ignores every attribute, and
    understands no scripting. What comes back is strings. A cell containing
    markup is text that happens to have angle brackets in it.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[Table] = []
        self._rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._rows = []
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._rows.append(self._row)
            self._row = None
        elif tag == "table":
            if self._rows:
                self.tables.append(Table(name=f"Table {len(self.tables) + 1}",
                                         rows=self._rows))
            self._rows = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def close(self):  # noqa: D102 - flush an unclosed table, which is common
        super().close()
        if self._rows:
            self.tables.append(Table(name=f"Table {len(self.tables) + 1}", rows=self._rows))
            self._rows = []


def _from_html(payload: bytes) -> list[Table]:
    reader = _TableReader()
    reader.feed(payload.decode("utf-8", errors="replace"))
    reader.close()
    # Rows are ragged in hand-written HTML: pad so every row is the same width
    # as the widest, because a column index has to mean one thing.
    for table in reader.tables:
        width = max((len(row) for row in table.rows), default=0)
        for row in table.rows:
            row.extend([""] * (width - len(row)))
    return [table for table in reader.tables if table.rows]


_SS = "{urn:schemas-microsoft-com:office:spreadsheet}"


def _from_xml(payload: bytes) -> list[Table]:
    """XML Spreadsheet 2003, which several web systems serve as `.xls`."""
    root = ElementTree.fromstring(payload)
    out: list[Table] = []
    for worksheet in root.iter(f"{_SS}Worksheet"):
        table = Table(name=worksheet.get(f"{_SS}Name", f"Sheet {len(out) + 1}"))
        for row in worksheet.iter(f"{_SS}Row"):
            cells: list[str] = []
            for cell in row.findall(f"{_SS}Cell"):
                # ss:Index skips columns, so a sparse row still lines up.
                index = cell.get(f"{_SS}Index")
                if index:
                    while len(cells) < int(index) - 1:
                        cells.append("")
                data = cell.find(f"{_SS}Data")
                cells.append("".join(data.itertext()).strip() if data is not None else "")
            table.rows.append(cells)
        width = max((len(row) for row in table.rows), default=0)
        for row in table.rows:
            row.extend([""] * (width - len(row)))
        if table.rows:
            out.append(table)
    return out


def _from_delimited(payload: bytes, path: Path) -> list[Table]:
    text = payload.decode("utf-8-sig", errors="replace")
    sample = text[:8192]
    delimiter = "\t" if (path.suffix.lower() == ".tsv" or sample.count("\t") > sample.count(",")) else ","
    rows = [[cell.strip() for cell in row]
            for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
    width = max((len(row) for row in rows), default=0)
    for row in rows:
        row.extend([""] * (width - len(row)))
    return [Table(name=path.stem, rows=[row for row in rows if any(row)])]
