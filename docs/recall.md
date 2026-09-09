# Item K1: reading back the log

Ranger has written `Ranger/log/<date>.md` since Tier 6 and could not read it.
**The fourth write-only path in this project**, after drafts, the account marker
and GP. A thing Jarvis produces and cannot look at is a thing the operator has
to open Obsidian to check on its behalf.

## What it answers

- *"What did we talk about yesterday"* — a digest of the last day with entries.
- *"What did I ask you to do this week"* — `days: 7`, the default.
- *"Have I already looked at Petmate"* — `about: "Petmate"`, a plain search.
- *"What did you file on Telly and when"* — the same search, and every line
  comes back with its date on it.

## Everything it returns is history, and says so in its shape

Inherited from the time-blind writing fix, and it matters more here because
**the log is nothing but old conclusions**. A three-week-old "waiting on their
reply" recalled as the state of today is the Telly problem with a longer fuse.

Every line is built in Python with the date in front of it and a past-tense
framing:

    On 8 September at 09:14 you asked: where are we on Illes Foods
    On 8 September at 09:14 Jarvis answered: Illes went quiet after the June quote.
    On 8 September at 10:02 Jarvis ran file_to_account ok: appended to Telly

A model told to use the past tense will mostly use the past tense. A line whose
framing is already past has nothing left to get wrong. Note what is *not*
rewritten: the operator's own question keeps its own words, present tense and
all. Rewriting "where are we on Illes" into the past would be putting words in
their mouth, which is a worse failure than the one being guarded against.

## Python assembles, the model reads

`ranger/recall.py` parses the table rows, groups them by day, decides which to
show and writes the lines. The model reads the result out. It cannot infer what
happened because it is not given anything to infer from — "have I looked at
Petmate" is answered by finding the rows that mention Petmate, not by concluding
anything about Petmate.

## Bounded, and it names what it left out

A digest across a window by default: seven days, six entries a day, the most
recent of each day first so a long day shows how it ended. Housekeeping rows
(`heartbeat`, `voice`, snapshots) are dropped from a digest so the shape of a
week is legible — **a search still reaches them**, or "have I looked at Petmate"
could be answered "no" because the only mention was in a row the digest
considers noise.

Ask for one `day` and that day comes back in full, housekeeping included.

It always says which it was:

    Showing 6 of 20 matching entries across 3 day(s). 14 were not shown.

    That is all 6 matching entries across 2 day(s). Nothing was left out.

Silent truncation of a spreadsheet is bad. Silent truncation of history is
worse: the operator cannot tell "that did not happen" from "that did not fit".

And nothing found is reported as **nothing logged, not nothing happened**. The
log holds what Jarvis did and what was said to it, not what the operator did
elsewhere.

## Untrusted, and more so than most

The log holds transcripts of what the operator said, and what the operator said
routinely includes pasted customer email and vendor text. **Trust attaches to
the path the bytes travelled, not to who wrote the file**: these came from
outside, went through the log, and are coming back. They reach the model fenced,
exactly as a dropped file does, and instruction-shaped language in them is
flagged.

## Read-only, ungated

Reading its own output is not consequential. What makes it safe is not a card:
nothing here writes, and everything here is fenced. Headless runs get it for
free — reads stay free when nobody is there, so a job that runs during a meeting
can read what the last one found.

## From the CLI, with no browser

    ranger log --recall                 # the last 7 days, as the tool sees it
    ranger log --recall --days 30       # a wider window, capped at 31
    ranger log --about Petmate          # every mention, across the window
    ranger log --about Telly --days 90
    ranger log 2026-09-08 --recall      # one day, in full
    ranger log 2026-09-08               # the raw table, unchanged

`--recall` prints exactly what the model is handed, assembled by the same
Python, so the two cannot drift. Without it the command still prints the raw
table — which is what to look at when the digest and the file seem to disagree.
