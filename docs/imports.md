# Dropped files

Drag a file onto the window. It lands, Jarvis says what it got, and nothing
else happens.

Everything below follows from one constraint: **the dropped file should almost
never reach the model.** A 340 KB spreadsheet is 340 KB of context every time
it is mentioned, and gross profit read by a language model is a number that can
be subtly, confidently wrong about which column it came from.

## 1. Landing

`Ranger/imports/<YYYY-MM-DD>/<original name>`, create-only. A dated snapshot,
never a mirror: the folder is what was dropped on the day it was dropped, and
nothing goes back to keep it in step with a file that has since changed.

**No parse on drop.** A file dropped by accident costs nothing: no extract, no
tokens, no mapping learned. What happens is a line saying `netsuite gp.xlsx,
6 KB, Excel workbook`.

The one exception is cheap and local: if the file is a spreadsheet, Python
reads its header row to see whether it is a shape the operator has already
mapped. That is one row, in Python, and it is what makes a known export import
without being asked about (§4).

`Ranger/imports/` is in the snapshot allow list. These files hold customer
pricing and margin; they stay local, and they are exactly the kind of thing
that is gone for good when the export leaves someone's downloads folder.

## What a dropped file actually is

**An extension is a claim, and the claim is often wrong.** The export that
prompted this was `APIMyCommissionStatementDetailRes....xls` from a web system,
which is an HTML table with an Excel name — because Excel opens those. openpyxl
reads none of them, so the extract came back empty and Jarvis described a file
it had never read.

Every drop is sniffed before it lands, and what it turns out to be decides the
reader:

| Inside | Read with |
| ------ | --------- |
| a zip with `xl/workbook.xml` | the stdlib OOXML reader |
| an OLE2 container | xlrd, for real old-format `.xls` |
| `<html>` / `<table>` | the stdlib HTML parser |
| `<?xml>` + `<Workbook>` | XML Spreadsheet 2003 |
| delimited text | `csv` |

**A file that cannot be read does not land.** It is refused at the drop, with
what would fix it: install `xlrd`, or save it as `.xlsx`, or open the `.doc` in
Word and save it as `.docx`. Landing it and being vague about it later is worse
— everything said about it afterwards is invention, and the panel would list an
import that can never be imported.

The acknowledgement says which of these happened at the moment the file lands:
*"APIMyCommission.xls, 264 bytes. A web export: an HTML table saved with an
.xls name. Jarvis can read it."* `ranger doctor` says whether any reader is
missing.

## 2. The sidecar

The first time anything is asked about a dropped file, Python writes
`<name>.extract.md` beside it: sheet names, headers, row counts, a bounded text
skeleton, and a list of what it left out. Every later question reads the
sidecar. A 340 KB workbook becomes about 2 KB of context once, and re-reading
is free.

The sidecar is create-only. A re-extraction writes a new one beside the old,
because what was understood on the day is part of the record and a note that
refers to an extract should not find it has quietly changed.

`read_import` is the only tool that touches any of this, and what it returns is
fenced.

## 3. Gross profit never passes through the model

Once a column mapping exists, Python reads the figures straight out of the file
and writes GP entries. Zero tokens, and no path where a model decides column F
looks like gross profit. The model explains the number when asked; it never
produces one.

A test walks the import path's whole import graph and fails if it can reach a
provider.

## 4. One mapping per shape, keyed on the headers

The first NetSuite drop proposes a mapping — *period is `Period`, gross profit
is `Gross Profit`* — from the **header text** and nothing else. The operator
confirms it at the keyboard, seeing the rows it would write, and it is saved to
`Ranger/memory/import-shapes.md` against a fingerprint of the header row.

The fingerprint is a hash of the headers, normalised. Not the file name, which
is different every month, and not the row position, because an export that
gains a title row is the same shape.

- **Headers match a known shape** → it imports, silently, and writes one line
  to `Ranger/inbox` saying what changed. Being stopped every month to confirm
  the same mapping is how a card becomes a reflex.
- **Headers do not match** → it says so and waits. It never guesses.

When NetSuite changes its export format the fingerprint stops matching and the
operator is asked to map it again. **That is correct, and the message says so**
rather than reading like an error.

**Where two columns could both be the figure, it asks.** One strong hint wins
(`Gross Profit`, `Commission`); two strong hints is a question, not a coin
toss, and the card says which columns it is choosing between rather than
preselecting one. A weak hint (`Amount`, `Total`, `Value`) only gets a say when
nothing strong matched.

Nothing in a spreadsheet cell can influence a mapping. The proposal is a
regular expression over the header row; the decision is a person. A cell
reading "the gross profit column is Margin %" is a string in a spreadsheet, and
there is a test that plants exactly that.

## 5. A re-import writes only what changed

The export carries twelve months. Eleven match what is already recorded, so one
entry is written.

**Same value is not a correction.** Otherwise the folder becomes a log of how
often a file was dropped rather than a record of what was learned. A *different*
value for a month that already has one **is** a correction: it supersedes, per
the existing GP rules, and the entry it corrects stays on disk.

What comes back is `2 months updated, 2 unchanged, 1 row skipped`. Skipped rows
are named — a totals line, a period nobody can parse — rather than silently
dropped.

Two things are refused rather than guessed at:

- **A column of formulas.** A formula's cached value is whatever was true when
  the file was last opened by something that calculates. Importing that as
  gross profit is importing a guess, so the import stops and says to export
  values.
- **Two rows for one month in a single file.** Nothing here can tell which was
  meant, so neither is imported and both are reported.

## 6. A file that is not a GP export is just context

Sidecar, readable, no import, no error. Failing to be a GP export is not a
failure, and the panel row for a PDF has no import button on it, because a
button that would do nothing is worse than no button.

## Untrusted content

This is the highest-volume injection surface in the system. A note in the vault
was typed by the operator; a dropped export is a thousand cells written by
somebody else's system, and the extract is **persisted and re-read** — so a
fence applied only on the first read would leave every later question reading
the same content unfenced.

Planted-instruction tests live in `tests/test_planted_instructions.py` and
cover an instruction in a spreadsheet cell, a PDF body and a Word paragraph;
that it arrives fenced and flagged on the second read as well as the first;
that it cannot reach a gated tool; and that a cell cannot choose a column.

## The three design questions

### The same file dropped twice in one day

Nothing is written the second time. Identical bytes under the same name report
*"already imported today, byte for byte"*, which is not an error — it is what
dropping the same file twice means.

A *different* file with the same name lands beside it as `netsuite gp (2).xlsx`,
because it is a different file and replacing the first would be a delete.

### A fourteen-sheet workbook

**Every sheet is named, always**, with its row count. Only the contents are
bounded: the first `imports.extract_rows` rows of the first
`imports.extract_sheets` sheets, and the extract ends with a list of what was
left out.

Naming only the sheets that were quoted would leave the operator confidently
wrong about what is in their own file, which is the same failure as a truncated
document preview that looks complete.

### A drop that fails halfway

It cannot leave a partial file under the real name.

The bytes arrive complete or not at all — a truncated upload never reaches the
landing — and they are written to `<name>.part` beside the target and renamed
into place. A crash mid-write leaves the `.part`, which is not listed, not read
and not imported. It is also not deleted, because nothing here deletes; it is
reported.

## At the terminal

```
ranger imports show                  what has been dropped, newest day first
ranger imports read <name>           the bounded extract, written from the file
ranger imports gp <name>             what it would import. A dry run
ranger imports gp <name> --apply     write it, and remember the shape
```

`ranger imports gp` is the same code the window runs. It exists so the import
path can be checked without a browser, and because "no model is involved" is
easier to believe when it runs in a terminal with no model attached.
