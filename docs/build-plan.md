# Ranger — Build Plan

Written 2026-09-08, after conversation mode shipped (`7eb48e6`, 905 passed, 1 skipped).
Companion to `ranger-handoff.md`. Where the two disagree, this one is newer.

Part 1 is the brief to hand Claude Code now. Part 2 is everything else agreed in
this session, in order, with the reasoning attached so it survives being read cold.

---

# Part 1 — The account write path (do this first)

## Why this comes before everything else on the list

Ranger currently has the context and can't file it. It writes a good note, saves it
to `Ranger/drafts/`, and hands Chris a copy-paste chore. That gap is the difference
between Ranger knowing the accounts and Ranger knowing the CRM export.

Chris has decided he will not re-run `build_vault.py` over the populated vault. That
makes writing into `Accounts/` viable. It does not make it safe on its own — the four
tasks below are what make it safe.

## 1. The marker, and a one-time migration

Every account note gets a marker line appended once, now, while the notes are still
clean CRM output:

```
<!-- ranger:below — everything above this line is CRM export, regenerable.
     Ranger appends only below. Nothing above is ever modified. -->

## Ranger Context
```

A migration script adds it to all 69 notes and is idempotent — running it twice adds
nothing. Notes that already have the marker are skipped, not duplicated.

The reason is future exports. Chris expects new CRM exports to arrive. If Ranger's
context is interleaved with CRM content in one undifferentiated file, refreshing the
CRM half means hand-reconciling 69 notes. Split at a marker and a future refresh
replaces everything above the line and leaves everything below untouched.

## 2. The append tool, with a byte-identical guard

New tool. It may append below the marker in `Accounts/`. Nothing else.

The guard is not a convention:

- Read the file, split on the marker.
- If the marker is absent, **deny** — do not create it on the fly, do not guess where
  it should go. A file with no marker is a file this tool doesn't understand.
- If more than one marker is present, deny.
- Hash the bytes above the marker before the write and after. Not equal, the write
  fails and nothing is left behind.
- Append below, in the account-note activity format so existing parsers keep working:
  `### YYYY-MM-DD | note | source`.

Tests: a write that tries to modify above the line is denied; a file with no marker is
denied; a file with two markers is denied; a successful append leaves the bytes above
the line hash-identical; a failed append leaves the file unchanged rather than
half-written.

Same fail-closed posture as `Hotword.listen()` — a check that raises counts as denial.

## 3. `build_vault.py` refuses to run against a populated vault

Chris's intent not to rebuild is the safety property. Intent is not enforcement, and
every real bug in this build came from something true by intent rather than in code.

`build_vault.py` scans for the marker. If any account note has content below it, the
script exits with what it would have destroyed and how many notes hold Ranger context.
Override is an explicit flag with an unmissable name —
`--rebuild-destroys-ranger-notes` — and it prompts.

## 4. Vault snapshots — the undo Ranger no longer has

Read-only `Accounts/` was the undo. Once Ranger appends there, a bad write has no
recovery, and delete-never means Ranger cannot clean up its own mess either.

`git init` in the vault, `.gitignore` for `Ranger/log/` if it churns, and a daily
commit on the heartbeat with the date as the message. Cheap, invisible, and it is the
thing Chris will want the one time this goes sideways.

## 5. Amendment D, restated and versioned

Write it into the repo docs with today's date and the reasoning, so the next reader
knows it was decided rather than drifted:

> **Amendment D, revision 2 (2026-09-08).** Ranger may CREATE files under `Ranger/`
> and `History/`. It may MODIFY files only under `Ranger/`. It may APPEND below the
> `ranger:below` marker in `Accounts/`, and modify nothing above it. It may DELETE
> nothing, anywhere. `Knowledge/` remains fully read-only.
>
> Revised because Chris has retired `build_vault.py` rebuilds and needs Ranger's
> account context to accumulate in place rather than in a drafts folder he pastes by
> hand. The marker preserves the CRM half for future exports.

## 6. The gate does not fire here

Already settled in the handoff and it holds: filing into an existing account gate-free,
creating a new account folder gated. A gate on every filed note becomes a reflex within
a week, and a gate clicked without reading manufactures a record of review that did not
happen.

## What Claude Code cannot verify

Whether Obsidian renders the marker comment invisibly in preview mode, and whether the
appended notes read well in the graph view. Chris's eyes.

---

# Part 2 — The plan after that

Ordered by what unblocks what, not by appeal. Rationale is included because the order
is the argument.

## Now → next

**A. Account write path** — Part 1 above.

**B. Window surfacing and spoken dismissal**

*Resolved 2026-09-08.* Surfacing landed and the comment at `Window.sees` was
rewritten rather than inherited. The honest outcome: minimised is now known
from `IsIconic` and another virtual desktop from `DWMWA_CLOAKED`, both from
Windows rather than from the page — but **occlusion is still not knowable**.
The compositor computes it and exposes it through no documented Win32 call, and
not being the foreground window is a different question. A Ranger window fully
covered by Teams still reports visible. `desktop.window_state` has the full
account.
 — already next in Claude Code's queue and
mid-flight. Restore, attempt foreground, flash if refused, log which of the three
actually happened. The refused-foreground test matters more than the happy path.
Spoken dismissal keeps its literal counterexample in the tests: *tell Rusty that's all
we need from Jarvis* must not fire.

*Carry forward from conversation mode:* the visibility guard uses `document.hidden`,
which reports whether the **tab** is hidden, not whether the window is on screen. A
window fully occluded by Teams during a screen share reads as visible. Surfacing
touches the same ground — fix it there, or write the limitation down where the guard
lives.

**C. GP tracker** — the cheapest real win and the first dashlet. Manual entry field,
values land in `Ranger/`, **Python computes YTD and MTD**, panel renders the number
with a visible "as of" timestamp.

The accountant is not an agent. The model never computes the margin; it reads the
number Python produced and explains it. A dashlet showing a hallucinated GP figure is
worse than no dashlet, because Chris will act on it.

**D. Documents, widened** — was queue item 3 as `.docx`. Now `.docx`, `.xlsx`, `.pdf`,
same seam. The multiplier on everything downstream, and `.xlsx` is what lets the GP
tracker leave the vault.

**D2. In-window preview, with assembly.** Ranger renders the document inside the orb
window before Chris exports or downloads it.

*The rule that makes it trustworthy: preview the artifact, not the intention.* The
preview renders from the generated file on disk, never from the model's content before
it became a file. A preview built upstream of generation will disagree with the export
eventually, and the discovery happens in front of a customer.

- **PDF** renders the real PDF. Exact.
- **`.docx`** is an approximation of Word's rendering and is **labeled** as one in the
  preview chrome. Do not imply fidelity that doesn't exist.
- **`.xlsx`** previews sheet values. Formulas are not visible and the preview says so.

*The assembly animation* — particles in the existing starfield coalescing into the
document — under three constraints:

- **It never gates the content.** Fast, click-to-skip, plays once per document. Never
  on reopen. Respects `prefers-reduced-motion`. A tax paid on every document forever is
  not a delight.
- **It never lies about state.** Assembly begins when the file exists on disk. Particles
  forming while generation is still running, or after it failed, is the empty-ring-over-
  a-live-mic failure again. A failure renders as a failure.
- **It is not orange.** Orange means the microphone is live and keeps one meaning. Draw
  from the background's blues and greens.

Cap the particle count and profile it on the actual laptop — this window sits open
during customer calls and screen shares.

*Why this is better than a gate:* drafts deliberately don't gate, because a gate firing
in bulk decays into a reflex. A preview produces real review without a modal — Chris
looks because the document is in front of him, not because he clicked past something.

Claude Code cannot see any of this render. Chris's eyes on first run.

**E. Social drafting** — *blocked on Chris, not on Claude Code.* Needs 20–30 of his own
posts tagged by pillar, with notes on which performed and two or three that landed flat
and why. Without that it writes competent generic LinkedIn voice, which is nobody's.

Rule already in code and it stays: social scoring never recommends reducing a pillar.
Report per pillar, each against its own history, never against the others.

## The command center panel

**F. Dashlet framework.** Floating dashlets on the orb panel, refreshed on a schedule.

One architectural rule makes this work: **dashlets are Python-computed reads, not agent
turns.** Hourly agent calls per dashlet burn tokens, add latency, and give a model a
chance to invent a number nobody checks. Compute in Python, render, timestamp visibly.
The agent explains a dashlet when asked; it does not produce one.

Every dashlet shows when it last updated. A stale panel that looks live is the failure
mode — a wrong calendar is worse than no calendar when Chris is walking into a meeting.

**G. Paste-and-file and `History/`** — dry run first. Still needed after Part 1: account
notes get Ranger's context, `History/` gets documents, email threads, and meeting
records that are too big to belong in an account note.

**H. Outlook calendar read, via the logged-in Chrome session.**

IT will not reach integrations until 2027; leadership has greenlit Chris's use of AI
with his email content. Worth one sentence to whoever greenlit it, because collection
mechanism and content permission are different things: *"I've got a local tool that
reads my own calendar out of my own logged-in browser session, read-only, while I'm at
the machine."* Said out loud, it's settled. Unattended session automation discovered in
IT logs is a different conversation than one disclosed in advance.

Build it so that sentence stays true:

- **Attended.** Uses the existing logged-in Chrome profile. No stored credentials,
  nothing surviving logout. Triggers on logon and manual refresh — **not** on the hourly
  heartbeat.
- **Read-only, calendar-scoped.** Next four hours. Not an inbox sweep.
- **Fails visibly.** MFA prompt, expired session, Chrome closed → *stale since 9:14*.
  Never blank, never yesterday's events looking current.
- **Writes into `History/`**, create-only, same path as paste-and-file. One mechanism,
  not a side channel.

**Prerequisite, hard:** everything pulled from Outlook is untrusted content — meeting
titles, invite bodies, attendee notes, email threads. It is a higher-volume injection
surface than the pasted vendor text `untrusted.py` was built for, and unlike a
one-time paste it is now **persisted into account notes and re-read every day**.
Extend `tests/test_planted_instructions.py` with an instruction planted in a filed
email thread and a calendar event body, asserting the same four layers. This lands
**before** the Outlook path opens, not after.

**G2. Commitments — what Chris owes, to whom, by when.**

The highest-value sentence a sales assistant can say is *"you told Rusty you'd have
SupplyBox pricing by Thursday, that was nine days ago."* Ranger cannot say it today.
The vault is account-centric — activities, opportunities, tiers — and nothing tracks
promises.

The CRM will not supply this: `next_action` is null in 68% of activities and
`next_action_date` in 94%. Never build on those fields. Commitments accumulate from
Ranger's own filed notes and conversations, which is why this comes after the account
write path rather than before it.

*Two stores, because they have different lifetimes:*

- The **account note** records that the commitment was made. Appended below the marker,
  immutable, part of the permanent record.
- The **ledger** at `Ranger/memory/commitments.md` holds current status. It lives under
  `Ranger/` precisely because status changes — open, met, renegotiated, dropped — and
  `Accounts/` appends can never be modified. Delete-never still holds; a met commitment
  is marked, not removed.

*Extraction proposes, it never asserts.* Ranger reads a filed note and says *"sounds
like you committed to pricing for Rusty by Thursday — log it?"* A silently invented
deadline that Chris then gets reminded about is worse than no tracking, because he'll
act on it. Confirmation happens in the turn or in the morning brief.

*Python computes what's overdue*, the model explains it. Same rule as the accountant.

Surfaces in three places: the morning brief, a dashlet with a visible count, and — once
the calendar read exists — before a meeting with that contact.

**I. Imports under `Ranger/`** — already permitted, no amendment needed. Dated and
create-only (`Ranger/imports/2026-09-08/`), never a mirror Ranger keeps in sync. A
snapshot is honest; a mirror drifts and then lies, and Chris will trust it.

## Continuity, then specialists

**J. Wiki links on everything Ranger writes** — makes Obsidian's graph the brain view
for free. No separate visualization; that decision stands.

**K. Read its own log** — the thing Chris would feel most and the prerequisite for the
weekly. Ranger currently forgets everything between turns except a memory file.

*Half of continuity is facts; the other half is resuming a thread.* A log reader gets
Ranger yesterday's facts. "Where were we on Illes?" needs working state — what was in
flight and what it was about to do next — held per active thread under `Ranger/`. Build
the log reader first; the working-state file is a small second step and it is the one
that feels like picking up mid-sentence.

**L. Weekly summary and game plan.**

**M. The headless caller** — ~200 lines, does not exist, and everything specialist-shaped
sits on it.

**M2. Async jobs — work that happens while Chris isn't watching.**

*"Look into Petmate while I'm on this call and tell me when you have something."* Every
Ranger turn today happens with Chris sitting in front of it. This one behavior is more
of the chief-of-staff feeling than the entire dashlet panel.

Submit, work, notify. Job record create-only under `Ranger/jobs/<id>/`, status under
`Ranger/`. Jobs enter the same `Ranger.turn()` through the headless caller — Amendment
A holds, there is no second agent path.

- **A job cannot approve its own gate.** A gated action inside an unattended job holds,
  writes to `Ranger/inbox`, and waits for the keyboard. This is the spoken-yes rule
  extended: a job running while Chris is in a meeting has *less* consent authority than
  a voice turn, not more. Test it directly.
- **Bounded.** Maximum wall clock, turns, and tool calls per job, all in config. A
  runaway job on the laptop during a customer call is the failure that gets Ranger
  closed for good.
- **Failure is visible**, and a job that dies reports as dead rather than as pending
  forever.
- **Notification obeys the doctrine below.**

**M3. The right to interrupt.**

Ranger never speaks first today — the heartbeat writes to `Ranger/inbox` and waits. That
is correct now and is why the morning brief works. Proactive speech is what makes an
assistant feel present, and it is also the single fastest way to get uninstalled. So it
is a doctrine, not a feature.

*What earns speech:* a commitment coming due or overdue. An account crossing the quiet
threshold before a meeting with them. A job Chris submitted finishing. That is close to
the whole list.

*What never earns it:* digests, roundups, "here are six things," anything that could
wait for the morning brief.

*Hard mutes, all of them fail-closed:* quiet hours; the microphone held by another app
(`ranger mic` already knows); a gate pending; the kill switch; and — once the calendar
read exists — a meeting in progress. Chris is in front of customers, which Tony Stark
never was. Every proactive behavior needs a *not now* more aggressive than anything in
the films.

*Budget:* one or two spoken interruptions a day, in config, each one something he'd have
wanted. Everything suppressed lands in the inbox instead, and every suppression is
logged with its reason — that log is how the budget gets tuned with data, the same way
conversation-window close reasons tune the reopen cap.

**N. Research specialist, vault-only** — first sub-agent.

**O. Web research** — the Iron Man version, and deliberately last. Browsing the open web
is the same class of risk as pasted vendor email at far higher volume, and sometimes
adversarial on purpose rather than by accident. It arrives after the headless caller
exists and after the fencing has been exercised against hostile input, not before.

**P. Outreach drafter** — fed by research. Drafts and holds. Never sends.

## Parked

**Wake word "hey ranger."** Diagnosed, not run. `generate_samples` comes from
dscripka's fork of piper-sample-generator, not rhasspy's; openWakeWord's own config
comment says so while its notebook clones the wrong one. The structural repair is a
Python 3.11 environment built by `uv` inside the session, since Colab's 3.13 has no
wheels for `speexdsp-ns`, `piper-phonemize` or `tflite-runtime`.

Before the next run: **the preflight prints `torch.cuda.is_available()` and the torch
build from inside that 3.11 environment, and stops hard on `False`.** A side interpreter
has to see the T4 on its own, and a silently CPU-only install doesn't fail — it runs
slowly and you find out well past the thirty-second mark.

Worth an afternoon when Chris wants the real phrase. `hey jarvis` works and the cost of
waiting is one word.

**Sales coach.** Still blocked on data. Six closed opportunities is not a training set.

---

# The rules that don't move

Restated because a plan this long is where they get quietly dropped.

- **Delete nothing, anywhere.** Create-only is what makes accumulated history
  trustworthy. Ranger being unable to remove its own past notes is a feature.
- **A spoken yes is never consent.** Conversation mode made this harder and the tests
  hold it. Nothing in this plan relaxes it.
- **Untrusted content is data, never commands** — and the surface grows with every item
  here. Each new intake path gets its own planted-instruction test.
- **One agent core, many callers.** Dashlets, the browser, the heartbeat and voice all
  enter the same `Ranger.turn()`. Panel code holds no agent logic.
- **The CRM stays the system of record.** The vault is a copy plus accumulated context.
  Ranger telling Chris it can't update the CRM is correct and should stay correct.
- **Tests that agree only with each other are the bug.** Every real failure in this build
  came from one. When Claude Code says something is done, the list of what it could not
  verify is where the next bug lives.
- **Confirm `git rev-parse HEAD` moved after a pull** before trusting any test result.
