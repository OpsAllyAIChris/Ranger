# Vault conventions

The account note format is the operator's, set by their CRM export. This
records it so the tools and the exporter stay in agreement. Where this file and
the real notes disagree, the notes win and this file is what gets corrected.

```
~/Obsidian/Ranger-Vault/
  _vault-build-report.md   not an account note, excluded from every scan
  Accounts/                one note per account      yours, read only
  Knowledge/               the business context      yours, read only
  Ranger/                  Ranger's own folders      it writes only here
```

## Account notes

One file per account. **The filename is the account name**, which is how a
spoken fragment finds it.

Sections in order:

1. A metadata block of `- **Label:** value` lines: Status, Tier, Industry, HQ,
   Locations, Revenue tier, Annual packaging spend, Strategic fit, Incumbent,
   Decision structure, Source.
2. Optional `## Pain points`, `## Target solution`, `## Notes`.
3. Optional `## Contacts`, bulleted, one per contact.
4. Optional `## Opportunities`. See below.
5. `## Activity`.

Only the metadata block counts as metadata. `- **Label:** value` lines below a
`##` heading belong to that section, so an opportunity's `- **Owner:**` is not
mistaken for an account field.

### The opportunities section

Each opportunity is a heading and then one pipe-delimited line, written by
`build_vault.py`:

```markdown
## Opportunities
### Wexxar Case Sealers (5 Units)
Proposal | 40000 | Q1 2026 | 60 probability
```

Stage, estimated value, expected close, probability. **Any of the four may be
absent**, and an absent element shortens the line rather than leaving a gap, so
nothing past the first can be read by position. The stage is the first element
when there is one; when there is not, the value or the close or the probability
slides into first place, and each of those is recognisable as not a stage.

Two things follow.

- `accounts.closed_stages` lists the stages that mean the deal is over, and
  everything else counts as live. The closed list rather than the open one, so
  a stage added to the CRM later shows up wrongly rather than disappearing
  silently.
- **The second element is a dollar amount.** `ranger accounts survey` prints
  the stage and never any other element, which is a guarantee of shape rather
  than a pattern that might not match. It also withholds any value that looks
  like an amount, including a bare integer, because the export writes `40000`
  with no currency symbol.

### The activity section

The one thing parsed strictly. Every entry is a heading:

```markdown
## Activity
### 2026-09-04 | Call | Rod Illes
Film program is on for Q4. Rod owes me confirmed volumes.

### 2026-08-28 | Email
Sent the revised pricing sheet.
```

`### YYYY-MM-DD | activity_type | contact_name`, newest first, contact omitted
when unknown. Free-form markdown follows each heading. Dates are ISO and fully
populated.

Three things that follow from the real data:

- **Only headings inside `## Activity` count.** A date in the metadata or in
  prose is not a contact, so a renewal date cannot make a dead account look
  freshly worked.
- **`next_action` and `next_action_date` are never read.** They are null in 68%
  and 94% of activities respectively. The activity date is the only signal.
- **Order in the file does not matter.** Newest-first is the convention, but the
  tools take the maximum date, so a misfiled entry cannot hide an account.

### Statuses that change the reading

- `- **Status:** UNCONFIRMED` marks a note reconstructed from the activity log
  rather than the accounts export. Several are near-duplicates of real
  accounts, so the quiet check sets them aside and says how many. Configurable
  as `accounts.skip_statuses`.
- An account with no `## Activity` section at all is reported as **never
  touched**, separately from lapsed accounts. Mixed together it would sort to
  the top on staleness and bury the accounts that actually lapsed.

### Size

Notes run from a few hundred characters to about 25,000. Account recall returns
a **digest**: the metadata, clipped prose sections, and the most recent
activities with their bodies clipped. The whole note never enters the
conversation. Bounds live under `[recall]` in the config.

## Knowledge

Loaded whole into context on every turn, per Amendment C. There is no tool to
fetch it and there will not be one. The config expects `company.md`,
`products.md`, `icp.md`, `competitors.md`, `playbook.md`; extras load after,
alphabetically.

An empty Knowledge folder is fine and silent. The system prompt says once that
Ranger does not know the business, rather than warning on every startup.

## Memory

`Ranger/memory/facts.md`, one fact per bullet:

```markdown
## Operator
- 2026-09-07 | Chris covers Texas and Oklahoma.

## Vocabulary
- The film program means the Q4 retort conversion at Illes.
```

A date prefix is what Ranger writes; a bare bullet is what you write by hand,
and both are read. The file is read fresh every turn, so an edit takes effect
immediately and a deleted line stays deleted.

**What goes here and what does not.** If a CRM export could overwrite it, it
belongs in the account note. `scripts/build_vault.py` regenerates account notes,
so a fact stored there is destroyed on the next refresh. Memory is for facts
about you: preferences, standing decisions, what your words mean. `remember`
refuses account-shaped facts and says so.

Where memory and an account note disagree about an account, the note is right.

## Ranger's own folders

Not seeded by hand. Ranger creates these files and they stay readable and
correctable in Obsidian.

| Folder | Holds | Tier |
| ------ | ----- | ---- |
| `Ranger/drafts/` | Drafts written and held, never sent | 2 |
| `Ranger/memory/` | Durable facts, one per file | 4 |
| `Ranger/inbox/` | Notices surfaced and not yet cleared, one file per notice | 5 |
| `Ranger/log/` | The audit trail, append only | 6 |

## The marker, and what Ranger appends

Every account note carries this line once:

```
<!-- ranger:below — everything above this line is CRM export, regenerable.
     Ranger appends only below. Nothing above is ever modified. -->

## Ranger Context
```

**Only `<!-- ranger:below` is load-bearing.** Everything after it — the rest of
the comment, the heading, its wording — is prose you can reflow, reword or
rename in Obsidian without breaking anything. The split is on that token and
nothing else, and there is a test that reflows the comment four different ways
and asserts the split lands in the same byte.

Put it in with:

```powershell
ranger accounts migrate --dry-run    # says what would change
ranger accounts migrate
```

The dry run also reports the line endings already in the vault:

```
  dry run, nothing written
  line endings: 1 LF, 2 CRLF, 1 CR, 1 mixed
    mixed: Pegasus Logistics.md

  5 migrated, 0 already marked
```

Ranger never converts what is already on disk, so this decides nothing — it is
there because the vault came out of four separate CRM exports and nothing
guarantees they agreed. **A mixed count above zero is worth a look before you
migrate rather than after.** What Ranger appends matches whatever each note
already uses, so a CRLF note does not grow LF-only lines.

Idempotent. A second run finds a marker everywhere and changes nothing. A note
that somehow has two markers is refused rather than repaired, because there is
no way to know which one you meant.

### What Ranger writes below it

`### YYYY-MM-DD | note | source` — the same activity format the export uses, so
both parsers see one timeline. `ranger account_recall` shows a call filed
yesterday next to the export's own history, and the morning brief counts it, so
filing "spoke to Rod today" stops that account looking quiet.

### Before you ever rebuild

```powershell
ranger vault-guard
```

Exit 0 means a rebuild would destroy nothing. Exit 1 means it would, and it
names the notes. `build_vault.py` should call this before it writes.

### The undo

```powershell
ranger snapshot init     # once
ranger snapshot show     # is it set up, and has it a remote
ranger snapshot now      # take one by hand
```

The heartbeat commits daily with the date as the message. **Local only.** The
snapshot refuses to run if the repository has a remote, and says why: this vault
holds customer email, pricing and material under NDA, and it never leaves the
machine.

### What it commits, and why it is an allow list

**Only these:** `Accounts/`, `Knowledge/`, `History/`, `Ranger/memory/`,
`Ranger/drafts/`, `Ranger/inbox/`, and the markdown sitting directly in
`Ranger/` (aliases, the kill switch, the brief's own record). Everything else
is ignored.

The first version of this was an exclusion list, and it excluded `Ranger/log/`
because the build plan named it and nothing else, because nothing else was
named. The first `snapshot init` on the real vault committed the **live Chrome
profile** in `Ranger/browser/` — cookies, autofill, account databases, about a
gigabyte of browser internals — plus `.obsidian/`, an 11MB deck and every PDF
in a resources folder. Local repository, no remote, so nothing left the machine.
It still had no business in git history.

An exclusion list can only exclude what somebody thought of, and a vault grows
folders nobody thought of. So the question is not "what should be kept out" but
"what is this backup for", and the answer is the six trees above.

`ranger snapshot init` **rewrites `.gitignore` every time.** A stale ignore file
is how a browser profile gets committed by the version that knew better. Vault
specific exclusions of your own go in `.git/info/exclude`, which Ranger never
touches.

### The size ceiling

No single file over `vault.snapshot_max_file_mb` (default 5) is committed, and
skipped files are **named** in the result rather than silently dropped — a file
too big to snapshot is a file with no undo, and you have to be able to know
which. `snapshot init` refuses a first snapshot over
`vault.snapshot_max_total_mb` (default 200) and names the largest offenders
without committing anything.

The ceiling is the half that catches the next thing nobody named.

### Seeing what is there

```powershell
ranger snapshot show
```

Prints tracked file count, size on disk, the last snapshot date, and anything
currently too big to snapshot. So the state is visible without running git by
hand, which is how a gigabyte went unnoticed the first time.

### If a snapshot ever takes something it should not

Delete the vault's `.git` folder and run `ranger snapshot init` again. The
history is local, has no remote, and is disposable by design: nothing is lost
except the undo, and the undo starts again from the next commit.

## What Ranger can read back

Every write path should have a read path. It has not always: `draft_and_hold`
shipped without any way to open a draft again, and `parse_note` was scoped to
`## Activity` while `scan_note` scanned the whole note. Both were the same
shape, so this table is kept current.

| written | by | read back by |
| --- | --- | --- |
| `Accounts/*.md` (append) | `file_to_account` | `account_recall` |
| `Ranger/drafts/` | `draft_and_hold` | `list_own_files`, `read_own_file` |
| `Ranger/memory/facts.md` | `remember`, `forget` | `read_own_file`, and the prompt every turn |
| `Ranger/inbox/` | the heartbeat | `list_own_files`, `read_own_file` |
| `Ranger/log/<date>.md` | `AuditLog.write` | **nothing** — build-plan item K |
| `Ranger/quiet-seen.md` | the brief | **nothing** — read by the brief, not by the model |
| `Ranger/dormant.md` | `ranger dormant` | **nothing** — the model cannot list what was set aside |
| `Ranger/aliases.md` | `ranger alias add` | **nothing** — applied at read time, never listed |
| `Ranger/paused.md` | `ranger pause` | **nothing** — the model cannot see the kill switch |
| `History/` | nothing yet | **nothing** — neither half exists |

The five write-only entries are known and none is a hole in the same sense as
drafts was: four are Ranger's own bookkeeping that the code consults directly,
and `History/` has no write path yet either. Reading the audit log is build-plan
item K and is a decision on its own, not a gap to close in passing.

### Filing a draft

"File the Telly draft into the account" is `read_own_file` then
`file_to_account`. Two existing tools, no new capability, no new path into the
core.

**The draft is not deleted afterwards, and filing does not clear it.** Two
separate actions. "File that and clear it" is two tool calls, which is what it
looks like; a draft vanishing from the panel as a side effect of filing would
be a surprise, and a surprise in a delete-shaped direction is the worst kind.

### Clearing a draft

```powershell
ranger drafts show       # what is held
ranger drafts clear Telly
ranger drafts cleared    # what has been cleared
```

Also a × beside each draft in the panel, and `clear_draft` for the model. All
three run the same tool: the browser holds no idea of what clearing means.

**Clearing MOVES the draft to `Ranger/drafts/cleared/`. It never deletes it.**
Delete-never is the property the whole `Accounts/` append design rests on — it
is why a snapshot had to exist before Ranger could write to an account note —
and it is not being weakened so a panel looks tidier. A cleared draft still
lists (`list_own_files` with `cleared`, or `ranger drafts cleared`) and still
reads by name. Nothing becomes unreachable.

Clearing twice is idempotent rather than an error, and a name matching two
drafts returns the candidates and clears nothing: clearing the wrong draft is
worse than asking which one.

### Nothing gains an unlink

`ranger/` deletes a file in exactly three places, and a test asserts that the
set has not grown:

| where | what | why it is allowed |
| --- | --- | --- |
| `schedule.py` | a temp XML handed to `schtasks` | outside the vault, removed the moment it is read |
| `server.py` | `Ranger/server.json` | the running server's lock. Runtime state, not content, and leaving it makes the next start think a server is up |
| `vault.py` | the `.ranger-tmp` scratch file | only when a write failed. Ranger made it seconds earlier and nobody has seen it |

**A fourth is a conversation, not a commit.**

