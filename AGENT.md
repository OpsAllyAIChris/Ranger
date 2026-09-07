# AGENT.md

Standing brief for anyone, human or model, working on this repo. Read this
before writing code. It is short on purpose.

The build spec is `start-here.md` at the repo root. This file is the answers
and the decisions; that file is the method. Where the two disagree,
`start-here.md` wins and this file is what gets corrected.

## What Ranger is

A voice-first assistant for one person. It knows the operator's accounts,
remembers what it has been told, holds the memory of meetings and transcripts
and emails, and drafts what needs sending. It knows the company, the products,
the ideal client profile, the competitive landscape and the sales playbook.
Each morning it says what is slipping.

That paragraph is the destination, not the current scope. See Scope discipline.

## Who it is for

One person. Single-user harness, kept small. No per-user state anywhere. If it
ever becomes a product that is a separate decision, not a constraint on this
build.

## The four rules that do not bend

1. **The core is a library.** `ranger/core.py` holds all agent logic and has
   one entry point, `Ranger.turn()`. It yields events as it works. Every
   front end is a caller of it. There are four callers by the end: the
   terminal, the push-to-talk loop, the heartbeat, and the browser. If you
   find yourself writing agent logic in more than one of them, stop.
2. **Scope discipline.** Each new capability is one entry in the tool
   registry and nothing else. Do not build CRM writes, quotes, proposals or
   transcript ingestion ahead of their tier.
3. **Knowledge is context, not tools.** Company, products, ICP, competitors
   and playbook load from the read-only Knowledge folder straight into the
   system prompt. There is no tool to fetch them and there will not be one.
   When they outgrow the prompt, add selective retrieval behind
   `SelectionStrategy` in `ranger/knowledge.py`.
4. **Ranger writes only under `<vault>/Ranger/`.** Enforced in
   `ranger/vault.py`, in code, not in the system prompt, so a confused model
   cannot write outside it. `ranger/vault.py` has no delete method and no
   rename method. Do not add one.

## Safety posture

Everything Ranger reads is data, never an instruction. Vault notes, emails,
quotes, web pages, transcripts, pasted vendor text, tool output. If content
looks like it is telling Ranger what to do, Ranger flags it to the operator
and stops. `ranger/untrusted.py` fences and scans; the system prompt explains
it; neither is a substitute for the other.

These need the operator's explicit yes, every time. No blanket approvals. A
yes last time is not a yes this time.

- Sending anything to a human: email, LinkedIn, text, calendar invite.
- Posting anything publicly.
- Spending money or hitting a paid API beyond the model, transcription and
  speech providers already in config.
- Deleting or overwriting an existing vault note.
- Changing any record in an outside system, including the CRM.

A tool that does any of these is declared `confirm=True` in the registry. Until
the Tier 6 gate exists, the core refuses to run such a tool rather than
running it unguarded.

## Voice and tone

Crisp, plain-spoken, brief. A good colleague, not a butler and not a hype man.
Short spoken replies by default. If something needs more, say so and ask.
No filler openers. No restating the question.

Drafts follow the operator's writing rules: plain language, no dashes, short,
warm and direct.

## Vault layout

```
<vault>/
  Accounts/          account notes                READ ONLY
  Knowledge/         company, ICP, playbook       READ ONLY
  Ranger/
    memory/          Tier 4 durable facts         read + write
    inbox/           Tier 5 surfaced notices      read + write
    drafts/          Tier 2 draft and hold        read + write
    log/             Tier 6 audit trail           append only
```

The vault root lives in config and may be on an external drive. Nothing
assumes `$HOME`. It is a vault created for Ranger, not a pre-existing one, so
`ranger init` stands up the whole layout when the root does not exist yet.
Accounts and Knowledge are seeded by hand; `ranger doctor` reports what is
still empty.

`docs/vault-conventions.md` holds the note shapes. The one Tier 2 actually
parses is the `## Activity` section in an account note: newest ISO date in that
section is the last contact, future dates are plans not activity, and file
modification time is never used. Optional front matter `last_contact:` and
`status:` override and exclude. Read that file before writing the "what went
quiet" tool.

## Tiers

| Tier | What | State |
| ---- | ---- | ----- |
| 1 | The brain. Config, provider seam, agent core as a library, event stream, vault guard, terminal REPL | done |
| 2 | The hands. Three tools: account recall, draft and hold, what went quiet | done |
| 3 | The ears and mouth. Push to talk, transcript shown, barge-in, speech starts on the first sentence | done |
| 4 | The memory. Durable facts in `Ranger/memory`, one fact per entry, hand-editable | done |
| 5 | The heartbeat. Morning surface, quiet hours, held notices, a schedule that survives restarts | done |
| 6 | The rails. Confirmation gate, audit trail, cost tally, kill switch, everything tunable in config | later |
| 7 | The face. Browser front end: orb, glass shell, mic bar | after 6 |

Each tier ends with something runnable and a verification step in
`start-here.md`. Do not start a tier until the one before it verifies, and do
not fuse two tiers together.

Each tier is verified before the next one starts. Tier 7 waits for Tier 6.

One exception: `prototypes/orb.html` (Tier 7a) is a standalone preview that
touches nothing else and may be built early. It is a preview, not a
dependency.

## Configuration

**The operator's settings go in `ranger.local.toml`, which is git-ignored.**
`ranger.toml` is tracked and changes as Ranger is built, so anything the
operator set there conflicted on every pull and they lost their voice id
repeatedly. The local file overrides the tracked one key by key, one level deep,
so setting `tts.voice_id` does not discard the rest of `[tts]`. Overrides are
listed by `ranger doctor` so they are visible rather than magic, and the
unknown-key check runs on the merged result, so a typo in the local file is
caught with the same did-you-mean. Never add a setting to `ranger.toml` that
the operator is expected to fill in: give it an empty default there and tell
them to override it.

Every table's keys are checked against `KNOWN_KEYS` at startup and an unknown
one is refused, with a pointer to the right table when the name exists
elsewhere. This is not tidiness: a dead `voice_id` sat in `[voice]` while the
code read `tts.voice_id`, so setting it looked like configuring the voice and
did nothing. `tomllib` already rejects a true duplicate within one table, so
duplicate detection would not have caught it. A setting nothing reads is worse
than a missing one.

Everything tunable lives in `ranger.toml`. No model name, vault path, hour or
threshold is hardcoded anywhere in the source. Secrets live in `.env`, which
is git-ignored, and never in `ranger.toml`.

### The model call

Two things about the current Anthropic API that are easy to get wrong, both
already handled in `provider.py`:

- **`temperature` is gone.** Current models reject it outright. The lever is
  `output_config: {effort: ...}`, one of low, medium, high, xhigh, max. It is
  `model.effort` in the config and defaults to `low`, because spoken replies
  should be quick. Raise it when Ranger starts writing proposals.
- **Adaptive thinking is on by default,** and thinking blocks must be echoed
  back unchanged on the next round of a tool-using turn. `provider.py` passes
  through every block it does not itself interpret. Dropping one breaks the
  turn. Thinking tokens also count against `max_tokens`, so leave headroom.

Not done yet, deliberately: prompt caching. Ranger sends the whole Knowledge
folder in the system prompt on every turn, which is exactly what caching is
for. The clock is already last in the system prompt so the prefix above it
stays byte-identical, which is the prerequisite. Adding the breakpoint belongs
with the cost tally in Tier 6.

### Hours and resilience

`schedule.morning_hour` must sit outside the quiet window. That is validated
at startup and the process refuses to run if it does not.

The network will drop. `model.max_retries`, `model.retry_backoff_seconds` and
`model.timeout_seconds` govern how hard Ranger tries. Retries only happen
before any of the reply has streamed, so a half-spoken answer is never
repeated. When Ranger gives up it says so in one plain sentence and hands back
a clean prompt. A failed turn is rolled out of the transcript entirely, so the
next turn starts from a conversation that actually happened.

## Tier 2, as built

Three tools in `ranger/toolset.py`, and the registry holds nothing else.
`account_recall`, `draft_and_hold`, `what_went_quiet`.

Reading account notes lives in `ranger/accounts.py`. The format contract is in
`docs/vault-conventions.md` and the parser follows it rather than the other way
round. Points worth knowing before changing any of it:

- **`scan_note` and `parse_note` must agree.** The quiet check uses the cheap
  scan across every note; recall uses the full parse on one. A test asserts
  they return the same last-activity date and status for every fixture.
- **Resolution never guesses.** Exact, then substring, then fuzzy, and fuzzy
  compares the fragment against each word of a name as well as the whole
  thing, because "Northwynd" scores 0.55 against "northwind provisions" and
  0.89 against "northwind". More than one hit is always a question for the
  operator.
- **Recall returns a digest.** Notes reach 25,000 characters and the whole note
  must never enter the conversation.
- **The dash rule is enforced in code,** in `ranger/drafts.py`. Em and en
  dashes only; hyphens are left alone because "follow-up" is an ordinary word.
  A refused draft comes back to the model for a rewrite.

The operator's real notes hold customer email, pricing and confidential
material. They are never committed here. `tests/fixtures/vault/` holds
fictional notes that match the contract exactly; the tests prove the logic and
the operator's own run proves the format.

## Tier 3, and why it is split

Audio is the most platform-specific thing in the build and **none of it can be
verified from the sandbox**: `import sounddevice` raises
`OSError: PortAudio library not found` there, because the generic wheel carries
no PortAudio. Only the operator can confirm a working microphone.

So Tier 3 is four things that run independently, and each one narrows where a
failure can be:

| Step | Command | Needs |
| ---- | ------- | ----- |
| 3a | `ranger audio devices`, `ranger audio check` | nothing. No keys, no network |
| 3b | `ranger transcribe file.wav` | Deepgram key, no microphone |
| 3c | `ranger say "..."` | ElevenLabs key, no microphone |
| 3d | `ranger --voice` | everything |

Rules that hold across all four:

- **No audioop.** Removed in Python 3.13. Level metering uses stdlib `array`.
- **sounddevice is imported lazily,** never at module import, so the rest of
  Ranger runs on a machine with no audio stack.
- **PyAudio is never a dependency.** No wheel for 3.14; it would build from
  source and need Visual C++ plus a PortAudio the operator supplies.
- **The backend is a Protocol** so every test runs against a fake.
- **Devices resolve by name fragment, not index.** Indices shuffle when a USB
  microphone or a headset connects, and a stale index silently records the
  wrong thing. Ambiguity is a question, never a guess, exactly as with account
  names.
- **Silence is the failure to design for.** It throws nothing: on Windows the
  microphone privacy setting is off, the stream opens, every sample is zero.
  `judge()` separates digital silence from a faint signal because they have
  different causes and different fixes.
- **No non-daemon timers.** One leaked `threading.Timer(60, ...)` held the
  whole process open for a minute after the command had finished, while the
  suite cheerfully reported passing in 1.6 seconds.

## Tier 5: the inbox is the schedule

There is no state file. Whether the morning surface has run today is answered by
whether `Ranger/inbox/2026-09-08 morning.md` exists. One decision, and restart
safety, catch-up and no-refire-storm all fall out of it rather than being
separately engineered:

- **Asleep at 07:00, opened at 14:30.** Today's file is missing and the hour has
  passed, so it runs then. Catch-up, not skip. This is the case the operator
  asked about by name.
- **Restarted three times before lunch.** The file exists, so nothing fires.
- **Away for a week.** Only today is considered. Six missed mornings are not
  replayed, because a week-old list of what went quiet is not news.
- **No state file to drift** from the vault, be lost on a reinstall, or need
  migrating.

Other rules that hold:

- **Quiet hours hold, they do not drop.** A check due at 23:00 is skipped and
  stays due, so it fires when the window ends. `runs_in_quiet_hours` is the
  opt-out for something genuinely urgent; nothing sets it yet.
- **Nothing waits on a person.** Every check runs under
  `heartbeat.check_timeout_seconds` and a hang leaves a note saying nothing was
  changed, rather than deadlocking on someone who is asleep.
- **No stacking.** A check still running when its next turn comes round is
  skipped, not queued.
- **Dismissal rewrites the file's front matter** to `status: dismissed`, at the
  operator's explicit command. Nothing is deleted: the vault has no delete path
  and is not getting one, so a dismissed notice stays readable as a record.
- **The morning surface calls the existing `what_went_quiet` tool.** There is no
  second implementation of that logic, so the spoken answer and the morning file
  cannot disagree.
- **Both front ends announce what is waiting** on startup. That is the
  catch-up-on-return half: the notice is held in the vault, and seen when the
  operator comes back.

## Memory against account notes, and the context budget

**The line.** Could a CRM export overwrite it? Then it belongs in the account
note. `scripts/build_vault.py` regenerates every account note from the exports,
so anything written into one is destroyed on the next refresh. Account notes
hold facts about companies. Memory holds facts about the operator and how they
work, which no export records and which would otherwise be lost.

Three things stop them drifting:

- **Account notes win on account facts.** Stated in the system prompt as an
  ordering rule, so a memory that contradicts a note is treated as stale.
- **`remember` refuses account-shaped facts** outright, naming the phrase that
  gave it away, and tells the operator it belongs in the note.
- **Every fact is dated,** so a stale one is visibly stale.

**The budget.** Account recall never competed: it is a tool result in the
messages, capped by `[recall]`. Only memory and knowledge share standing space,
and `[context] budget_chars` is the total. **Memory is loaded first** up to
`memory.reserve_chars`; knowledge gets the remainder. When they collide,
knowledge loses, because memory is small, is about the operator, is
unrecoverable, and a dropped knowledge file announces itself.

The real constraint is not the context window, which is far larger than this
budget. It is that the system prompt is resent every turn. Prompt caching makes
a stable prefix nearly free after the first turn and would let the knowledge
budget rise a long way; the clock is already last in the prompt so the prefix
is byte-stable. That is Tier 6.

**Deleting is gated.** `remember` appends, so nothing already written can be
lost. `forget` rewrites a file, which is on the never-without-asking list, so it
is `confirm=True` and the core refuses it until the Tier 6 gate exists. The
operator deleting the line in Obsidian is the immediate path and always will be.

## Misheard account names: which layer fixes it

A real decision, recorded because the obvious answer is wrong.

Account names are what transcription gets wrong most, and `resolve_account`
already matches fuzzily. So the temptation is to loosen the matcher. **Do not.**

- **Hinting fixes the string. Fuzzy matching only fixes the lookup.** If the
  transcript says "Ellis Foods", fuzzy matching finds the right note, but the
  reply and any draft still say Ellis. Only hinting corrects the text that
  flows onward.
- **Loosening the matcher trades a visible failure for an invisible one.**
  Today an ambiguous name is a question to the operator. Drop the cutoff and
  more queries resolve to a single confident answer, some of them the wrong
  account, said out loud with no signal that anything was guessed. Reading out
  the wrong account's status is worse than asking which one.
- **There is no data yet.** Tuning a matcher before seeing which words Deepgram
  actually gets wrong is guessing. `ranger transcribe --expect` exists to
  produce that data.

So: hinting only, at the transcription layer. `resolve_account` is unchanged.

**Measured, once real audio existed.** Every residual mishearing turned out to
be a spelling variant of a name heard correctly: wexar/Wexxar, illus/Illes,
vitalogy/Vytalogy, prejus/Pregis, captivair/CaptiveAire. All of them already
resolve to the right account, at 0.80 to 0.91 similarity. `prejus` at 0.67 is
the thin one, and is the reason the 0.6 cutoff should not be raised either.

**Not every name is hintable, and the reason is acoustic.** Hinting rescued
Pregis and CaptiveAire but not Wexxar, Illes or Vytalogy. The rescued ones were
misheard as different *sounds* (prejus, captivair): the hint gave the decoder
information it lacked. The unrescued ones are **homophones of their
mis-spelling**. "wexar" and "Wexxar" sound identical, so there is no acoustic
evidence for a hint to tip. Doubling a letter, or swapping i for y, changes the
spelling and not the sound. Expect names of that shape to stay wrong in the
transcript and to be caught downstream by `resolve_account` instead.

The next move, once there is evidence, is **not** a looser cutoff but deriving
`keyterms` from the account filenames in the vault, so the hint list is exactly
the 69 names that matter and stays current as accounts are added. That is still
the transcription layer. Only if hinting demonstrably fails on a name should the
recall layer change, and then by adding a spoken-form alias to the note rather
than by loosening the matcher for all 69.

## Tier 3d, the full loop

`ranger/voiceloop.py` holds no agent logic. A spoken turn is a typed turn with
different ends: the transcript goes into the same `Ranger.turn()`. `ranger`
with no flags is still the typed REPL and always will be.

Four things it exists to get right, all about how the wait feels:

- **Something is printed the instant the key comes up,** before transcription.
  That gap is most of a second and silence in it reads as broken.
- **The transcript is shown next to the reply, every turn,** so a wrong answer
  can be blamed on the ears or the brain without guessing. Words Deepgram was
  unsure about are listed under it.
- **Speech starts on the first sentence,** not the finished reply.
  `ranger/speech.py` splits the stream, holding back on abbreviations,
  decimals and initials, with a lower length bar for the first sentence
  because that is the one being waited on.
- **A press interrupts, and is the same press that starts the next turn.**
  Cutting in and speaking is one action. Quitting is deliberately not the same
  event as interrupting: both stop the speech, only a press is a barge-in and
  carries forward.

Recording never overlaps playback, because recording only begins after the
speech has been stopped. There is a test asserting that from the backend's own
event order rather than from the code's intent.

## Where the hint list comes from

`ranger/keyterms.py` builds it from the account filenames, so it cannot go
stale. Three decisions in it:

- **Only distinctive words get a slot.** A token needs four letters, must not
  be a generic business word, and must appear in exactly one account name.
  Document frequency does most of the work: "Packaging" in a dozen names
  disqualifies itself with no list to maintain. A name made entirely of common
  words falls back to hinting the whole name.
- **Ranked by recency of activity, then volume.** That is what predicts what
  the operator is about to say. An account worked last week earns a slot over
  one last touched in January.
- **Cut at `stt.max_hints`, default 100.** Deepgram's real limit could not be
  verified from the sandbox. If the cap is above their limit the request comes
  back 400 and the error names the parameter, which is the same path that
  covers the parameter names changing.

Hints are not free: the operator measured 827ms with 11 hints against 516ms
with none. That is one sample and could be noise, but it means raising
`max_hints` toward 100 should be checked against latency rather than assumed.
`ranger keyterms` shows exactly what would be sent and what was cut.

## Layout

```
ranger/
  config.py      load and validate ranger.toml, fail loudly at startup
  events.py      the event stream: state, text, tool calls, notices
  vault.py       the wall. read anywhere in the vault, write only under Ranger/
  untrusted.py   fence and scan anything Ranger did not write itself
  provider.py    the model seam. AnthropicProvider is one implementation
  knowledge.py   Amendment C. loads Knowledge/ whole, with a retrieval seam
  prompts.py     system prompt, assembled from parts
  tools.py       the registry machinery
  toolset.py     the three Tier 2 tools, and nothing else
  accounts.py    reading account notes: parse, resolve a name, what went quiet
  drafts.py      draft and hold, including the writing rules
  audio.py       devices, levels, wav, and the PortAudio backend behind a seam
  trigger.py     push to talk: hold via pynput, toggle via stdlib, fixed for tests
  audiocheck.py  Tier 3a: 'ranger audio devices' and 'ranger audio check'
  stt.py         Tier 3b: Deepgram behind a seam, plain HTTP, no SDK
  compare.py     what you said against what it heard, with a word error rate
  keyterms.py    the hint list, derived from account filenames and ranked
  memory.py      Tier 4: durable facts, plain markdown, read fresh every turn
  heartbeat.py   Tier 5: the loop, the inbox, and the checks. Inbox is the state
  tts.py         Tier 3c: ElevenLabs behind a seam, streaming, plain HTTP
  speech.py      splitting a streaming reply into speakable sentences
  voiceloop.py   Tier 3d: push to talk wrapped around the core, no agent logic
  core.py        the agent. one entry point. all the logic
  cli.py         the terminal. first caller of the core, permanent debug path
  testing.py     ScriptedProvider, so the core is verifiable with no API key
```

## Working here

- Python 3.11 or newer. No heavy framework. Small enough to read whole.
- `pip install -e ".[dev]"` then `pytest`. The suite runs offline and costs
  nothing; keep it that way.
- Run it as bare `pytest`. `python -m pytest` prepends the working directory to
  `sys.path`, which masks import mistakes: it once hid a `from tests.conftest
  import ...` that broke collection for anyone running it normally.
  `tests/test_suite_hygiene.py` now fails on that pattern. Shared test helpers
  are fixtures, never imports.

## Windows cannot be verified from here

The operator develops on Windows. Every agent session for this project runs in
a Linux sandbox, so **a green suite here is not evidence that the suite is
green for them.** The operator is the only one who can confirm that. Say so
rather than implying otherwise, and do not call a change verified on the
strength of a Linux run alone.

This is not hypothetical caution. Three Windows breaks got through in a row:

1. `VaultFile.relative` built with `str(Path.relative_to(...))`, so backslashes
   leaked into the system prompt and two tests asserted the POSIX form.
2. `from tests.conftest import ...`, which resolves under `python -m pytest`
   and not under a bare `pytest`, so collection died on their machine while
   looking green here.
3. The test config template interpolated a Windows `tmp_path` into a
   double-quoted TOML string, where a backslash is an escape. 6 failed, 54
   errors, and none of it reproducible here without deliberate simulation.

What actually helps, in order:

- **Write platform-neutral assertions.** `Path.name` splits on backslash under
  Windows and not under POSIX, so an assertion using it can mean two different
  things. Assert on strings when the property under test is a string.
- **Simulate deliberately.** Patching a fixture to emit a Windows-shaped path
  reproduced break 3 exactly (6 failed, 12 passed, 54 errors) before any fix
  was written. A simulation stops being faithful once it depends on the
  filesystem, though: after the fix, the same simulation fails 16 tests on
  Linux for reasons that do not exist on Windows.
- **Prefer things that cannot differ.** POSIX separators in strings that reach
  the prompt, `encoding="utf-8"` pinned on every read and write, ASCII-only
  console output, TOML literal strings for any path.
- **Never write `%-` or `%#` in a date format.** `%-d` is a glibc extension:
  Linux prints "7", Windows raises `ValueError: Invalid format string`. The one
  that shipped would have crashed the morning check every day and every
  `ranger inbox` listing. Use zero-padded directives, or take the integer off
  the datetime; `ranger/dates.py` has the helpers.
  `tests/test_suite_hygiene.py` fails the suite on either directive anywhere in
  the tree, so it cannot come back.
- **Hand the operator a command, not a claim,** when a break is theirs to
  observe. Ask for the full traceback instead of guessing from a truncated one.
- **Never let a simulation write into the repo.** A Windows-shaped path is
  relative on POSIX, so `mkdir(parents=True)` builds it under the working
  directory. A `C:\tmp\...` tree got committed that way, and git on Windows
  then refused every checkout with `error: invalid path`, so the operator sat
  three commits behind while reporting results from stale code. Two guards now
  exist: `vault.root` must be absolute, and `tests/test_suite_hygiene.py` fails
  on any tracked path Windows cannot represent.
- **Audit the index, not the working tree,** before pushing. `git ls-files`
  reads the index; a `git reset` can silently restore files you thought you had
  removed.
- `ranger doctor` checks config, vault and environment.
- `ranger init` creates Ranger's own folders and nothing else, after asking.
- `ranger` starts the terminal REPL.
