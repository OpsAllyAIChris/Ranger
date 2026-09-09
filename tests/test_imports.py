"""Dropped files: landing, the sidecar, and gross profit that never sees a model.

The design this tests is driven by token cost and by one rule about numbers.

**The file almost never reaches the model.** It lands and nothing happens. The
first question about it writes a bounded sidecar; every later question reads
the sidecar. And the figures never go through a model at all: once a column
mapping is confirmed at a keyboard, Python reads the numbers straight out of
the file.

So the tests are mostly about what did *not* happen. Nothing parsed on drop.
Nothing written on a re-import that changed nothing. No mapping invented from a
file nobody has mapped. No figure produced by anything but arithmetic on cells.

The planted-instruction tests for this path live in
`tests/test_planted_instructions.py`, with the others, because that file is the
evidence about intake paths and this is the highest-volume one there is.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ranger import gp, imports, shapes

TODAY = date(2026, 9, 9)

HEADERS = ["Period", "Revenue", "COGS", "Gross Profit", "Margin %"]
EXPORT = [
    ["Wexxar Packaging Inc."],
    ["Gross Profit by Period"],
    ["1 Jan 2026 - 31 Dec 2026"],
    [],
    HEADERS,
    ["Jun 2026", "182000", "141000", "41000", "22.5"],
    ["Jul 2026", "190500", "145000", "45500", "23.9"],
    ["Aug 2026", "201000", "152750", "48250", "24.0"],
    ["Total", "573500", "438750", "134750", "23.5"],
]


def workbook(rows=None, *, sheets=None, formula_at=None) -> bytes:
    """A .xlsx as a byte string, written by openpyxl and read by nothing that
    knows about openpyxl."""
    import io

    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "GP by Period"
    for row in rows if rows is not None else EXPORT:
        sheet.append(row)
    if formula_at:
        sheet.cell(row=formula_at[0], column=formula_at[1]).value = "=SUM(B2:C2)"
    for name, content in (sheets or {}).items():
        extra = book.create_sheet(name)
        for row in content:
            extra.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def drop(vault, config, payload=None, name="netsuite gp.xlsx", today=TODAY):
    return imports.land(
        vault, config, name, payload if payload is not None else workbook(), today=today
    )


# -- the drop ---------------------------------------------------------------


def test_a_dropped_file_lands_in_a_dated_folder_under_its_own_name(vault, config):
    """A dated snapshot, never a mirror. The folder is what was dropped on the
    day it was dropped, and nothing goes back to keep it in step."""
    landed = drop(vault, config)

    assert landed.path == config.vault.ranger / "imports" / "2026-09-09" / "netsuite gp.xlsx"
    assert landed.relative == "Ranger/imports/2026-09-09/netsuite gp.xlsx"
    assert landed.path.read_bytes().startswith(b"PK")
    assert landed.kind == "Excel workbook"
    assert "netsuite gp.xlsx" in landed.describe() and "KB" in landed.describe()


def test_nothing_is_parsed_by_landing_a_file(vault, config):
    """**A file dropped by accident costs nothing.** No sidecar, no extract, no
    reading, no tokens. The extract is written the first time something is
    actually asked."""
    landed = drop(vault, config)

    beside = list(landed.path.parent.iterdir())
    assert [p.name for p in beside] == ["netsuite gp.xlsx"]
    assert imports.existing_extract(landed.path) is None
    assert not shapes.path_for(config).exists(), "and nothing was learned either"


def test_the_same_file_twice_in_one_day_writes_once(vault, config):
    """Not an error, and not a second copy: identical bytes under the same name
    report that they were already there. The alternative is a folder that
    records how often a file was dragged."""
    first = drop(vault, config)
    second = drop(vault, config)

    assert second.already is True
    assert second.path == first.path
    assert len(list(first.path.parent.glob("*.xlsx"))) == 1
    assert "already imported today" in second.describe()


def test_a_different_file_with_the_same_name_lands_beside_it(vault, config):
    """Next month's export has the same name and different numbers. Replacing
    the first would be a delete."""
    first = drop(vault, config)
    changed = list(EXPORT)
    changed[7] = ["Aug 2026", "201000", "152750", "47900", "23.8"]
    second = drop(vault, config, workbook(changed))

    assert second.already is False
    assert second.path != first.path
    assert second.name == "netsuite gp (2).xlsx"
    assert first.path.is_file() and second.path.is_file()


def test_a_partial_file_never_appears_under_the_real_name(vault, config, monkeypatch):
    """Landing writes a `.part` and renames it into place, so what is in the
    folder is the whole file or nothing at all. A crash mid-write leaves the
    part file, which is not listed, not read, and not deleted."""
    real = Path.replace
    calls: list[tuple[Path, Path]] = []

    def die(self, target):
        calls.append((self, Path(target)))
        raise OSError("the disk went away")

    monkeypatch.setattr(Path, "replace", die)
    with pytest.raises(OSError):
        drop(vault, config)

    folder = config.vault.ranger / "imports" / "2026-09-09"
    assert not (folder / "netsuite gp.xlsx").exists(), "no half file under the real name"
    assert [p.name for p in folder.iterdir()] == ["netsuite gp.xlsx.part"]
    monkeypatch.setattr(Path, "replace", real)
    assert imports.listing(vault, config) == [], "and a part file is not an import"


@pytest.mark.parametrize(
    "name, expected",
    [
        ("../../../etc/passwd.csv", "etcpasswd.csv"),
        ("C:\\Users\\Chris\\gp.xlsx", "CUsersChrisgp.xlsx"),
        ("  spaced   out .csv", "spaced out .csv"),
    ],
)
def test_the_name_comes_from_a_browser_and_is_treated_like_it(vault, config, name, expected):
    assert imports.safe_name(name) == expected


def test_a_file_type_it_does_not_take_is_refused(vault, config):
    """An allow list: the folder is inside the vault and the vault is backed
    up. A dropped installer is not context."""
    with pytest.raises(imports.Refused) as caught:
        imports.land(vault, config, "setup.exe", b"MZ...", today=TODAY)
    assert ".exe" in str(caught.value)
    assert ".xlsx" in str(caught.value), "say what it does take"


def test_a_file_over_the_ceiling_is_refused_with_the_size(vault, config):
    with pytest.raises(imports.Refused) as caught:
        imports.land(vault, config, "huge.csv", b"x" * 2048, today=TODAY, max_bytes=1024)
    assert "over the" in str(caught.value)


def test_imports_are_in_the_snapshot_allow_list():
    """Customer pricing and margin, in files that exist nowhere else once the
    export is gone from someone's downloads folder."""
    from ranger.snapshot import INCLUDED, ignore_file

    assert "Ranger/imports/" in INCLUDED
    assert "!/Ranger/imports/" in ignore_file()


# -- the sidecar ------------------------------------------------------------


def test_the_first_question_writes_a_sidecar_and_the_second_does_not(vault, config):
    """The whole economic argument, as a test. A 340KB workbook becomes about
    2KB of context once, and re-reading is free."""
    landed = drop(vault, config)

    text, written = imports.sidecar_text(vault, landed.path)
    assert written is True
    assert len(text) < 4000

    again, written_again = imports.sidecar_text(vault, landed.path)
    assert written_again is False, "the second question reads the sidecar"
    assert again == text
    assert len(list(landed.path.parent.glob("*.extract.md"))) == 1


def test_a_re_extraction_never_edits_the_first_one(vault, config):
    landed = drop(vault, config)
    first = imports.extract(vault, landed.path, when=datetime(2026, 9, 9, 10, 0))
    before = first.read_text(encoding="utf-8")
    second = imports.extract(vault, landed.path, when=datetime(2026, 9, 9, 11, 30))

    assert second != first
    assert first.read_text(encoding="utf-8") == before
    assert first.is_file() and second.is_file()


def test_every_sheet_of_a_wide_workbook_is_named_even_when_it_is_not_quoted(
    vault, config
):
    """**A fourteen sheet workbook where four sheets are listed is a file the
    operator will be wrong about.** Every sheet is named with its row count;
    only the contents are bounded, and the cut is stated."""
    sheets = {f"Sheet {n}": [["a", "b"], ["1", "2"]] for n in range(13)}
    landed = drop(vault, config, workbook(sheets=sheets))
    built = imports.build_extract(landed.path, max_sheets=3)

    assert "## 14 sheet(s)" in built.text
    for n in range(13):
        assert f"### Sheet {n} " in built.text, "every sheet is named"
    assert any("contents of" in item for item in built.cut)
    assert "What was left out" in built.text


def test_a_long_sheet_says_how_many_rows_it_did_not_quote(vault, config):
    rows = [["Period", "Gross Profit"]] + [[f"Jan 20{n:02d}", str(n)] for n in range(90)]
    landed = drop(vault, config, workbook(rows))
    built = imports.build_extract(landed.path, max_rows=10)

    assert any("rows of" in item for item in built.cut)
    assert "What was left out" in built.text


def test_a_file_that_is_not_a_spreadsheet_is_readable_context(vault, config):
    """**Failing to be a GP export is not a failure.** A dropped PDF or Word
    document has a sidecar and can be asked about, and no import happens."""
    docs = pytest.importorskip("docx")
    from ranger import documents

    payload = documents._docx_bytes(
        documents.Spec(title="Illes pricing", blocks=(documents.text("Held at 12%."),))
    )
    landed = imports.land(vault, config, "illes.docx", payload, today=TODAY)

    assert not landed.tabular
    text, _ = imports.sidecar_text(vault, landed.path)
    assert "Held at 12%." in text
    assert imports.tables_of(landed.path) == [], "nothing to import, and that is fine"


# -- recognising the shape --------------------------------------------------


def test_the_header_row_is_found_under_the_title_rows(vault, config):
    """NetSuite puts a company name, a report name and a date range above the
    headers, and every one of them is one cell wide."""
    landed = drop(vault, config)
    table = imports.tables_of(landed.path)[0]
    index = shapes.header_row(table.rows)

    # 3, not 4: a row with no cells in it is not written into the sheet XML at
    # all, so the blank line in the export above is not a row here. Row numbers
    # from this reader are positions in what was read, and the formula
    # positions are counted the same way, which is what makes them line up.
    assert index == 3
    assert table.header_at(index) == HEADERS


def test_the_fingerprint_is_the_headers_and_not_the_file_name(vault, config):
    """`GP Sep 2026.xlsx` is a different name every month and the same shape.
    A title row added above the headers is also the same shape."""
    plain = shapes.fingerprint(HEADERS)

    assert shapes.fingerprint([h.upper() for h in HEADERS]) == plain
    assert shapes.fingerprint(["  Period ", "Revenue", "COGS", "Gross Profit", "Margin %"]) == plain
    assert shapes.fingerprint(HEADERS[:-1]) != plain


def test_the_proposal_reads_the_headers(vault, config):
    proposal = shapes.propose(HEADERS)
    assert (proposal.period, proposal.amount) == ("Period", "Gross Profit")
    assert not proposal.ambiguous, "Margin % is a weaker hint than Gross Profit"


def test_two_columns_that_could_both_be_the_figure_are_a_question(vault, config):
    """**The instinct kept.** Picking one would be a number the operator never
    chose, so it asks, and it says which columns it is choosing between."""
    proposal = shapes.propose(["Month", "Amount", "Total", "Value"])

    assert proposal.period == "Month"
    assert proposal.amount == ""
    assert proposal.ambiguous
    assert set(proposal.amount_options) == {"Amount", "Total", "Value"}
    assert "more than one column could be the figure" in proposal.why()


def test_a_commission_column_is_recognised(vault, config):
    """The real dropped file was a commission statement, not a GP export."""
    proposal = shapes.propose(["Account", "Period", "Commission"])
    assert (proposal.period, proposal.amount) == ("Period", "Commission")


def test_a_shape_that_has_not_been_confirmed_is_not_remembered(vault, config):
    """It never guesses. A proposal is not a mapping until a person says so."""
    assert shapes.find(vault, config, HEADERS) is None


def test_a_confirmed_shape_is_recognised_next_time(vault, config):
    shape = shapes.Shape(
        fingerprint=shapes.fingerprint(HEADERS), name="netsuite gp.xlsx",
        headers=tuple(HEADERS), period_column="Period", amount_column="Gross Profit",
        confirmed="2026-09-09",
    )
    shapes.remember(vault, config, shape)

    found = shapes.find(vault, config, HEADERS)
    assert found is not None
    assert (found.period_column, found.amount_column) == ("Period", "Gross Profit")
    assert shapes.path_for(config).is_file()


def test_when_the_export_format_changes_the_shape_stops_matching(vault, config):
    """And that is correct behaviour, not an error. The message says so; this
    asserts the mechanism underneath it."""
    shapes.remember(vault, config, shapes.Shape(
        fingerprint=shapes.fingerprint(HEADERS), name="x", headers=tuple(HEADERS),
        period_column="Period", amount_column="Gross Profit",
    ))
    moved = ["Accounting Period", "Revenue", "COGS", "GP", "Margin %"]

    assert shapes.find(vault, config, moved) is None


def test_remembering_a_shape_again_appends_rather_than_rewrites(vault, config):
    first = shapes.Shape(fingerprint="aaa", name="one", headers=("A",),
                         period_column="A", amount_column="A")
    second = shapes.Shape(fingerprint="bbb", name="two", headers=("B",),
                          period_column="B", amount_column="B")
    shapes.remember(vault, config, first)
    shapes.remember(vault, config, second)

    text = shapes.path_for(config).read_text(encoding="utf-8")
    assert "## one" in text and "## two" in text
    assert len(shapes.load(vault, config)) == 2


# -- the numbers, and who is allowed to work them out -----------------------


def plan_for(vault, config, payload=None, rows=None):
    landed = drop(vault, config, payload if payload is not None else workbook(rows))
    table = imports.tables_of(landed.path)[0]
    index = shapes.header_row(table.rows)
    headers = [h.casefold() for h in table.header_at(index)]
    return landed, table, gp.plan_import(
        gp.ledger_for(config, vault), table.rows, header_index=index,
        period_column=headers.index("period"),
        amount_column=headers.index("gross profit"),
        formulas=table.formulas,
    )


def test_the_first_import_reads_every_month_and_skips_the_total(vault, config):
    _, _, plan = plan_for(vault, config)

    assert [(c.period, str(c.amount), c.verdict) for c in plan.changes] == [
        ("2026-06", "41000", "new"),
        ("2026-07", "45500", "new"),
        ("2026-08", "48250", "new"),
    ]
    assert plan.skipped == ["Total 573500"], "a totals row is not a month"
    assert plan.summary() == "3 months updated, 0 unchanged, 1 row skipped"


def test_a_re_import_writes_only_what_changed(vault, config):
    """**Same value is not a correction.** Twelve months dropped every month
    would otherwise write twelve superseding entries a month, and the folder
    would record how often a file was dropped rather than what was learned."""
    _, _, first = plan_for(vault, config)
    gp.apply_import(config, vault, first, source="netsuite gp.xlsx",
                    now=datetime(2026, 9, 9, 9, 0))

    changed = list(EXPORT) + [["Sep 2026", "210000", "158000", "52000", "24.8"]]
    changed[7] = ["Aug 2026", "201000", "152750", "47900", "23.8"]
    _, _, second = plan_for(vault, config, workbook(changed))

    verdicts = {c.period: c.verdict for c in second.changes}
    assert verdicts == {
        "2026-06": "unchanged", "2026-07": "unchanged",
        "2026-08": "corrects", "2026-09": "new",
    }
    assert second.summary() == "2 months updated, 2 unchanged, 1 row skipped"

    written = gp.apply_import(config, vault, second, source="netsuite gp (2).xlsx",
                              now=datetime(2026, 9, 9, 10, 0))
    assert len(written) == 2, "only what changed"

    ledger = gp.ledger_for(config, vault)
    assert ledger.current()["2026-08"].amount == Decimal("47900")
    assert ledger.corrections("2026-08") == 1
    assert len(list((config.vault.ranger / "gp").glob("*.md"))) == 5, "nothing overwritten"


def test_an_imported_figure_says_where_it_came_from(vault, config):
    _, _, plan = plan_for(vault, config)
    written = gp.apply_import(config, vault, plan, source="netsuite gp.xlsx")

    assert written[0].path.read_text(encoding="utf-8").endswith(
        "Imported from netsuite gp.xlsx.\n"
    )


def test_a_formula_column_is_refused_rather_than_read(vault, config):
    """A formula's cached value is whatever was true when the file was last
    opened by something that calculates. Importing that as gross profit is
    importing a guess."""
    payload = workbook(formula_at=(6, 4))  # the June gross profit cell
    _, _, plan = plan_for(vault, config, payload)

    assert plan.refused
    assert "values only" in plan.refused
    assert plan.writes == []


def test_two_rows_for_one_month_import_neither(vault, config):
    rows = list(EXPORT) + [["Aug 2026", "1", "2", "99999", "1"]]
    _, _, plan = plan_for(vault, config, workbook(rows))

    august = [c for c in plan.changes if c.period == "2026-08"]
    assert len(august) == 1 and str(august[0].amount) == "48250"
    assert any("a second row for 2026-08" in item for item in plan.skipped)


@pytest.mark.parametrize(
    "raw, expected",
    [("2026-08", "2026-08"), ("Aug 2026", "2026-08"), ("August 2026", "2026-08"),
     ("8/2026", "2026-08"), ("08/01/2026", "2026-08"), ("2026 Aug", "2026-08"),
     ("Q3", ""), ("Total", ""), ("FY2026", ""), ("", "")],
)
def test_periods_an_export_might_actually_use(raw, expected):
    assert imports.read_period(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [("$48,250.00", "48250.00"), ("(1,234.50)", "-1234.50"), ("48250", "48250")],
)
def test_figures_an_export_might_actually_use(raw, expected):
    assert imports.read_amount(raw) == Decimal(expected)


def test_a_cell_that_is_not_a_figure_is_not_a_zero(vault, config):
    assert imports.read_amount("n/a") is None
    assert imports.read_amount("") is None


# -- the seam ---------------------------------------------------------------


def test_no_part_of_the_import_path_can_reach_a_model():
    """**The point of the whole design.** Python reads the numbers; the model
    explains them. Walked rather than asserted in a docstring, the same way the
    dashlet seam is."""
    import ast

    ranger = Path(__file__).resolve().parent.parent / "ranger"
    seen: set[str] = set()
    queue = ["imports", "shapes"]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        source = ranger / f"{name}.py"
        if not source.is_file():
            continue
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level:
                queue.extend([node.module.split(".")[0]] if node.module
                             else [a.name for a in node.names])

    assert not (seen & {"provider", "core", "prompts", "assembly", "toolset"}), (
        f"the import path reaches {sorted(seen & {'provider', 'core', 'prompts'})}"
    )


# -- the browser's half ----------------------------------------------------


class Agent:
    registry = None

    def __init__(self, config, vault):
        self.config = config
        self.vault = vault


def session(config, vault):
    from ranger.bridge import Session

    sent: list[dict] = []
    return Session(agent=Agent(config, vault), send=sent.append), sent


def kinds(sent):
    return [event["kind"] for event in sent]


def messages(sent):
    return " ".join(str(event.get("message", "")) for event in sent)


def test_a_drop_of_an_unknown_shape_imports_nothing(vault, config):
    """**It never guesses.** An export nobody has mapped lands, is named, and
    waits. Nothing is written and nothing is asked of a model."""
    drop(vault, config)
    talk, sent = session(config, vault)

    talk._dropped({"name": "netsuite gp.xlsx"})

    assert "not been shown before" in messages(sent)
    assert "panel" in kinds(sent)
    assert gp.ledger_for(config, vault).entries == []


def test_a_drop_of_a_known_shape_imports_without_asking(vault, config):
    """**A known shape applies silently.** Being stopped every month to confirm
    the same mapping is how a card becomes a reflex."""
    shapes.remember(vault, config, shapes.Shape(
        fingerprint=shapes.fingerprint(HEADERS), name="netsuite gp.xlsx",
        headers=tuple(HEADERS), period_column="Period", amount_column="Gross Profit",
        confirmed="2026-09-09",
    ))
    drop(vault, config)
    talk, sent = session(config, vault)

    talk._dropped({"name": "netsuite gp.xlsx"})

    assert "3 months updated, 0 unchanged" in messages(sent)
    ledger = gp.ledger_for(config, vault)
    assert sorted(ledger.current()) == ["2026-06", "2026-07", "2026-08"]


def test_a_silent_import_still_leaves_a_line_in_the_inbox(vault, config):
    """The operator asked to know if an export moved five months without being
    stopped every time. This is the trace."""
    from ranger.heartbeat import Inbox

    shapes.remember(vault, config, shapes.Shape(
        fingerprint=shapes.fingerprint(HEADERS), name="x", headers=tuple(HEADERS),
        period_column="Period", amount_column="Gross Profit",
    ))
    drop(vault, config)
    talk, _ = session(config, vault)
    talk._dropped({"name": "netsuite gp.xlsx"})

    notices = Inbox(vault, config.vault.inbox).pending()
    assert len(notices) == 1
    assert "3 months updated" in notices[0].title
    assert "supersedes" in notices[0].body


def test_the_proposal_shows_what_it_would_write_before_writing_it(vault, config):
    """A confirmation that does not show the rows is a click, not a decision."""
    drop(vault, config)
    talk, sent = session(config, vault)

    talk._import_propose({"name": "netsuite gp.xlsx"})

    proposal = [e for e in sent if e["kind"] == "import_proposal"][0]
    assert proposal["known"] is False
    assert proposal["period"] == "Period" and proposal["amount"] == "Gross Profit"
    assert proposal["headers"] == HEADERS
    assert [c["period"] for c in proposal["changes"]] == ["2026-06", "2026-07", "2026-08"]
    assert proposal["summary"] == "3 months updated, 0 unchanged, 1 row skipped"
    assert gp.ledger_for(config, vault).entries == [], "proposing writes nothing"


def test_confirming_writes_the_figures_and_remembers_the_shape(vault, config):
    drop(vault, config)
    talk, sent = session(config, vault)

    talk._import_apply({
        "name": "netsuite gp.xlsx", "period": "Period", "amount": "Gross Profit",
    })

    assert "3 months updated" in messages(sent)
    assert len(gp.ledger_for(config, vault).entries) == 3
    remembered = shapes.find(vault, config, HEADERS)
    assert remembered is not None
    assert remembered.amount_column == "Gross Profit"


def test_confirming_columns_that_are_not_in_the_file_is_refused(vault, config):
    drop(vault, config)
    talk, sent = session(config, vault)

    talk._import_apply({"name": "netsuite gp.xlsx", "period": "Period", "amount": "Nope"})

    assert kinds(sent) == ["error"]
    assert gp.ledger_for(config, vault).entries == []
    assert shapes.find(vault, config, HEADERS) is None


def test_a_moved_column_asks_again_rather_than_failing(vault, config):
    """When NetSuite changes its export, the fingerprint stops matching. That
    is a mapping to confirm again, and the message says so."""
    shapes.remember(vault, config, shapes.Shape(
        fingerprint=shapes.fingerprint(HEADERS), name="x", headers=tuple(HEADERS),
        period_column="Period", amount_column="Gone",
    ))
    drop(vault, config)
    talk, sent = session(config, vault)

    talk._dropped({"name": "netsuite gp.xlsx"})

    assert "map it again" in messages(sent)
    assert gp.ledger_for(config, vault).entries == []


def test_a_dropped_file_that_is_not_tabular_does_nothing_on_drop(vault, config):
    docs = pytest.importorskip("docx")
    from ranger import documents

    payload = documents._docx_bytes(
        documents.Spec(title="Notes", blocks=(documents.text("hello"),))
    )
    imports.land(vault, config, "notes.docx", payload, today=TODAY)
    talk, sent = session(config, vault)

    talk._dropped({"name": "notes.docx"})

    assert kinds(sent) == ["panel"], "listed, and nothing else"


def test_the_panel_lists_dropped_files_with_their_date(vault, config):
    from ranger.panel import snapshot

    drop(vault, config)
    view = snapshot(config, vault)

    assert len(view["imports"]) == 1
    row = view["imports"][0]
    assert row["title"] == "netsuite gp.xlsx"
    assert row["when"] == "2026-09-09"
    assert row["kind"] == "import:table"
    assert "not read yet" in row["detail"]


# -- the route the file arrives on -----------------------------------------


@pytest.fixture
def served(config, vault):
    import threading
    from dataclasses import replace

    from ranger.server import build

    server = build(replace(config, server=replace(config.server, port=0)))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def post(port, payload, *, name="netsuite gp.xlsx", origin=None, path="/drop"):
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=payload, method="POST",
        headers={"X-Ranger-Filename": name, "Content-Type": "application/octet-stream",
                 **({"Origin": origin} if origin else {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, {}


def test_a_dropped_file_arrives_over_the_post_route(served, config, vault):
    status, result = post(served, workbook())

    assert status == 200 and result["ok"] is True
    assert result["name"] == "netsuite gp.xlsx"
    assert result["tabular"] is True
    assert "Excel workbook" in result["message"]
    landed = config.vault.ranger / "imports" / date.today().isoformat() / "netsuite gp.xlsx"
    assert landed.is_file()


def test_a_drop_from_another_page_is_refused(served):
    """A cross-origin POST needs no preflight, so any site the operator has
    open could push a file into their vault if this were not checked here."""
    status, result = post(served, workbook(), origin="https://not-jarvis.example")

    assert status == 403
    assert result["ok"] is False


def test_a_drop_of_something_it_does_not_take_is_refused_with_a_sentence(served):
    status, result = post(served, b"MZ nope", name="setup.exe")

    assert status == 400
    assert result["ok"] is False
    assert ".exe" in result["message"]


def test_a_drop_over_the_ceiling_is_refused_before_it_is_read(served, config):
    """Refused on the header, without reading the body.

    Sent as a raw request that announces a huge size and then sends nothing,
    which is the point: a 400MB drop must not be read into memory before being
    refused. A real browser is told the same thing and the page checks the size
    before it uploads, so this is the second line rather than the first.
    """
    import socket

    size = config.imports.max_bytes + 1024
    with socket.create_connection(("127.0.0.1", served), timeout=5) as client:
        client.sendall(
            b"POST /drop HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"X-Ranger-Filename: huge.csv\r\n"
            + f"Content-Length: {size}\r\n".encode()
            + b"\r\n"
        )
        reply = client.recv(4096).decode("utf-8", errors="replace")

    assert "413" in reply.splitlines()[0]
    assert "MB" in reply


def test_a_refusal_reads_the_body_before_it_answers():
    """The mechanism itself, without needing a Windows machine to fail on.

    Windows turns a close-with-unread-bytes into an RST, so the client loses a
    response that was already written. Linux does not, which is why the
    integration tests above passed here and the same code reported
    `WinError 10053` there. This asserts the thing that differs: the body is
    consumed before the answer goes out.
    """
    import io

    from ranger.server import FrontEndHandler

    handler = FrontEndHandler.__new__(FrontEndHandler)
    handler.headers = {"Content-Length": "1000"}
    handler.rfile = io.BytesIO(b"x" * 1000)

    handler._drain_body()

    assert handler.rfile.read() == b"", "the socket must not be left holding the body"


def test_draining_a_refused_body_is_bounded():
    """A refusal must not have to swallow a 400MB upload to be polite about it.
    That body is refused for its size, and the window checks the size before it
    uploads, which is where that message actually comes from."""
    import io

    from ranger.server import FrontEndHandler

    handler = FrontEndHandler.__new__(FrontEndHandler)
    handler.headers = {"Content-Length": str(400 * 1_048_576)}
    handler.rfile = io.BytesIO(b"x" * (FrontEndHandler.DRAIN_LIMIT + 4096))

    handler._drain_body()

    assert len(handler.rfile.read()) == 4096, "it stops at the limit"


def test_a_body_length_that_is_nonsense_does_not_hang_the_refusal():
    import io

    from ranger.server import FrontEndHandler

    handler = FrontEndHandler.__new__(FrontEndHandler)
    handler.headers = {"Content-Length": "not a number"}
    handler.rfile = io.BytesIO(b"x" * 10)

    handler._drain_body()  # returns rather than raising


def test_nothing_else_can_be_posted(served, config):
    """The route refuses a POST it does not own, **and says so readably**.

    The first Windows run of this got `WinError 10053` instead of a status:
    the server answered and closed with the request body still unread, and
    Windows turns that into an RST, so the client never saw the refusal it had
    already been sent. Linux sends a clean close and the same code looked fine.
    The body is drained before the answer now, so the refusal is legible on
    both.
    """
    status, result = post(served, b'{"drop": "this"}', path="/status")

    assert status == 404
    assert result["ok"] is False
    # And the refusal is a refusal, not just a status code.
    root = config.vault.ranger / "imports"
    assert not root.exists() or not any(root.rglob("*")), "nothing was written"


def test_a_refused_post_is_still_readable_when_it_carries_a_body(served):
    """The mechanism, isolated: a rejected POST with a body long enough to sit
    in the socket must still answer, and the answer must be readable after the
    whole body has been written."""
    status, result = post(served, b"x" * 200_000, path="/status")

    assert status == 404
    assert "nothing is posted there" in result["message"]


def test_a_drop_from_another_page_is_refused_readably_with_a_body(served, config):
    status, result = post(served, workbook(), origin="https://not-jarvis.example")

    assert status == 403
    assert result["ok"] is False
    root = config.vault.ranger / "imports"
    assert not root.exists() or not any(root.rglob("*"))


# -- what the file actually is ---------------------------------------------
#
# The export that started this was `APIMyCommissionStatementDetailRes....xls`,
# which is an HTML table with an Excel name, because that is what web systems
# serve. openpyxl reads none of those, so the extract came back empty and
# Jarvis described a file it had never read. An extension is a claim.


HTML_XLS = b"""<html><head><meta charset="utf-8"></head><body>
<table>
<tr><th>Account</th><th>Period</th><th>Commission</th></tr>
<tr><td>Illes Foods</td><td>Aug 2026</td><td>1,240.50</td></tr>
<tr><td>Rusty Supply</td><td>Aug 2026</td><td>980.00</td></tr>
</table></body></html>"""

XML_XLS = b"""<?xml version="1.0"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"
          xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
<Worksheet ss:Name="Commission">
<Table>
<Row><Cell><Data ss:Type="String">Account</Data></Cell>
     <Cell><Data ss:Type="String">Commission</Data></Cell></Row>
<Row><Cell><Data ss:Type="String">Illes Foods</Data></Cell>
     <Cell><Data ss:Type="Number">1240.5</Data></Cell></Row>
</Table></Worksheet></Workbook>"""


def legacy_xls() -> bytes:
    """A real old-format Excel file, written by something that is not xlrd."""
    xlwt = pytest.importorskip("xlwt", reason="writing a real .xls needs xlwt")
    import io

    book = xlwt.Workbook()
    sheet = book.add_sheet("Commission")
    for row_index, row in enumerate(
        [["Account", "Period", "Commission"],
         ["Illes Foods", "Aug 2026", 1240.5],
         ["Rusty Supply", "Aug 2026", 980]]
    ):
        for column_index, value in enumerate(row):
            sheet.write(row_index, column_index, value)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_a_web_export_named_xls_is_recognised_as_what_it_is(vault, config):
    """**The file that started this.** It is an HTML table."""
    from ranger import tabular

    assert tabular.sniff(HTML_XLS, "APIMyCommissionStatementDetailRes.xls") == "html-table"
    can = tabular.readability(HTML_XLS, "APIMyCommissionStatementDetailRes.xls")
    assert can.readable and can.tabular
    assert "HTML table" in can.why and ".xls" in can.why


def test_a_web_export_named_xls_can_actually_be_read(vault, config):
    landed = imports.land(
        vault, config, "APIMyCommission.xls", HTML_XLS, today=TODAY
    )

    assert landed.format == "html-table"
    assert landed.tabular is True
    table = imports.tables_of(landed.path)[0]
    assert table.rows[0] == ["Account", "Period", "Commission"]
    assert table.rows[1] == ["Illes Foods", "Aug 2026", "1,240.50"]


def test_the_drop_says_at_drop_time_what_it_can_read(vault, config):
    """Not later, and not vaguely. The acknowledgement carries it."""
    landed = imports.land(vault, config, "APIMyCommission.xls", HTML_XLS, today=TODAY)

    assert "HTML table" in landed.describe()
    assert "can read it" in landed.describe()


def test_a_real_old_format_excel_file_is_read(vault, config):
    pytest.importorskip("xlrd", reason="reading a real .xls needs xlrd")
    landed = imports.land(vault, config, "legacy.xls", legacy_xls(), today=TODAY)

    assert landed.format == "xls"
    table = imports.tables_of(landed.path)[0]
    assert table.rows[0] == ["Account", "Period", "Commission"]
    assert table.rows[1][2] == "1240.5", "a number, not a float with a tail"


def test_an_xml_spreadsheet_named_xls_is_read(vault, config):
    landed = imports.land(vault, config, "statement.xls", XML_XLS, today=TODAY)

    assert landed.format == "xml-spreadsheet"
    table = imports.tables_of(landed.path)[0]
    assert table.name == "Commission"
    assert table.rows[1] == ["Illes Foods", "1240.5"]


def test_an_xlsx_with_the_wrong_extension_is_still_read(vault, config):
    landed = imports.land(vault, config, "actually a workbook.xls", workbook(), today=TODAY)

    assert landed.format == "xlsx"
    table = imports.tables_of(landed.path)[0]
    assert table.header_at(shapes.header_row(table.rows)) == HEADERS


def test_an_old_word_document_is_refused_with_what_to_do(vault, config):
    """It cannot be read, so it does not land. Refusing with a next step beats
    accepting it and being vague about it later."""
    ole2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512
    with pytest.raises(imports.Refused) as caught:
        imports.land(vault, config, "notes.doc", ole2, today=TODAY)

    assert "save it as .docx" in str(caught.value).lower()
    assert not list((config.vault.ranger / "imports").rglob("*.doc"))


def test_a_file_nothing_can_read_does_not_land(vault, config):
    """**A file that lands and cannot be read is worse than a refused one**:
    everything said about it afterwards is invention, and the panel would list
    an import that can never be imported."""
    with pytest.raises(imports.Refused) as caught:
        imports.land(vault, config, "mystery.xls", b"\x01\x02\x03 not anything", today=TODAY)

    assert "could not tell what" in str(caught.value)
    assert imports.listing(vault, config) == []


def test_a_real_xls_without_a_reader_is_refused_and_says_how_to_fix_it(
    vault, config, monkeypatch
):
    """The machine with no xlrd. Refused, with the install line, rather than
    landed and unreadable."""
    from ranger import tabular

    monkeypatch.setattr(tabular, "have", lambda module: False)
    ole2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512

    with pytest.raises(imports.Refused) as caught:
        imports.land(vault, config, "legacy.xls", ole2, today=TODAY)

    assert "pip install xlrd" in str(caught.value)
    assert "save it as .xlsx" in str(caught.value)


def test_the_panel_offers_an_import_on_a_web_export(vault, config):
    """The row has to know it is tabular, and that is a question about the
    contents. Under the old rule this file listed as an import and had no
    import button, because `.xls` was not in the tabular list."""
    from ranger.panel import snapshot

    imports.land(vault, config, "APIMyCommission.xls", HTML_XLS, today=TODAY)
    row = snapshot(config, vault)["imports"][0]

    assert row["kind"] == "import:table"


def test_a_web_export_imports_gross_profit_like_any_other_table(vault, config):
    """The path that never fired: mapped, planned, written, all in Python."""
    landed = imports.land(vault, config, "commission.xls", HTML_XLS, today=TODAY)
    table = imports.tables_of(landed.path)[0]
    index = shapes.header_row(table.rows)

    proposal = shapes.propose(table.header_at(index))
    assert (proposal.period, proposal.amount) == ("Period", "Commission")

    lowered = [h.casefold() for h in table.header_at(index)]
    plan = gp.plan_import(
        gp.ledger_for(config, vault), table.rows, header_index=index,
        period_column=lowered.index("period"), amount_column=lowered.index("commission"),
        formulas=table.formulas,
    )
    assert [(c.period, str(c.amount)) for c in plan.changes] == [("2026-08", "1240.50")]
