# Who is allowed to say yes, and to what

**Consequentiality depends on the caller, not on the tool alone.** That was
implicit and it needed to be explicit, because it went wrong in the obvious
way: `ranger run` printed *"nothing here can approve a gate"* and then
completed a write to an account with nobody watching.

## The two facts about a tool

Every tool carries two flags, and they mean different things:

- `writes` — it changes something in the vault.
- `confirm` — it is consequential enough that a person answers for it **even
  when they are sitting there**. One tool has this: `forget`, which removes a
  fact.

## The two callers

**At the keyboard** — voice, typing, the browser. Filing into an existing
account is gate-free here, and that was decided rather than skipped: a card on
every filed note becomes a reflex inside a week, and a card clicked without
reading manufactures a record of review that did not happen. What makes it safe
is that the operator is *there*: they see the note as it is written, and the
tool can only add, only below the marker, only to a note the export already
wrote.

**Headless** — `ranger run`, and the jobs that will be built on it. That
reasoning inverts, because the premise is gone. The operator sees what was
written only after it is permanent. So **every tool that writes stops at the
gate**, which cannot say yes: it writes the request into the inbox and waits
for a keyboard. Reads stay free, because a headless run that could not read
would be useless and reading changes nothing anybody needs to see first.

It is one property on the core — `hold_writes` — set by the caller, not a
second list of tool names kept somewhere it can drift.

## The table

| Tool | Does | At the keyboard | Headless |
| ---- | ---- | --------------- | -------- |
| `account_recall` | read | free | free |
| `analyse` | write | free | **gate** |
| `clear_draft` | write | free | **gate** |
| `draft_and_hold` | write | free | **gate** |
| `file_to_account` | write | free | **gate** |
| `forget` | write | **gate** | **gate** |
| `gross_profit` | read | free | free |
| `list_own_files` | read | free | free |
| `read_import` | read | free | free |
| `read_own_file` | read | free | free |
| `remember` | write | free | **gate** |
| `what_went_quiet` | read | free | free |
| `write_document` | write | free | **gate** |

`tests/test_headless.py` runs every writing tool through a headless run and
asserts each one holds, and a separate test asserts that list is exactly the
registry's writing tools — **so a tool added later fails until somebody
classifies it.**

## The inbox line

`ranger run` says something is waiting only when the gate actually held. It
used to say it whenever the word "held" appeared anywhere in any tool's
summary, and `what_went_quiet` reports *"3 slipping, 2 deals, 1 withheld"* — so
it announced a pending approval on two runs that only read, and said nothing on
the run that wrote to an account. A prompt that cries wolf is one the operator
learns to scroll past within a week, which is worse than not having it.

It now matches the gate's own outcome on a failed step, and names the tool that
is waiting.
