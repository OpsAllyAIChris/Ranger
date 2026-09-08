# Amendments

What Ranger may and may not do to the operator's vault, dated, with the reason
each revision was made. Written down because a rule with no recorded reason gets
relaxed by the next person who finds it inconvenient — including a later version
of the assistant that wrote it.

Enforcement lives in `ranger/vault.py` and `ranger/marker.py`, not here. This
file says what was decided; those say what is true.

---

## Amendment D, revision 2 — 2026-09-08

> Ranger may **CREATE** files under `Ranger/` and `History/`.
> It may **MODIFY** files only under `Ranger/`.
> It may **APPEND** below the `<!-- ranger:below` marker in `Accounts/`, and
> modify nothing above it.
> It may **DELETE** nothing, anywhere.
> `Knowledge/` remains fully read-only.

### Why it was revised

Ranger had the context and could not file it. It wrote a good note, saved it to
`Ranger/drafts/`, and handed the operator a copy-paste chore. That gap is the
difference between Ranger knowing the accounts and Ranger knowing the CRM
export, and it got wider every week.

Revision 1 forbade all writes into `Accounts/` because `build_vault.py`
regenerates those notes from a CRM export: anything Ranger wrote there would be
destroyed on the next refresh. The operator has since decided not to re-run
`build_vault.py` over the populated vault, which makes writing there viable.

**Viable is not safe on its own.** Four things make it safe, and the amendment
depends on all four:

1. **The marker.** Every account note carries `<!-- ranger:below` once.
   Everything above it belongs to the export and is regenerable; everything
   below belongs to Ranger and is not. A future export replaces the half above
   and leaves the half below alone, so the decision not to rebuild can be
   reversed later without hand-reconciling 69 notes.
2. **The append guard.** `Vault.append_below_marker` is the only path that
   writes into `Accounts/`. It denies a note with no marker, denies a note with
   two, hashes the bytes above the marker before and after, and writes
   atomically so an interrupted write leaves the original intact rather than
   half of each. A check that raises counts as a denial.
3. **The rebuild guard.** `ranger vault-guard` reports what a rebuild would
   destroy and exits non-zero if the answer is anything. The operator's
   intention not to rebuild is the safety property today, and intention is not
   enforcement: every real bug in this build came from something true by intent
   rather than in code.
4. **Snapshots.** Read-only `Accounts/` *was* the undo. `ranger snapshot init`
   makes the vault a local git repository and the heartbeat commits daily, so
   there is a way back from a bad append. Local only: the snapshot refuses to
   run if the repository has a remote, because the vault holds customer email,
   pricing and material under NDA.

### What did not change

**Delete nothing, anywhere.** Create-only is what makes accumulated history
trustworthy. Ranger being unable to remove its own past notes is a feature, and
it is why item 4 above had to exist before item 2 shipped.

**`Knowledge/` is read-only.** It is the operator's own writing about how they
work. Nothing Ranger produces belongs in it.

**The gate does not fire on filing into an existing account.** Settled
deliberately, not skipped. Filing happens often enough that a confirmation card
would become a reflex within a week, and a card clicked without reading
manufactures a record of review that did not happen. Creating a *new* account
is a different act and still gates. What makes the ungated version safe is that
it can only add, only below the marker, and only to a note the export already
wrote.

---

## Amendment D, revision 1 — superseded

> Ranger may create files under `Ranger/` and `History/`, modify files only
> under `Ranger/`, and delete nothing. `Accounts/` and `Knowledge/` are
> read-only.

Superseded on 2026-09-08 by revision 2. `Accounts/` is no longer fully
read-only; everything else stands.
