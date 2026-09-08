"""The line that splits a CRM export from Ranger's own context.

Account notes now hold two things with different owners. Everything above the
marker belongs to `build_vault.py` and is regenerable; everything below belongs
to Ranger and is not. The promise being made to the operator is that Ranger
never modifies the half above, and these are the tests that hold it.

The one that matters most is `test_a_hand_edited_comment_still_splits_in_the
_same_place`. The token is a constant shared by the code that writes the marker
and the code that reads it, which is fine. What would not be fine is the two
sharing an assumption about the text *around* it, because the operator reads
these notes in Obsidian and will reword or reflow that comment eventually. That
is the shape of the bug that flattened the training notebook: a producer and a
checker agreeing with each other and with nothing outside.
"""

from __future__ import annotations

from datetime import date

import pytest

from ranger.marker import (
    BLOCK,
    TOKEN,
    MarkerError,
    count,
    digest,
    entry,
    has_context,
    marked,
    migrate,
    notes_with_context,
    rebuild_refusal,
    split,
)

EXPORT = """\
# Illes Foods

- **Tier:** 1
- **Status:** Active

## Activity

### 2026-09-04 | Call | Rod Illes
Talked about the retort line.
"""


def note(text: str = EXPORT, *, marker: bool = True) -> str:
    return text + ("\n" + BLOCK if marker else "")


# -- the split --------------------------------------------------------------


def test_the_split_lands_on_the_token():
    above, below = split(note())

    assert above == EXPORT + "\n"
    assert below.startswith(TOKEN)


def test_a_note_with_no_marker_is_refused_not_repaired():
    """Guessing where the line goes is how a tool that may only append ends up
    rewriting an export."""
    with pytest.raises(MarkerError) as raised:
        split(note(marker=False))

    assert "no" in str(raised.value)
    assert "ranger accounts migrate" in str(raised.value)


def test_two_markers_are_refused():
    with pytest.raises(MarkerError) as raised:
        split(note() + BLOCK)

    assert "2" in str(raised.value)


def test_a_hand_edited_comment_still_splits_in_the_same_place():
    """The one that matters.

    Four notes whose marker comments have been reflowed, rewrapped, reworded and
    had the heading renamed by hand in Obsidian. Only the token is the same. All
    four must split at exactly the same byte, or the guard is holding an
    assumption about prose rather than a rule about a token.
    """
    variants = [
        # As written by the migration.
        BLOCK,
        # Reflowed onto one line by an editor.
        "<!-- ranger:below — everything above this line is CRM export, regenerable. "
        "Ranger appends only below. Nothing above is ever modified. -->\n\n## Ranger Context\n",
        # Reworded, and the heading renamed.
        "<!-- ranger:below\n     do not touch anything above here, it comes from the CRM\n-->\n"
        "\n## Chris + Ranger notes\n",
        # Cut down to almost nothing, and no heading at all.
        "<!-- ranger:below -->\n",
    ]

    for variant in variants:
        text = EXPORT + "\n" + variant
        above, below = split(text)

        assert above == EXPORT + "\n", f"split moved for: {variant[:40]!r}"
        assert below.startswith(TOKEN)
        assert digest(above) == digest(EXPORT + "\n")


def test_the_marker_travels_with_the_half_ranger_owns():
    """So rewriting the export half never has to reproduce it."""
    above, below = split(note())

    assert TOKEN not in above
    assert TOKEN in below


def test_marked_is_true_only_for_exactly_one():
    assert marked(note()) is True
    assert marked(note(marker=False)) is False
    assert marked(note() + BLOCK) is False


# -- has this note got anything in it? --------------------------------------


def test_a_freshly_migrated_note_holds_no_context():
    """A marker and a heading is what the migration leaves. Counting that as
    context would make the rebuild guard refuse a vault Ranger has never
    written to."""
    assert has_context(note()) is False


def test_a_note_with_an_appended_entry_holds_context():
    text = note() + entry("Rod wants pricing by Thursday", "call", date(2026, 9, 8))

    assert has_context(text) is True


def test_an_unmarked_note_holds_no_context():
    assert has_context(note(marker=False)) is False


# -- the entry format -------------------------------------------------------


def test_an_entry_is_in_the_format_the_notes_already_use():
    """So the parsers in accounts.py keep working over what Ranger writes."""
    line = entry("Rod wants pricing", "call", date(2026, 9, 8)).strip()

    assert line == "### 2026-09-08 | Rod wants pricing | call"


def test_a_note_containing_a_newline_cannot_forge_a_second_heading():
    """An entry is exactly one line. A note carrying a newline would otherwise
    produce a fragment on its own line that parses as a different activity, and
    the text being filed is untrusted content like everything else."""
    line = entry("first\n### 2026-01-01 | forged | x", "call", date(2026, 9, 8))

    assert line.strip().splitlines() == [
        "### 2026-09-08 | first ### 2026-01-01 | forged | x | call"
    ]


def test_an_appended_entry_is_read_back_by_the_account_parser():
    """The format claim, checked against the parser rather than asserted.

    `scan_note` already counted filed entries, because it scans headings across
    the whole note, so the quiet check and the morning brief were right from the
    start. `parse_note` was not: its activity list was scoped to `## Activity`,
    so recall would have shown a call filed yesterday nowhere near the account's
    history. Both now see one timeline.
    """
    from ranger.accounts import parse_note, scan_note

    text = note() + entry("Rod wants pricing by Thursday", "call", date(2026, 9, 8))
    parsed = parse_note(text, "Illes Foods", None)

    assert [a.date for a in parsed.activities] == [date(2026, 9, 8), date(2026, 9, 4)]
    assert parsed.activities[0].kind == "Rod wants pricing by Thursday"

    # And the cheap pass agrees, which is what the brief ranks on.
    scan = scan_note(text, "Illes Foods", None)
    assert scan.last_activity == date(2026, 9, 8)
    assert scan.activity_count == 2


def test_the_timeline_is_split_on_the_marker_not_on_the_heading_name():
    """The heading is editable prose. Parsing by its name would work until the
    first time the operator renamed it in Obsidian."""
    from ranger.accounts import parse_note

    renamed = EXPORT + "\n<!-- ranger:below -->\n\n## Chris + Ranger notes\n"
    text = renamed + entry("Rod wants pricing", "call", date(2026, 9, 8))

    parsed = parse_note(text, "Illes Foods", None)

    assert [a.date for a in parsed.activities] == [date(2026, 9, 8), date(2026, 9, 4)]


# -- the migration ----------------------------------------------------------


@pytest.fixture
def accounts(tmp_path):
    folder = tmp_path / "Accounts"
    folder.mkdir()
    for name in ("Illes Foods", "Rusty's BBQ", "Pegasus Logistics"):
        (folder / f"{name}.md").write_text(EXPORT, encoding="utf-8")
    return folder


def test_the_migration_marks_every_note(accounts):
    report = migrate(None, accounts)

    assert len(report.migrated) == 3
    assert report.already == []
    for note_path in accounts.glob("*.md"):
        assert count(note_path.read_text(encoding="utf-8")) == 1


def test_a_second_run_adds_nothing(accounts):
    migrate(None, accounts)
    before = {p.name: p.read_bytes() for p in accounts.glob("*.md")}

    report = migrate(None, accounts)

    assert report.migrated == []
    assert len(report.already) == 3
    assert {p.name: p.read_bytes() for p in accounts.glob("*.md")} == before


def test_the_migration_does_not_repair_a_double_marked_note(accounts):
    """There is no way to know which of the two the operator meant."""
    broken = accounts / "Illes Foods.md"
    broken.write_text(EXPORT + BLOCK + BLOCK, encoding="utf-8")
    before = broken.read_bytes()

    report = migrate(None, accounts)

    assert broken.read_bytes() == before
    assert [name for name, _ in report.refused] == ["Illes Foods.md"]


def test_a_dry_run_changes_nothing(accounts):
    before = {p.name: p.read_bytes() for p in accounts.glob("*.md")}

    report = migrate(None, accounts, dry_run=True)

    assert len(report.migrated) == 3
    assert {p.name: p.read_bytes() for p in accounts.glob("*.md")} == before


def test_the_migration_never_modifies_what_was_there(accounts):
    migrate(None, accounts)

    for note_path in accounts.glob("*.md"):
        above, _ = split(note_path.read_text(encoding="utf-8"))
        assert above.startswith(EXPORT)


def test_a_note_already_ending_in_a_blank_line_does_not_gain_three(accounts):
    (accounts / "Illes Foods.md").write_text(EXPORT + "\n", encoding="utf-8")

    migrate(None, accounts)

    text = (accounts / "Illes Foods.md").read_text(encoding="utf-8")
    assert "\n\n\n" not in text


# -- the rebuild guard ------------------------------------------------------


def test_a_migrated_but_unwritten_vault_does_not_block_a_rebuild(accounts):
    migrate(None, accounts)

    assert notes_with_context(accounts) == []
    assert rebuild_refusal(accounts) == ""


def test_one_note_with_context_blocks_the_rebuild(accounts):
    migrate(None, accounts)
    target = accounts / "Rusty's BBQ.md"
    target.write_text(
        target.read_text(encoding="utf-8") + entry("Ordered again", "call"),
        encoding="utf-8",
    )

    holding = notes_with_context(accounts)

    assert [name for name, _ in holding] == ["Rusty's BBQ.md"]
    refusal = rebuild_refusal(accounts)
    assert "REFUSING" in refusal
    assert "Rusty's BBQ.md" in refusal
    assert "--rebuild-destroys-ranger-notes" in refusal


def test_the_refusal_counts_every_note_and_names_the_first_few(accounts):
    migrate(None, accounts)
    for note_path in accounts.glob("*.md"):
        note_path.write_text(
            note_path.read_text(encoding="utf-8") + entry("something", "call"),
            encoding="utf-8",
        )

    refusal = rebuild_refusal(accounts)

    assert "3 account notes hold Ranger context" in refusal


def test_an_unreadable_note_counts_as_holding_context(accounts, monkeypatch):
    """Fail closed. A note that cannot be checked is not a note that has been
    cleared, and the cost of being wrong is a destroyed record."""
    migrate(None, accounts)

    real = type(accounts).read_text

    def refuse(self, *args, **kwargs):
        if self.name == "Illes Foods.md":
            raise OSError("locked by another process")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(type(accounts), "read_text", refuse)

    assert ("Illes Foods.md", -1) in notes_with_context(accounts)
    assert "unreadable" in rebuild_refusal(accounts)
