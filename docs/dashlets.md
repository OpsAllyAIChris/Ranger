# Dashlets, and the GP tracker

Item C on the build plan is the gross profit tracker. It is also the first
dashlet, so the seam it sits on is the more consequential half of the work:
everything else on the command centre panel hangs off the rule set here.

## The rule

**A dashlet is a Python-computed read of the vault.** Not an agent turn, not a
cached model answer, not a background prompt on a timer.

Two reasons, and the second is the one that matters.

*Cost and noise.* The panel redraws on every push — every turn, every tool
call, every dismissal. Eight dashlets that each cost a model call would be
eight calls a minute spent restating numbers already sitting on disk.

*Correctness.* A figure a language model produced can be wrong in a way that
looks exactly right. The operator acts on gross profit. **A dashlet showing a
hallucinated GP figure is worse than no dashlet**, because an empty panel is
obviously empty and a wrong number is not.

`ranger/dashlets.py` holds the seam: a frozen `Reading`, and `readings()` which
walks a fixed tuple of sources. Registration is that tuple and nothing else —
there is no discovery and no plugin loading, because a dashlet that appears by
being on disk is a dashlet nobody chose.

`tests/test_gp.py::test_the_dashlet_path_cannot_reach_a_model` walks the import
graph of `dashlets.py` and `gp.py`, function-level imports included, and fails
if it can reach `provider`, `core`, `prompts`, `assembly`, `speech`, `tts` or
`stt`. That test, not this document, is what keeps the rule true in six months.

Three properties every reading holds to:

- **Absence is never zero.** No value, and words saying why. A zero looks like
  a figure that was measured.
- **Every reading carries when it is from.** `as_of` is the moment the
  underlying entry was recorded, never the moment the panel drew it.
- **A failed read says so.** An exception becomes a reading with `error` set.
  "Could not read" and "nothing entered" are different states and the panel
  shows them differently.

## GP: the three design questions

### What does an entry look like?

One create-only note per entry, in `Ranger/gp/`:

```
Ranger/gp/2026-08 entered 2026-09-01 090000.md
Ranger/gp/2026-08 entered 2026-09-03 163000.md    <- a correction
Ranger/gp/2026-09 entered 2026-09-08 111500.md
```

```markdown
---
period: 2026-08
amount: 45500
recorded: 2026-09-03T16:30:00
---

# Gross profit for 2026-08

$45,500.00
```

`period` is the month the figure is *for*; `recorded` is when it was entered.
They are different fields because August's figure usually arrives in September.

**A correction is a new entry, not an edit.** Delete-never holds here as it
does everywhere else, so a figure that turns out wrong is superseded rather
than changed: a second note for the same period, and reading takes the newest
`recorded` per period. The earlier note stays on disk. The folder therefore
reads as a record of what was believed and when, which is more useful than a
file that only ever shows the current answer — and `ranger gp history` prints
every entry, marking the superseded ones.

Nothing in this path can overwrite: `gp.write` goes through `vault.write_new`,
which refuses an existing file, and two entries made in the same second get
`(2)` appended rather than one landing on the other.

That collision is more common than it sounds, and it carried a bug worth
recording. `recorded` is stored to the second, and a person correcting a figure
does it seconds after noticing — so both entries carry the same stamp and the
tie has to be broken by something else. It used to be broken on the filename,
where `... 213149 (2).md` sorts *before* `... 213149.md`, because a space is
lower than a dot. The correction lost to the figure it was correcting and year
to date was quietly light. It is now broken on the collision counter, which is
the write order: `(2)` exists only because the first note was already there.

The suite did not catch it — every fixture corrected a figure on a different
day — and running `ranger gp add` twice did, in about four seconds. The
regression test is `test_a_correction_made_in_the_same_second_still_wins`.

### The year and month boundaries

Year to date sums the current entry for each month from the start of the
financial year to the current month. `gp.year_starts_month` is configuration
rather than an assumption, because a year starting in April is ordinary and
guessing January would put April's figure in the previous year's total — the
figures would still add up, which is what makes that failure dangerous. With a
non-January start the label reads `FY2026` rather than `2026`.

On the first day of a new year YTD legitimately drops to nothing. Rather than
looking broken, the reading carries `last_year` and `last_year_total`, and the
panel shows last year's total beside the empty one.

The month boundary is the same shape: MTD is `None` until that month is
entered, and the detail line says `2026-10 not entered`. It is never `$0`.

A figure entered against a future month stays on disk and out of year to date.

### What the panel shows with no entries

**"no GP entered yet".** Not `$0.00`, not `—`, not a blank where a figure would
go. `Reading.value` is empty and the front end draws `empty` in a different
style from a value, so a missing figure can never be mistaken for a measured
one. Once a year has turned over and nothing has been entered in the new one,
it says `nothing entered for 2027 yet` instead — which is a different state,
and one where last year's total is still worth showing.

Every figure the panel draws carries an "as of" line: when the newest entry was
recorded, and how many days ago that was. Past `gp.stale_after_days` (45 by
default — GP arrives monthly, so one missed month shows) the value dims and the
line says `stale`. **A stale figure that looks current is this dashlet's
failure mode**, so the age is on screen rather than in a tooltip.

## Getting a figure in

Three surfaces, one implementation (`gp.record`):

- the entry field in the panel's Numbers section — type the figure, Enter;
- `ranger gp add 48250`, or `ranger gp add 44000 --period 2026-08`;
- and that is it.

**The model has no way to record a figure.** There is one GP tool,
`gross_profit`, and it reads. A model that could enter a figure could enter one
it inferred from a conversation, and a figure nobody typed is exactly what this
module exists to keep out of the vault. What `gross_profit` returns is already
added up and already formatted, and it says in as many words that the numbers
are not to be recalculated, converted or extrapolated.

## What is in the snapshot

`Ranger/gp/` is in the snapshot allow list. Hand-entered figures exist nowhere
else — unlike everything under `Accounts/`, they cannot be rebuilt from a CRM
export — so they belong in the vault's only undo.

## Adding the next dashlet

1. Write the read in its own module. Python only; no provider, no core.
2. Return a `Reading`: a value **or** an `empty` string, an `as_of`, and a
   `stale` flag if the number can go off.
3. Add it to the tuple in `dashlets.sources()`.
4. Add its module to the import-graph test's starting set.
5. If the panel needs to write anything back, it goes through one function that
   the CLI calls too, so the two surfaces cannot mean different things on disk.
