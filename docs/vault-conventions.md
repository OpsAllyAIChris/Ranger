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
4. Optional `## Opportunities`: `### <name>`, a pipe-delimited meta line, then
   `- **Label:** value` lines.
5. `## Activity`.

Only the metadata block counts as metadata. `- **Label:** value` lines below a
`##` heading belong to that section, so an opportunity's `- **Owner:**` is not
mistaken for an account field.

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
| `Ranger/inbox/` | Notices surfaced and not yet cleared | 5 |
| `Ranger/log/` | The audit trail, append only | 6 |
