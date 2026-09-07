# Vault conventions

What to put in the vault by hand, and the shapes Tier 2 will read.

This is a proposal, not a law. It is cheap to change now and expensive to
change after forty notes exist, so push back before you seed rather than after.
Nothing here is enforced in code yet; Tier 2 is what will read it.

```
~/Obsidian/Ranger-Vault/
  Accounts/          one note per account      yours, read only
  Knowledge/         the business context      yours, read only
  Ranger/            Ranger's own folders      it writes only here
```

---

## Accounts

**One note per account. The filename is the account name.**

That is how "where are we on Illes Foods" finds the right note, so name the
file the way you say the name out loud. `Illes Foods.md`, not `illes-foods-
2026.md` or `ACC-00417.md`. Subfolders are fine; Ranger searches the whole
Accounts tree.

Everything below the front matter is free-form. Ranger reads it and answers in
a sentence or two, so write it for yourself, not for a parser. The one part
that is parsed is the activity dates.

### The activity log

**"What went quiet" reads an `## Activity` section and takes the newest date in
it.** Entries start with an ISO date. Anything after the date is yours.

```markdown
## Activity
- 2026-08-12 Call with Rod. Film program is on for Q4, waiting on volumes.
- 2026-07-30 Sent revised SupplyBox pricing.
- 2026-07-02 Intro call.
```

Newest first or oldest first, either works. Ranger takes the maximum date, not
the first line.

Three deliberate choices, so you know what you are agreeing to:

- **Only dates inside `## Activity` count.** A date anywhere else in the note is
  a note, not a contact. This is what stops "renewal due 2027-01-01" from making
  an account look freshly touched.
- **Future dates are ignored.** A booked meeting is a plan, not activity. It
  starts counting the day it happens.
- **File modification time is never used.** Fixing a typo is not contact, and
  Obsidian sync rewrites timestamps anyway.

If an account has no `## Activity` section at all, Ranger will say it cannot
tell rather than guessing, and it will not appear in the quiet list.

### Front matter

Optional, and only two fields are read:

```markdown
---
last_contact: 2026-08-12   # overrides the Activity section, for backfill
status: active             # active | paused | closed. Anything but active is
---                        # left out of the quiet list.
```

Use `last_contact` when you are importing history you do not want to retype as
an activity log. Use `status` to stop a dormant account nagging you forever.

### A whole note

```markdown
---
status: active
---

# Illes Foods

Food manufacturer, Dallas. Rod Illes is the decision maker, Marcy runs ops
and joins the technical calls.

## Where we are
Film program for Q4 is verbally agreed. Blocked on them confirming volumes,
which Rod owes me. SupplyBox pricing sent and not yet discussed.

## Activity
- 2026-08-12 Call with Rod. Film program on for Q4, waiting on volumes.
- 2026-07-30 Sent revised SupplyBox pricing.
- 2026-07-02 Intro call.
```

With `accounts.quiet_after_days = 21`, that note goes quiet three weeks after
12 August.

---

## Knowledge

Loaded whole into Ranger's context on every turn. There is no tool to fetch it
and there will not be one, so keep it to what actually changes how Ranger
answers. The config expects these five, in this order:

| File | What goes in it |
| ---- | --------------- |
| `company.md` | Who you are, what you sell, how you talk about yourselves |
| `products.md` | Products and services, what each is for, rough pricing shape |
| `icp.md` | Ideal client profile. Who is a fit, who is not, and why |
| `competitors.md` | The competitive landscape and how you position against it |
| `playbook.md` | The sales playbook. Stages, qualification, what good looks like |

Extra files are fine; they load after these five, alphabetically. `ranger
doctor` lists which of the five are still missing.

Two things to keep in mind while writing them:

- **Prose beats bullets.** This becomes prompt context, not a checklist. Write
  the way you would brief a new hire.
- **There is a budget.** `knowledge.budget_chars` is 60,000. Past that, Ranger
  says which files it left out rather than truncating one mid-sentence. That is
  the signal to start splitting these up, which is what the retrieval seam in
  `knowledge.py` is for.

Start rough. A half-page `company.md` is worth more than an empty folder, and
these are the easiest files in the vault to improve later.

---

## Ranger's own folders

You do not seed these. Ranger creates the files itself and you can read and
correct any of them in Obsidian.

| Folder | Holds | Tier |
| ------ | ----- | ---- |
| `Ranger/drafts/` | Drafts it has written and is holding for you | 2 |
| `Ranger/memory/` | Durable facts, one per file, hand-editable | 4 |
| `Ranger/inbox/` | Notices it has surfaced and you have not cleared | 5 |
| `Ranger/log/` | The audit trail. Append only | 6 |
