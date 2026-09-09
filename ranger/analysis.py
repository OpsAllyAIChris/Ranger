"""Analysis of a dropped file: **every number computed here, in Python.**

The operator drops an export and asks a question of it. "Total commission by
account for August" is two different jobs, and this module does exactly one of
them.

**The model chooses; Python computes.** Which grouping answers the question,
what the columns mean, what the result implies, what looks wrong: judgement,
and the model's. Sums, counts, averages, deltas, percentages, ordering: the
model does not touch them, and the tool it calls has nowhere to put a number
even if it tried. A spec names columns and an operation; there is no field in
it that takes a value the model made up.

**The artifact is written before it is shown.** Every table the operator sees
was computed here, written to a file under `Ranger/analysis/`, and rendered
back out of that file. Two reasons, and the second is the one that bites:
"export this to Excel" is then a format change on a file that has already been
seen rather than a second computation that might disagree; and a figure on
screen at four o'clock is still reproducible at six.

Create-only, like everything else. A report is never edited, and asking the
same question twice writes a second report beside the first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

FOLDER = "analysis"

#: What Python will do to a column. There is no "expression" here on purpose:
#: an operation the model could write is an operation the model could get
#: wrong, and every one of these is a function in this file.
OPERATIONS = ("sum", "count", "average", "min", "max")

#: How many rows of a result the panel draws before saying how many are left.
#: The computation always runs over every row; this is only what is rendered,
#: the same rule the document preview follows.
MAX_SHOWN = 200

#: Labels that mean "this row is the file's own arithmetic, not data". A
#: totals line grouped alongside the rows it totals doubles the answer, and the
#: doubled figure looks entirely normal on screen. They are excluded **and
#: named**: silently dropping a row is its own hazard, so the report says which
#: ones went and why.
TOTAL_LABELS = re.compile(
    r"^(grand\s+)?(total|totals|sum|subtotal|sub-total|all\s+accounts)\b[: ]*$", re.I
)

#: A hard ceiling on how many groups a single analysis may produce, so a
#: grouping by a free-text column cannot turn into a 40,000 row report.
MAX_GROUPS = 5000


class AnalysisError(Exception):
    """The analysis was refused, and why. Never a silent empty table."""


@dataclass(frozen=True)
class Filter:
    """One exact match on one column. Not an expression."""

    column: str
    equals: str

    def describe(self) -> str:
        return f"{self.column} is {self.equals!r}"


@dataclass(frozen=True)
class Spec:
    """What to compute. **Every field names a column or picks an operation.**

    There is deliberately nowhere in here to put a figure. The model can say
    "group by Account and sum Commission"; it cannot say "the total is 291,546",
    because this structure has no field that would carry it.
    """

    file: str
    group_by: str = ""
    value: str = ""
    operation: str = "sum"
    sheet: str = ""
    where: Filter | None = None
    compare_to: Filter | None = None
    sort: str = "value"
    descending: bool = True
    #: The model's own words for what the question was. Checked for digits and
    #: refused if it carries any that did not come from the spec, because a
    #: sentence is exactly where a made-up figure would hide.
    title: str = ""


@dataclass
class Row:
    label: str
    value: Decimal | int
    compared: Decimal | int | None = None
    delta: Decimal | int | None = None
    count: int = 0


@dataclass
class Result:
    """What Python worked out, and everything needed to say where it came from."""

    spec: Spec
    rows: list[Row] = field(default_factory=list)
    total: Decimal | int | None = None
    compared_total: Decimal | int | None = None
    #: Rows of the source that were read, and rows that were not figures.
    read: int = 0
    skipped: int = 0
    #: Rows left out because they were the file's own totals, by label.
    excluded: list[str] = field(default_factory=list)
    source: str = ""
    sheet: str = ""
    columns: tuple[str, ...] = ()
    when: datetime | None = None

    @property
    def money(self) -> bool:
        return self.spec.operation in ("sum", "average", "min", "max")

    def summary(self) -> str:
        """The sentence above the table, **written here rather than by a model.**

        Every figure in it came out of the arithmetic below. This is the line
        the panel shows and the line the model is handed; it is not asked to
        write one of its own, because a paragraph is where an invented number
        would look most convincing.
        """
        if not self.rows:
            return f"Nothing in {self.source} matched."
        what = {
            "sum": "total", "count": "count", "average": "average",
            "min": "lowest", "max": "highest",
        }[self.spec.operation]
        parts = [
            f"{len(self.rows)} {self.spec.group_by or 'row'}(s)",
            f"{what} of {self.spec.value}" if self.spec.value else what,
        ]
        if self.total is not None:
            parts.append(f"totalling {render(self.total)}")
        if self.spec.where:
            parts.append(f"where {self.spec.where.describe()}")
        if self.compared_total is not None:
            parts.append(
                f"against {render(self.compared_total)} for "
                f"{self.spec.compare_to.equals!r}"
            )
        if self.skipped:
            parts.append(f"{self.skipped} row(s) had no readable figure")
        if self.excluded:
            parts.append(
                f"{len(self.excluded)} totals row(s) left out ("
                + ", ".join(repr(name) for name in self.excluded[:3]) + ")"
            )
        return ", ".join(parts) + "."


def render(value: Any) -> str:
    """A figure a person reads. Formatting happens once, here."""
    if isinstance(value, Decimal):
        quantised = value.quantize(Decimal("0.01"))
        sign = "-" if quantised < 0 else ""
        return f"{sign}{abs(quantised):,.2f}"
    return f"{value:,}"


_NUMBER = re.compile(r"\d")
_ACCOUNTING = re.compile(r"^\((?P<inner>.+)\)$")


def _amount(raw: Any) -> Decimal | None:
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


def check_title(spec: Spec) -> str:
    """Refuse a caption carrying a figure the model made up.

    **The failure this exists for**: a model writing "$291,546" into a sentence
    above a table. It looks right, it is the one number nobody computed, and it
    is the number the operator would repeat to a customer.

    A digit is allowed through only when it appears in the spec's own filter
    values, which came from the file. Everything else is refused, and the
    message tells the model to leave the numbers to the table.
    """
    title = spec.title or ""
    if not _NUMBER.search(title):
        return ""
    allowed = " ".join(
        [f.equals for f in (spec.where, spec.compare_to) if f is not None]
        + [spec.group_by, spec.value, spec.file]
    )
    for run in re.findall(r"\d[\d,.]*", title):
        if run.strip(".,") and run.strip(".,") not in allowed:
            return (
                f"The title carries a figure ({run}) that Jarvis did not compute. "
                "Titles say what the question was; the numbers come from the table "
                "underneath, which Python fills in. Send the title without it."
            )
    return ""


def run(table: Any, spec: Spec, *, source: str = "", now: datetime | None = None) -> Result:
    """Group and total, over every row. The whole of the arithmetic.

    Columns are resolved against the header row and anything else is refused:
    a column name that is not in the file is a question nobody can answer, and
    guessing at the nearest one is how the wrong column gets summed.
    """
    from . import shapes

    if spec.operation not in OPERATIONS:
        raise AnalysisError(
            f"{spec.operation!r} is not something Jarvis computes. It does: "
            + ", ".join(OPERATIONS)
        )
    problem = check_title(spec)
    if problem:
        raise AnalysisError(problem)

    header_index = shapes.header_row(table.rows)
    if header_index < 0:
        raise AnalysisError(f"{source} has no header row Jarvis can see")
    headers = table.header_at(header_index)
    lowered = [h.casefold() for h in headers]

    def column(name: str, what: str) -> int:
        wanted = " ".join(str(name or "").split()).casefold()
        if not wanted:
            raise AnalysisError(f"which column is the {what}?")
        if wanted not in lowered:
            raise AnalysisError(
                f"there is no column called {name!r} in {source}. "
                f"The columns are: {', '.join(headers)}"
            )
        return lowered.index(wanted)

    group_at = column(spec.group_by, "grouping") if spec.group_by else -1
    value_at = column(spec.value, "figure") if spec.value else -1
    where_at = column(spec.where.column, "filter") if spec.where else -1
    compare_at = column(spec.compare_to.column, "comparison") if spec.compare_to else -1
    if spec.operation != "count" and value_at < 0:
        raise AnalysisError("a sum needs a column to add up")

    groups: dict[str, list[Decimal]] = {}
    compared: dict[str, list[Decimal]] = {}
    excluded: list[str] = []
    skipped = read = 0

    for index, row in enumerate(table.rows):
        if index <= header_index or not any(str(cell).strip() for cell in row):
            continue

        def cell(at: int) -> str:
            return str(row[at]) if 0 <= at < len(row) else ""

        in_main = True
        in_compared = False
        if spec.where is not None:
            in_main = cell(where_at).strip().casefold() == spec.where.equals.casefold()
        if spec.compare_to is not None:
            in_compared = (
                cell(compare_at).strip().casefold() == spec.compare_to.equals.casefold()
            )
        if not (in_main or in_compared):
            continue

        label = cell(group_at).strip() if group_at >= 0 else "All rows"
        if group_at >= 0 and TOTAL_LABELS.match(label):
            # The file's own total, grouped beside the rows it totals, would
            # double the answer -- and a doubled figure looks entirely normal.
            if label not in excluded:
                excluded.append(label)
            continue
        read += 1
        if not label:
            label = "(blank)"
        if spec.operation == "count":
            figure = Decimal(1)
        else:
            figure = _amount(cell(value_at))
            if figure is None:
                skipped += 1
                continue
        if in_main:
            groups.setdefault(label, []).append(figure)
        if in_compared:
            compared.setdefault(label, []).append(figure)
        if len(groups) > MAX_GROUPS:
            raise AnalysisError(
                f"grouping by {spec.group_by!r} makes more than {MAX_GROUPS} groups, "
                "which is a list rather than an answer. Group by something coarser."
            )

    def reduce(values: list[Decimal]):
        if spec.operation == "count":
            return len(values)
        if not values:
            return None
        if spec.operation == "sum":
            return sum(values, Decimal(0))
        if spec.operation == "average":
            return (sum(values, Decimal(0)) / Decimal(len(values))).quantize(Decimal("0.01"))
        return min(values) if spec.operation == "min" else max(values)

    rows: list[Row] = []
    for label, values in groups.items():
        value = reduce(values)
        other = reduce(compared[label]) if label in compared else None
        delta = None
        if value is not None and other is not None:
            delta = value - other
        rows.append(Row(label=label, value=value, compared=other, delta=delta, count=len(values)))
    # A label present only in the comparison is still an answer: it went to zero.
    for label, values in compared.items():
        if label in groups:
            continue
        other = reduce(values)
        rows.append(Row(label=label, value=0 if spec.operation == "count" else Decimal(0),
                        compared=other, delta=-other if other is not None else None,
                        count=0))

    if spec.sort == "label":
        rows.sort(key=lambda item: item.label.casefold(), reverse=not spec.descending)
    else:
        rows.sort(key=lambda item: (item.value is None, item.value or 0),
                  reverse=spec.descending)

    totals = [row.value for row in rows if row.value is not None]
    compared_totals = [row.compared for row in rows if row.compared is not None]
    return Result(
        spec=spec,
        rows=rows,
        total=(sum(totals, Decimal(0)) if spec.operation != "count" else sum(totals))
        if totals and spec.operation in ("sum", "count") else None,
        compared_total=(
            sum(compared_totals, Decimal(0)) if spec.operation != "count"
            else sum(compared_totals)
        ) if compared_totals and spec.operation in ("sum", "count") else None,
        read=read,
        skipped=skipped,
        excluded=excluded,
        source=source,
        sheet=spec.sheet or getattr(table, "name", ""),
        columns=tuple(headers),
        when=now or datetime.now(),
    )


# -- the artifact -----------------------------------------------------------


def folder_for(config: Any, today: date | None = None) -> Path:
    return config.vault.ranger / FOLDER / (today or date.today()).isoformat()


def slugify(text: str) -> str:
    from .drafts import slugify as _slug

    return _slug(text or "analysis")


def report_text(result: Result) -> str:
    """The report, as the file that will be rendered. Markdown, and parseable.

    The sheet draws this file rather than the result object, so what is on
    screen is what is on disk. The columns keep the source's own names: a
    header renamed on the way to the screen is a column the operator cannot
    find again in their own export.
    """
    spec = result.spec
    when = (result.when or datetime.now()).isoformat(timespec="seconds")
    head = [
        "---",
        f"title: {spec.title or 'Analysis'}",
        f"source: {result.source}",
        f"sheet: {result.sheet}",
        f"operation: {spec.operation}",
        f"group_by: {spec.group_by}",
        f"value: {spec.value}",
        f"rows_read: {result.read}",
        f"rows_out: {len(result.rows)}",
        f"computed: {when}",
        "---",
        "",
        f"# {spec.title or 'Analysis'}",
        "",
        result.summary(),
        "",
        "Every figure below was computed in Python from the file named above. "
        "Nothing here was written by a language model.",
        "",
    ]

    label = spec.group_by or "Row"
    value_name = spec.value or "Count"
    columns = [label, value_name]
    if spec.compare_to is not None:
        columns += [f"{value_name} ({spec.compare_to.equals})", "Change"]
    if spec.operation not in ("count",):
        columns.append("Rows")

    body = ["| " + " | ".join(columns) + " |",
            "| " + " | ".join("---" for _ in columns) + " |"]
    for row in result.rows:
        cells = [row.label, render(row.value) if row.value is not None else ""]
        if spec.compare_to is not None:
            cells.append(render(row.compared) if row.compared is not None else "")
            cells.append(render(row.delta) if row.delta is not None else "")
        if spec.operation not in ("count",):
            cells.append(str(row.count))
        body.append("| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |")

    tail = ["", "## Where this came from", ""]
    tail.append(f"- file: {result.source}")
    if result.sheet:
        tail.append(f"- sheet: {result.sheet}")
    tail.append(f"- columns used: {spec.group_by or '(none)'} / {spec.value or '(none)'}")
    if spec.where is not None:
        tail.append(f"- filtered where: {spec.where.describe()}")
    if spec.compare_to is not None:
        tail.append(f"- compared against: {spec.compare_to.describe()}")
    tail.append(f"- rows read: {result.read}, groups out: {len(result.rows)}")
    if result.skipped:
        tail.append(f"- rows with no readable figure: {result.skipped}")
    if result.excluded:
        tail.append(
            "- left out as the file's own totals: "
            + ", ".join(repr(name) for name in result.excluded)
        )
    tail.append(f"- computed: {when}")
    return "\n".join(head + body + tail) + "\n"


def write(vault: Any, config: Any, result: Result, *, today: date | None = None) -> Path:
    """Create-only, in a dated folder, like everything else here."""
    folder = folder_for(config, today)
    stamp = (result.when or datetime.now()).strftime("%H%M%S")
    stem = f"{slugify(result.spec.title or result.spec.group_by or 'analysis')} {stamp}"
    target = folder / f"{stem}.md"
    counter = 2
    while target.exists():
        target = folder / f"{stem} ({counter}).md"
        counter += 1
    return vault.write_new(target, report_text(result))


@dataclass
class Report:
    """A written report, read back off disk for rendering."""

    path: Path
    relative: str
    title: str
    summary: str
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    provenance: list[str] = field(default_factory=list)
    total_rows: int = 0
    truncated: bool = False
    source: str = ""
    sheet: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "columns": self.columns,
            "rows": self.rows,
            "provenance": self.provenance,
            "total_rows": self.total_rows,
            "truncated": self.truncated,
            "source": self.source,
            "sheet": self.sheet,
            "relative": self.relative,
        }


_FRONT = re.compile(r"\A---\n(?P<body>.*?)\n---\n", re.DOTALL)
_FIELD = re.compile(r"^(?P<key>[a-z_]+):[ \t]*(?P<value>.*?)[ \t]*$", re.MULTILINE)


def read_report(path: Path, root: Path | None = None, *, max_rows: int = MAX_SHOWN) -> Report:
    """Parse a written report back. **The sheet renders this, not the result.**

    Preview the artifact, not the intention: a table on screen that was never
    written down is a number nobody can reproduce, and an export built from a
    second computation is an export that can disagree with what was seen.
    """
    text = path.read_text(encoding="utf-8")
    front = _FRONT.match(text)
    fields = (
        {m.group("key"): m.group("value") for m in _FIELD.finditer(front.group("body"))}
        if front else {}
    )
    body = text[front.end():] if front else text

    columns: list[str] = []
    rows: list[list[str]] = []
    provenance: list[str] = []
    summary_lines: list[str] = []
    total = 0
    in_provenance = False

    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## Where this came from"):
            in_provenance = True
            continue
        if in_provenance:
            if stripped.startswith("- "):
                provenance.append(stripped[2:])
            continue
        if stripped.startswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if all(set(cell) <= set("-: ") for cell in cells):
                continue
            if not columns:
                columns = cells
                continue
            total += 1
            if len(rows) < max_rows:
                rows.append(cells)
            continue
        if stripped.startswith("#") or not stripped:
            continue
        if not columns:
            summary_lines.append(stripped)

    return Report(
        path=path,
        relative=path.relative_to(root).as_posix() if root else path.name,
        title=fields.get("title", "Analysis"),
        summary=" ".join(summary_lines[:1]),
        columns=columns,
        rows=rows,
        provenance=provenance,
        total_rows=total,
        truncated=total > len(rows),
        source=fields.get("source", ""),
        sheet=fields.get("sheet", ""),
    )


def to_document(report: Report):
    """The report as a document spec, for exporting what is on screen.

    A format change on a file that has already been seen, built from that same
    file: the export cannot disagree with the table it came from, because it is
    made out of it.
    """
    from . import documents

    return documents.Spec(
        title=report.title,
        subtitle=report.summary,
        blocks=(
            documents.table(report.columns, report.rows, name="Analysis"),
            documents.heading("Where this came from", 2),
            *[documents.bullet(line) for line in report.provenance],
        ),
        source=documents.provenance(report.source or "a dropped file"),
    )
