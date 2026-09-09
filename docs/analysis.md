# Analysis: where Python ends and the model begins

The operator drops an export and asks a question of it. "Total commission by
account for August" is two jobs, and the whole design is about which is whose.

## The line

**Python: every number.** Sums, counts, averages, minimums, deltas, grouping,
ordering, month against month. If it is arithmetic, the model does not do it.

**The model: which analysis answers the question.** What the columns mean, what
the result implies, what looks wrong, which comparison is worth making. What it
never does is produce a figure — including in the sentence above the table.

So "total commission by account for August" is the model choosing *group by
Account, sum Commission, where Period is Aug 2026*, and Python producing every
figure in the result.

## How that is enforced, not just intended

A model writing `$291,546` into a summary paragraph is the failure. It looks
exactly as right as the rest of the answer, and it is the number that gets
repeated to a customer. Three mechanisms, because one is a hope:

**1. The tool has nowhere to put a number.** The `analyse` spec names a file,
two columns, an operation from a fixed enum, and optionally an exact value to
filter on. There is no expression field, no formula field, no value field. A
test asserts that every property in the schema is a string, an object or a
boolean — a numeric property would be a number arriving from a model.

**2. A title carrying a figure is refused.** The model supplies the caption in
the operator's words. If it contains a digit run that did not come from the
spec's own filter values, `analysis.run` raises and tells it to leave the
numbers to the table. The summary line above the table is written by Python
from the arithmetic it just did.

**3. Every figure Jarvis states is checked against what the tools returned.**
After each turn, `figures.unverified` looks for figure-shaped numbers in the
reply — money, thousands separators, two decimal places, percentages — and
compares them, normalised, against every tool result of that turn plus what the
operator themselves said. Anything left over is named in the panel as a warning
and written to the audit log.

It reports; it does not rewrite. A wrong number that has been pointed at is
recoverable, and silently editing what Jarvis said would hide that it happened.
The check is deliberately narrow: "three accounts" and "the 2026 export" are
not figures, and flagging them would train the operator to ignore it.

## Preview the artifact, restated for computed output

A table shown on screen was computed by Python, written to
`Ranger/analysis/<date>/<title> <time>.md`, and rendered back out of that file.

Two reasons. "Export this to Excel" becomes a format change of a file that has
already been seen, rather than a second computation that might disagree with
the first. And a figure quoted at four o'clock is still reproducible at six.

The report is create-only. The same question asked twice writes a second
report; a report is the record of what a figure was when it was quoted.

## The three design questions

### How a table gets requested

A tool call with a **constrained spec**, not free-form SQL and not a
calculation:

```json
{"file": "commission", "group_by": "Account", "value": "Commission",
 "operation": "sum",
 "where": {"column": "Period", "equals": "Aug 2026"},
 "compare_to": {"column": "Period", "equals": "Jul 2026"},
 "title": "Commission by account, August against July"}
```

Every field names something that exists in the file. Column names are resolved
against the header row and anything absent is refused with the real column list
— guessing at the nearest one is how the wrong column gets summed.

### 4,000 rows

The arithmetic runs over **every** row. The rendering is capped at
`analysis.max_rows` (200) and the sheet says *"showing 200 of 4,000 rows"*, the
same rule the document preview keeps. The written file holds the complete
result, and the export is built from the file, so exporting a truncated view
still gives all of it.

Grouping that would produce more than 5,000 groups is refused: that is a list
rather than an answer, and the message says to group by something coarser.

### One table or a report

The sheet renders **a report**: title, the Python-written summary line, the
table, and a "where this came from" section. One `analyse` call writes one
report, and a comparison is columns in the same table rather than a second one
— `Commission`, `Commission (Jul 2026)`, `Change`. The body scrolls, so a
report growing more sections later is a change to the file format and not to
the surface.

## What the table shows

- **Column headers as they are in the source.** A column renamed on the way to
  the screen is a column the operator cannot find again in their own export.
- **Row count, and what was left out**, when the render is capped.
- **Which file and which sheet**, on screen at all times.
- **The mapping used**, plus the filters, the rows read, and anything excluded.

One exclusion is worth knowing about: a row whose group label is `Total`,
`Subtotal` or `Grand total` is the file's own arithmetic, and grouping it
beside the rows it totals doubles the answer. Those rows are left out **and
named**, in the summary and in the provenance, because silently dropping a row
is its own hazard.

## Untrusted content

Cell values reaching the sheet are data. A cell containing markup or an
instruction renders as text, is grouped like any other label, and changes
nothing about the arithmetic. When a cell's text reaches the model — through
`read_import`, or in the rows the `analyse` tool hands back — it is fenced like
everything else that came out of a file.
