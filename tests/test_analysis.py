"""Analysis of a dropped file, and the line between Python and the model.

The line: **Python computes every number; the model chooses which numbers.**
Which grouping answers the question, what the columns mean, what the result
implies -- judgement, and the model's. Sums, counts, averages, deltas,
ordering -- arithmetic, and never the model's.

Three things enforce that, and this file tests all three, because a rule that
is only in the prompt is a rule that holds until the day it doesn't:

1. **The tool has nowhere to put a number.** A spec names a file, two columns
   and an operation from a fixed list. There is no expression field, no
   formula, no value.
2. **A title carrying a figure is refused.** The failure is a model writing
   "$291,546" into a sentence above a table: it looks exactly as right as the
   rest, and it is the number that gets repeated to a customer.
3. **Every figure Jarvis states is checked against what the tools returned.**
   Anything left over is named in the panel and written to the log.

And the artifact rule, restated for computed output: a table shown to the
operator was written to a file first, and the sheet renders that file. So an
export is a format change of the thing they were looking at rather than a
second computation that might disagree, and a figure quoted at four o'clock is
reproducible at six.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ranger import analysis, imports

TODAY = date(2026, 9, 9)
NOW = datetime(2026, 9, 9, 10, 0)

COMMISSION = [
    ["Account", "Period", "Commission"],
    ["Illes Foods", "Jul 2026", "1100.00"],
    ["Illes Foods", "Aug 2026", "1240.50"],
    ["Rusty Supply", "Jul 2026", "1010.00"],
    ["Rusty Supply", "Aug 2026", "980.00"],
    ["Telly Packaging", "Jul 2026", "400.00"],
    ["Telly Packaging", "Aug 2026", "1310.25"],
    ["Total", "", "6040.75"],
]


def table(rows=None):
    from ranger.tabular import Table

    return Table(name="Commission", rows=[list(row) for row in (rows or COMMISSION)])


def spec(**overrides) -> analysis.Spec:
    base = dict(file="commission.xls", group_by="Account", value="Commission",
                operation="sum", title="Commission by account")
    base.update(overrides)
    return analysis.Spec(**base)


def run(**overrides) -> analysis.Result:
    rows = overrides.pop("rows", None)
    return analysis.run(table(rows), spec(**overrides), source="commission.xls", now=NOW)


# -- Python does the arithmetic --------------------------------------------


def test_a_sum_by_group_is_computed_over_every_row():
    result = run()

    assert [(row.label, str(row.value)) for row in result.rows] == [
        ("Illes Foods", "2340.50"),
        ("Rusty Supply", "1990.00"),
        ("Telly Packaging", "1710.25"),
    ], "biggest first, because that is the order the question is usually asked in"
    assert result.total == Decimal("6040.75")
    assert result.excluded == ["Total"], (
        "the file's own totals row, grouped beside the rows it totals, would have "
        "doubled the answer -- and a doubled figure looks entirely normal"
    )
    assert "1 totals row(s) left out ('Total')" in result.summary()


def test_a_filter_is_an_exact_match_on_a_column():
    result = run(where=analysis.Filter("Period", "Aug 2026"))

    assert [row.label for row in result.rows] == [
        "Telly Packaging", "Illes Foods", "Rusty Supply",
    ]
    assert result.total == Decimal("3530.75")


def test_a_comparison_gives_a_change_column_python_worked_out():
    result = run(
        where=analysis.Filter("Period", "Aug 2026"),
        compare_to=analysis.Filter("Period", "Jul 2026"),
    )
    changes = {row.label: (str(row.value), str(row.compared), str(row.delta))
               for row in result.rows}

    assert changes["Illes Foods"] == ("1240.50", "1100.00", "140.50")
    assert changes["Rusty Supply"] == ("980.00", "1010.00", "-30.00")
    assert changes["Telly Packaging"] == ("1310.25", "400.00", "910.25")


def test_something_that_only_appears_in_the_comparison_is_still_an_answer():
    """An account that billed in July and nothing in August went to zero, and
    a table that dropped the row would be hiding the interesting one."""
    rows = COMMISSION + [["Wexxar", "Jul 2026", "500.00"]]
    result = run(rows=rows, where=analysis.Filter("Period", "Aug 2026"),
                 compare_to=analysis.Filter("Period", "Jul 2026"))
    wexxar = {row.label: row for row in result.rows}["Wexxar"]

    assert wexxar.value == Decimal(0)
    assert wexxar.compared == Decimal("500.00")
    assert wexxar.delta == Decimal("-500.00")


@pytest.mark.parametrize(
    "operation, expected",
    [("count", "2"), ("average", "1170.25"), ("min", "1100.00"), ("max", "1240.50")],
)
def test_the_operations_are_functions_here_and_not_expressions(operation, expected):
    result = run(operation=operation, where=None)
    illes = {row.label: row for row in result.rows}["Illes Foods"]

    assert str(illes.value) == expected


def test_an_operation_it_does_not_have_is_refused_by_name():
    with pytest.raises(analysis.AnalysisError) as caught:
        run(operation="regress")
    assert "sum" in str(caught.value) and "regress" in str(caught.value)


def test_a_column_that_is_not_in_the_file_is_refused_rather_than_guessed():
    """Guessing at the nearest column is how the wrong column gets summed."""
    with pytest.raises(analysis.AnalysisError) as caught:
        run(value="Gross Profit")

    assert "no column called 'Gross Profit'" in str(caught.value)
    assert "Account, Period, Commission" in str(caught.value)


def test_grouping_that_produces_a_list_rather_than_an_answer_is_refused():
    rows = [["Reference", "Amount"]] + [[f"REF{n}", "1"] for n in range(analysis.MAX_GROUPS + 5)]
    with pytest.raises(analysis.AnalysisError) as caught:
        run(rows=rows, group_by="Reference", value="Amount")

    assert "coarser" in str(caught.value)


def test_accounting_parentheses_are_negative():
    rows = [["Account", "Amount"], ["Illes Foods", "(1,234.50)"]]
    result = run(rows=rows, value="Amount", where=None)

    assert result.rows[0].value == Decimal("-1234.50")


def test_the_summary_line_is_written_by_python():
    """The sentence above the table is the one place an invented figure would
    look most convincing, so it is not asked for -- it is computed."""
    result = run(where=analysis.Filter("Period", "Aug 2026"))
    summary = result.summary()

    assert "3,530.75" in summary
    assert "3 Account(s)" in summary
    assert "where Period is 'Aug 2026'" in summary


# -- the model cannot state a figure ---------------------------------------


def test_a_title_carrying_a_figure_is_refused(vault, config):
    """**The failure this exists for.** A model writing "$291,546" into a
    sentence above a table: it looks right, nobody computed it, and it is the
    number that gets repeated to a customer."""
    with pytest.raises(analysis.AnalysisError) as caught:
        run(title="Commission was $291,546 in August")

    assert "291,546" in str(caught.value)
    assert "did not compute" in str(caught.value)


def test_a_title_may_carry_a_value_that_came_out_of_the_file():
    """'August 2026' is not an invented figure: it is the filter, which came
    from a column. Refusing it would make the guard something to work around."""
    result = run(
        where=analysis.Filter("Period", "Aug 2026"),
        title="Commission by account for Aug 2026",
    )
    assert result.rows


def test_the_tool_schema_has_nowhere_to_put_a_number(config, vault):
    """Not a rule about what the model should send: a shape with no field for
    it. Every property names a column, a file, or an operation."""
    from ranger.toolset import build_registry

    tool = [t for t in build_registry(config, vault) if t.name == "analyse"][0]
    properties = tool.input_schema["properties"]

    assert set(properties) == {
        "file", "sheet", "group_by", "value", "operation", "where", "compare_to",
        "sort", "descending", "title",
    }
    for name, shape in properties.items():
        assert shape["type"] in ("string", "object", "boolean"), (
            f"{name} takes a {shape['type']}, which is a number arriving from a model"
        )
    assert properties["operation"]["enum"] == list(analysis.OPERATIONS)


# -- the artifact ----------------------------------------------------------


def test_the_table_is_written_before_it_is_shown(vault, config):
    result = run()
    written = analysis.write(vault, config, result, today=TODAY)

    assert written.parent == config.vault.ranger / "analysis" / "2026-09-09"
    assert written.is_file()
    assert "Every figure below was computed in Python" in written.read_text(encoding="utf-8")


def test_the_same_question_twice_writes_a_second_report(vault, config):
    """Create-only. A report is a record of what a figure was when it was
    quoted, so it is never edited."""
    result = run()
    first = analysis.write(vault, config, result, today=TODAY)
    second = analysis.write(vault, config, result, today=TODAY)

    assert first != second
    assert first.is_file() and second.is_file()


def test_the_sheet_renders_the_file_and_not_the_computation(vault, config):
    """Preview the artifact, restated for computed output: what is on screen is
    parsed back out of the file that was written."""
    written = analysis.write(vault, config, run(), today=TODAY)
    report = analysis.read_report(written, config.vault.root)

    assert report.title == "Commission by account"
    assert report.columns == ["Account", "Commission", "Rows"]
    assert report.rows[0] == ["Illes Foods", "2,340.50", "2"]
    assert "totals row(s) left out" in report.summary
    assert "6,040.75" in report.summary
    assert report.source == "commission.xls"


def test_the_headers_are_the_source_headers(vault, config):
    """A column renamed on the way to the screen is a column the operator
    cannot find again in their own export."""
    written = analysis.write(vault, config, run(
        where=analysis.Filter("Period", "Aug 2026"),
        compare_to=analysis.Filter("Period", "Jul 2026"),
    ), today=TODAY)
    report = analysis.read_report(written, config.vault.root)

    assert report.columns[0] == "Account"
    assert report.columns[1] == "Commission"
    assert report.columns[2] == "Commission (Jul 2026)"


def test_the_report_says_which_file_and_which_sheet_it_came_from(vault, config):
    written = analysis.write(vault, config, run(
        where=analysis.Filter("Period", "Aug 2026")
    ), today=TODAY)
    report = analysis.read_report(written, config.vault.root)
    provenance = " ".join(report.provenance)

    assert "file: commission.xls" in provenance
    assert "sheet: Commission" in provenance
    assert "columns used: Account / Commission" in provenance
    assert "filtered where: Period is 'Aug 2026'" in provenance
    assert "rows read" in provenance


def test_four_thousand_rows_are_all_computed_and_not_all_drawn(vault, config):
    """The arithmetic runs over every row; the rendering is capped and says how
    many it is not showing, the same rule the document preview keeps."""
    rows = [["Account", "Amount"]] + [[f"Account {n}", "10"] for n in range(4000)]
    result = run(rows=rows, group_by="Account", value="Amount")

    assert len(result.rows) == 4000
    assert result.total == Decimal("40000")

    written = analysis.write(vault, config, result, today=TODAY)
    report = analysis.read_report(written, config.vault.root, max_rows=200)

    assert len(report.rows) == 200
    assert report.total_rows == 4000
    assert report.truncated
    # And the file itself holds all of them, because the export is that file.
    assert written.read_text(encoding="utf-8").count("\n| Account ") >= 4000


def test_the_export_is_the_table_that_was_on_screen(vault, config):
    """Not a second computation. The document is built from the written report,
    so it cannot disagree with what the operator was looking at."""
    from ranger import documents
    from ranger.preview import preview

    written = analysis.write(vault, config, run(), today=TODAY)
    report = analysis.read_report(written, config.vault.root)
    made = documents.generate(
        vault, config.vault.drafts, analysis.to_document(report), "xlsx", today=TODAY
    )

    sheet = [row.cells for row in preview(made.path).sheets[1].rows]
    assert list(sheet[0]) == report.columns
    assert [list(row) for row in sheet[1:]] == report.rows


# -- what Jarvis says about it ---------------------------------------------


def test_a_figure_nothing_computed_is_caught():
    from ranger.figures import unverified

    tool = "| Illes Foods | 1,240.50 |\n| Rusty Supply | 980.00 |\ntotalling 2,220.50"
    found = unverified("August came to $291,546 across the two accounts.", [tool])

    assert [item.text for item in found] == ["$291,546"]


def test_restating_a_computed_figure_is_not_caught():
    """The check has to be quiet when Jarvis is right, or it will be ignored."""
    from ranger.figures import unverified

    tool = "| Illes Foods | 1,240.50 |\ntotalling 2,220.50"
    assert unverified("Illes was $1,240.50, so the total is 2,220.50.", [tool]) == []
    assert unverified("Illes was 1240.50.", [tool]) == [], "same figure, written differently"


def test_a_figure_the_operator_said_is_not_one_jarvis_invented():
    from ranger.figures import unverified

    assert unverified("Yes, £1,240.50 is what the file says.", ["", "is £1,240.50 right?"]) == []


def test_ordinary_numbers_are_not_flagged():
    """Flagging 'three accounts' or a year would train the operator to ignore
    this, which is worse than not having it."""
    from ranger.figures import unverified

    assert unverified("Three accounts, and the 2026 export has 12 rows.", [""]) == []


async def test_the_panel_is_told_when_a_figure_came_from_nowhere(config, vault):
    """The third thing that enforces the rule, at the end of a real turn."""
    from ranger.bridge import Session
    from ranger.core import Ranger
    from ranger.testing import ScriptedProvider
    from ranger.toolset import build_registry

    sent: list[dict] = []
    agent = Ranger(
        config=config,
        provider=ScriptedProvider([{"text": "August commission came to $291,546."}]),
        registry=build_registry(config, vault),
        vault=vault,
    )
    session = Session(agent=agent, send=sent.append)
    await session._run("how did August look")

    warnings = [e for e in sent if e["kind"] == "notice" and e.get("level") == "warn"]
    assert warnings, "an unverified figure has to be visible"
    assert "$291,546" in warnings[0]["message"]
    assert "no tool computed" in warnings[0]["message"]


async def test_a_turn_with_no_invented_figures_says_nothing(config, vault):
    from ranger.bridge import Session
    from ranger.core import Ranger
    from ranger.testing import ScriptedProvider
    from ranger.toolset import build_registry

    sent: list[dict] = []
    agent = Ranger(
        config=config,
        provider=ScriptedProvider([{"text": "Three accounts billed in August."}]),
        registry=build_registry(config, vault),
        vault=vault,
    )
    await Session(agent=agent, send=sent.append)._run("how did August look")

    assert not [e for e in sent if e["kind"] == "notice" and e.get("level") == "warn"]


# -- through the tool, end to end ------------------------------------------


HTML_XLS = (
    b"<html><body><table>"
    b"<tr><th>Account</th><th>Period</th><th>Commission</th></tr>"
    b"<tr><td>Illes Foods</td><td>Aug 2026</td><td>1,240.50</td></tr>"
    b"<tr><td>Rusty Supply</td><td>Aug 2026</td><td>980.00</td></tr>"
    b"</table></body></html>"
)


async def test_the_tool_computes_writes_and_hands_back_python_figures(vault, config):
    from ranger.toolset import build_registry

    imports.land(vault, config, "commission.xls", HTML_XLS, today=TODAY)
    registry = build_registry(config, vault, today=lambda: TODAY)
    result = await registry.run("analyse", {
        "file": "commission", "group_by": "Account", "value": "Commission",
        "operation": "sum", "title": "Commission by account",
    })

    assert result.ok
    assert "1,240.50" in result.content and "980.00" in result.content
    assert "Do not restate a number that is not in it" in result.content
    written = list((config.vault.ranger / "analysis").rglob("*.md"))
    assert len(written) == 1, "the table exists on disk before it is shown"


async def test_the_tool_refuses_a_file_with_no_table_in_it(vault, config):
    from ranger.toolset import build_registry

    imports.land(vault, config, "notes.txt", b"just a note about pricing\n", today=TODAY)
    registry = build_registry(config, vault, today=lambda: TODAY)
    result = await registry.run("analyse", {"file": "notes", "group_by": "a", "value": "b"})

    assert not result.ok
    assert "no table in it" in result.content
    assert "read_import" in result.content, "and the thing that does work is named"


async def test_the_bridge_shows_the_written_report(vault, config):
    """Same sheet as a document preview: one surface, two sources."""
    from ranger.bridge import Session

    imports.land(vault, config, "commission.xls", HTML_XLS, today=TODAY)
    written = analysis.write(
        vault, config,
        analysis.run(table(), spec(), source="commission.xls", now=NOW),
        today=TODAY,
    )

    class Agent:
        registry = None

    agent = Agent()
    agent.config, agent.vault = config, vault
    sent: list[dict] = []
    session = Session(agent=agent, send=sent.append)
    session._send_analysis(written)

    event = [e for e in sent if e["kind"] == "analysis"][0]
    assert event["columns"] == ["Account", "Commission", "Rows"]
    assert event["source"] == "commission.xls"
    assert event["relative"].startswith("Ranger/analysis/")


async def test_exporting_from_the_sheet_builds_from_the_file(vault, config):
    from ranger.bridge import Session

    written = analysis.write(
        vault, config,
        analysis.run(table(), spec(), source="commission.xls", now=NOW),
        today=TODAY,
    )
    relative = written.relative_to(config.vault.root).as_posix()

    class Agent:
        registry = None

    agent = Agent()
    agent.config, agent.vault = config, vault
    sent: list[dict] = []
    Session(agent=agent, send=sent.append)._analysis_export(
        {"relative": relative, "format": "xlsx"}
    )

    assert any("exported to Ranger/drafts/" in str(e.get("message", "")) for e in sent)
    assert [e for e in sent if e["kind"] == "document"], "and it opens what it made"


async def test_nothing_but_an_analysis_can_be_exported(vault, config):
    from ranger.bridge import Session

    class Agent:
        registry = None

    agent = Agent()
    agent.config, agent.vault = config, vault
    sent: list[dict] = []
    Session(agent=agent, send=sent.append)._analysis_export(
        {"relative": "Ranger/memory/facts.md", "format": "xlsx"}
    )

    assert [e["kind"] for e in sent] == ["error"]


def test_analysis_is_in_the_snapshot_allow_list():
    from ranger.snapshot import INCLUDED, ignore_file

    assert "Ranger/analysis/" in INCLUDED
    assert "!/Ranger/analysis/" in ignore_file()


# -- the doubled total, which happened on a real file ----------------------
#
# The sheet reported 23,944.40 for "All rows" on a commission statement whose
# own Total row is 11,972.20. Exactly double: the file's totals row was summed
# alongside the rows it totals. It rendered as COMPUTED, with full provenance,
# which is the worst version of this -- a wrong figure carrying authority.
#
# The exclusion existed and never ran. It was written as
# `if group_at >= 0 and TOTAL_LABELS.match(label)`, so a question with **no
# grouping** -- one figure for the whole file, which is what was asked -- never
# checked at all. The fixture that found the hazard grouped by account, so the
# suite agreed with itself.

STATEMENT = [
    ["Account", "Period", "Commission"],
    ["Illes Foods", "Aug 2026", "5,120.40"],
    ["Rusty Supply", "Aug 2026", "3,880.00"],
    ["Telly Packaging", "Aug 2026", "2,971.80"],
    ["Total", "", "11,972.20"],
]


def test_a_total_row_is_excluded_when_nothing_is_grouped():
    """**The real one.** No group_by, so there was no column to look in."""
    result = analysis.run(
        table(STATEMENT),
        analysis.Spec(file="statement.xls", value="Commission", title="Total commission"),
        source="statement.xls", now=NOW,
    )

    assert result.total == Decimal("11972.20"), "not 23,944.40"
    assert result.excluded == ["Total"]
    assert "1 totals row(s) left out ('Total')" in result.summary()


@pytest.mark.parametrize(
    "label",
    ["Total", "TOTAL", "total", "Totals", "Grand Total", "Subtotal", "Sub-Total",
     "Total:", "Statement Total", "Report total", "Total for period", "Sum"],
)
def test_the_labels_a_statement_might_actually_use(label):
    rows = [["Account", "Commission"], ["Illes Foods", "100.00"], [label, "100.00"]]
    result = analysis.run(
        table(rows), analysis.Spec(file="x", value="Commission", title="t"),
        source="x", now=NOW,
    )

    assert result.excluded == [label]
    assert result.total == Decimal("100.00")


@pytest.mark.parametrize("label", ["Total Packaging Ltd", "Sumitomo", "Subtotal Systems Inc"])
def test_a_company_whose_name_starts_with_a_total_word_is_not_dropped(label):
    """Widened, not loosened. A customer called Sumitomo is data."""
    rows = [["Account", "Commission"], [label, "100.00"], ["Illes Foods", "50.00"]]
    result = analysis.run(
        table(rows), analysis.Spec(file="x", group_by="Account", value="Commission",
                                   title="t"),
        source="x", now=NOW,
    )

    assert result.excluded == []
    assert result.total == Decimal("150.00")


def test_a_totals_row_with_no_label_at_all_is_flagged_rather_than_dropped():
    """**Not a guess.** If a row might be the file's own total and nothing says
    so, it is included and said out loud, because dropping data on a hunch is
    its own hazard."""
    rows = [
        ["Account", "Commission"],
        ["Illes Foods", "5,120.40"],
        ["Rusty Supply", "3,880.00"],
        ["Telly Packaging", "2,971.80"],
        ["", "11,972.20"],
    ]
    result = analysis.run(
        table(rows), analysis.Spec(file="x", value="Commission", title="t"),
        source="x", now=NOW,
    )

    assert result.total == Decimal("23944.40"), "included, because nothing proved it"
    assert result.doubling
    assert "11,972.20" in result.doubling
    assert "exactly the sum of every other row" in result.doubling
    assert "line 5" in result.doubling, "and it says which line to go and look at"
    assert "might be the file's own total and were INCLUDED" in result.summary()


def test_a_genuine_row_that_equals_the_rest_is_flagged_and_kept():
    """The rule is a flag, not a refusal. One account really can equal the
    others, and dropping it would be the same failure the other way up."""
    rows = [["Account", "Commission"], ["Big One", "100.00"],
            ["A", "50.00"], ["B", "50.00"]]
    result = analysis.run(
        table(rows), analysis.Spec(file="x", group_by="Account", value="Commission",
                                   title="t"),
        source="x", now=NOW,
    )

    assert result.total == Decimal("200.00")
    assert [row.label for row in result.rows] == ["Big One", "A", "B"]
    assert "Big One" in result.doubling


def test_the_report_always_says_what_it_found_about_totals_rows(vault, config):
    """*"rows read: 4, groups out: 1"* said nothing about the one thing that had
    gone wrong. Silence about a totals row is not the same as there not being
    one, so the line is written whichever way it came out."""
    clean = analysis.run(
        table(), spec(), source="commission.xls", now=NOW,
    )
    written = analysis.write(vault, config, clean, today=TODAY)
    provenance = " ".join(analysis.read_report(written, config.vault.root).provenance)
    assert "totals rows: 1 totals row(s) left out" in provenance

    nothing = analysis.run(
        table([["Account", "Commission"], ["Illes Foods", "10.00"]]),
        analysis.Spec(file="x", group_by="Account", value="Commission", title="t"),
        source="x", now=NOW,
    )
    written = analysis.write(vault, config, nothing, today=TODAY)
    provenance = " ".join(analysis.read_report(written, config.vault.root).provenance)
    assert "totals rows: no totals row found in the source" in provenance


def test_the_doubling_flag_reaches_the_written_report(vault, config):
    rows = [["Account", "Commission"], ["Illes Foods", "100.00"],
            ["Rusty Supply", "50.00"], ["", "150.00"]]
    result = analysis.run(
        table(rows), analysis.Spec(file="x", value="Commission", title="t"),
        source="x", now=NOW,
    )
    written = analysis.write(vault, config, result, today=TODAY)
    report = analysis.read_report(written, config.vault.root)

    assert any("CHECK:" in line for line in report.provenance)
    assert "might be the file's own total" in report.summary


def test_a_totals_row_is_excluded_whichever_column_carries_the_label():
    """A statement puts the marker wherever it has room."""
    rows = [
        ["Ref", "Account", "Commission"],
        ["001", "Illes Foods", "100.00"],
        ["", "Total", "100.00"],
    ]
    result = analysis.run(
        table(rows), analysis.Spec(file="x", group_by="Account", value="Commission",
                                   title="t"),
        source="x", now=NOW,
    )

    assert result.excluded == ["Total"]
    assert result.total == Decimal("100.00")


def test_a_figure_column_holding_the_word_total_is_not_a_label():
    """The value column is skipped when looking for the marker: a column called
    Total is a perfectly ordinary column of figures."""
    rows = [["Account", "Total"], ["Illes Foods", "100.00"], ["Rusty Supply", "50.00"]]
    result = analysis.run(
        table(rows), analysis.Spec(file="x", group_by="Account", value="Total", title="t"),
        source="x", now=NOW,
    )

    assert result.excluded == []
    assert result.total == Decimal("150.00")
