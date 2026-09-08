"""Clearing a draft: a move, never a delete.

The panel fills up and drafts need to leave it. The mechanism is a move to
`Ranger/drafts/cleared/`, and that is not a detail of the implementation, it is
the point. **Delete-never is the property the whole `Accounts/` append design
rests on** — it is why a snapshot had to exist before Ranger could write to an
account note at all — and it is not being weakened so a panel looks tidier.

So the assertions here are mostly about what is still true afterwards: the file
exists, it lists on request, it reads by name, and nothing anywhere has grown
an `unlink`.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from ranger.ownfiles import CLEARED, clear, listing, read
from ranger.vault import Vault, VaultError, VaultWriteDenied

FIXTURES = Path(__file__).parent / "fixtures" / "vault"

DRAFT = """\
---
created: {created}
title: {title}
status: draft, not sent
---
{body}
"""


@pytest.fixture
def seeded(vault_root, config):
    for name in ("Accounts", "Knowledge"):
        shutil.rmtree(vault_root / name, ignore_errors=True)
        shutil.copytree(FIXTURES / name, vault_root / name)
    (config.vault.drafts / "2026-09-08-telly-follow-up.md").write_text(
        DRAFT.format(created="2026-09-08", title="Telly follow up",
                     body="Dana, pricing lands Thursday."),
        encoding="utf-8",
    )
    (config.vault.drafts / "2026-09-02-illes-pricing.md").write_text(
        DRAFT.format(created="2026-09-02", title="Illes pricing", body="numbers"),
        encoding="utf-8",
    )
    return config


@pytest.fixture
def vault(seeded):
    return Vault(seeded.vault)


@pytest.fixture
def registry(seeded, vault):
    from ranger.toolset import build_registry

    return build_registry(seeded, vault, today=lambda: date(2026, 9, 8))


async def run(registry, tool, **payload):
    return await registry.run(tool, payload)


def names(files) -> list[str]:
    return [item.name for item in files]


# -- the mechanism ----------------------------------------------------------


def test_clearing_moves_the_file_and_does_not_delete_it(vault, seeded):
    outcome, _ = clear(vault, seeded, "Telly")

    assert outcome is not None
    moved = seeded.vault.drafts / CLEARED / "2026-09-08-telly-follow-up.md"
    assert moved.is_file(), "the draft was not moved"
    assert not (seeded.vault.drafts / "2026-09-08-telly-follow-up.md").exists()
    assert "pricing lands Thursday" in moved.read_text(encoding="utf-8")


def test_the_filename_is_preserved(vault, seeded):
    outcome, _ = clear(vault, seeded, "Telly")

    assert outcome.name == "2026-09-08-telly-follow-up.md"
    assert outcome.now.endswith("cleared/2026-09-08-telly-follow-up.md")


def test_a_cleared_draft_is_gone_from_the_default_listing(vault, seeded):
    clear(vault, seeded, "Telly")

    assert names(listing(vault, seeded, "drafts")) == ["2026-09-02-illes-pricing.md"]


def test_a_cleared_draft_is_listed_on_request(vault, seeded):
    """Nothing becomes unreachable. That is the difference between moving a
    file and deleting one."""
    clear(vault, seeded, "Telly")

    assert names(listing(vault, seeded, "drafts", cleared=True)) == [
        "2026-09-08-telly-follow-up.md"
    ]


def test_a_cleared_draft_can_still_be_read_by_name(vault, seeded):
    clear(vault, seeded, "Telly")

    found, text, _ = read(vault, seeded, "drafts", "Telly")

    assert found is not None
    assert "pricing lands Thursday" in text


def test_clearing_twice_is_idempotent_not_an_error(vault, seeded):
    """The second click on a button is not a mistake."""
    clear(vault, seeded, "Telly")

    outcome, candidates = clear(vault, seeded, "Telly")

    assert outcome is not None and outcome.already
    assert candidates == []
    assert (seeded.vault.drafts / CLEARED / "2026-09-08-telly-follow-up.md").is_file()


def test_an_ambiguous_name_clears_nothing(vault, seeded):
    """Clearing the wrong draft is worse than asking which one."""
    before = sorted(p.name for p in seeded.vault.drafts.glob("*.md"))

    outcome, candidates = clear(vault, seeded, "2026")

    assert outcome is None
    assert len(candidates) == 2
    assert sorted(p.name for p in seeded.vault.drafts.glob("*.md")) == before


def test_a_name_matching_nothing_clears_nothing(vault, seeded):
    outcome, candidates = clear(vault, seeded, "Petmate")

    assert outcome is None and candidates == []
    assert len(list(seeded.vault.drafts.glob("*.md"))) == 2


def test_the_panels_row_id_resolves(vault, seeded):
    """The button sends the vault-relative path, so the browser needs to know
    nothing about how names are matched."""
    outcome, _ = clear(vault, seeded, "Ranger/drafts/2026-09-08-telly-follow-up.md")

    assert outcome is not None


# -- the wall ---------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    ["Accounts/Illes Foods.md", "Knowledge/how-i-sell.md", "notes.md", "../escape.md"],
)
def test_a_move_cannot_reach_outside_rangers_own_folders(vault, seeded, target):
    source = seeded.vault.drafts / "2026-09-08-telly-follow-up.md"

    with pytest.raises((VaultWriteDenied, VaultError)):
        vault.move_within_ranger(source, seeded.vault.root / target)

    assert source.is_file(), "a refused move still moved the file"


def test_a_move_cannot_start_outside_rangers_own_folders(vault, seeded):
    account = seeded.vault.accounts / "Illes Foods.md"

    with pytest.raises(VaultWriteDenied):
        vault.move_within_ranger(account, seeded.vault.drafts / CLEARED / "stolen.md")

    assert account.is_file()


def test_a_move_will_not_land_on_an_existing_file(vault, seeded):
    """A move that silently replaced something would be a delete wearing a
    different name."""
    (seeded.vault.drafts / CLEARED).mkdir(parents=True)
    occupied = seeded.vault.drafts / CLEARED / "2026-09-08-telly-follow-up.md"
    occupied.write_text("something else\n", encoding="utf-8")

    with pytest.raises(VaultWriteDenied) as raised:
        clear(vault, seeded, "Telly")

    assert "would be a delete" in str(raised.value)
    assert occupied.read_text(encoding="utf-8") == "something else\n"


def test_the_audit_log_is_not_movable(vault, seeded):
    entry = seeded.vault.log / "2026-09-08.md"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("log\n", encoding="utf-8")

    with pytest.raises(VaultWriteDenied):
        vault.move_within_ranger(entry, seeded.vault.drafts / "stolen.md")


def test_nothing_in_the_vault_can_delete(vault):
    """Stated as a test because it is a promise, not a habit. Nothing in Ranger
    ever gains an unlink; if something seems to need one, that is a
    conversation, not a commit."""
    surface = {name for name in dir(vault) if not name.startswith("_")}

    assert not surface & {"delete", "remove", "unlink", "rename", "rmtree", "clear"}


#: The only places in `ranger/` that remove a file, each with the reason it is
#: allowed. **A fourth is a conversation, not a commit.** The rule the operator
#: set is that nothing gains an unlink without being asked about first, so this
#: is written as an allow list: adding one fails this test rather than passing
#: quietly.
KNOWN_UNLINKS = {
    # A temp XML handed to schtasks and removed the moment it has been read.
    # Outside the vault entirely, in the system temp directory.
    ("schedule.py", "os.unlink(handle.name)"),
    # Ranger/server.json, the running server's lock file, removed when the
    # server stops. Inside the vault but not content: it is runtime state that
    # is meaningless once the process is gone, and leaving it behind makes the
    # next start think a server is already up.
    ("server.py", "lock.unlink()"),
    # The scratch file in the account write path, unlinked only when a write
    # failed. Ranger created it seconds earlier and the operator has never seen
    # it; leaving it would litter Accounts/ with half-written notes.
    ("vault.py", "scratch.unlink(missing_ok=True)"),
}


def test_nothing_in_ranger_deletes_except_the_three_known_places():
    """The promise, kept as an allow list rather than as a habit.

    Delete-never is the property the `Accounts/` append design rests on. It is
    not enough for it to be true today: a fourth unlink appearing has to break
    a test, so that adding one is a decision somebody made out loud.
    """
    import ast

    root = Path(__file__).resolve().parent.parent / "ranger"
    found: set[tuple[str, str]] = set()
    for source in sorted(root.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call = ast.unparse(node.func)
            # Filesystem-shaped only. A bare `.remove(` is a list method, and
            # AliasFile.remove rewrites a markdown file rather than deleting
            # one -- catching those would make this test noise and noise is
            # how a real fourth unlink would get waved through.
            deletes = (
                call.endswith(".unlink")
                or call.endswith(".rmtree")
                or call.endswith(".rmdir")
                or call in {"os.remove", "unlink", "rmtree"}
            )
            if deletes:
                found.add((source.name, ast.unparse(node)))

    assert found == KNOWN_UNLINKS, (
        "the set of places ranger/ deletes has changed.\n"
        f"  new:  {sorted(found - KNOWN_UNLINKS)}\n"
        f"  gone: {sorted(KNOWN_UNLINKS - found)}"
    )


# -- through the tool -------------------------------------------------------


async def test_the_tool_clears_a_draft(registry, seeded):
    result = await run(registry, "clear_draft", name="Telly")

    assert result.ok
    assert "moved" in result.content
    assert (seeded.vault.drafts / CLEARED / "2026-09-08-telly-follow-up.md").is_file()


async def test_the_tool_says_it_is_recoverable(registry):
    """The model has to be able to tell the operator the truth about what it
    just did."""
    result = await run(registry, "clear_draft", name="Telly")

    assert "not deleted" in result.content


async def test_the_tool_asks_rather_than_guessing(registry, seeded):
    result = await run(registry, "clear_draft", name="2026")

    assert "Ask which one" in result.content
    assert len(list(seeded.vault.drafts.glob("*.md"))) == 2


async def test_the_tool_needs_a_name(registry):
    result = await run(registry, "clear_draft", name="  ")

    assert not result.ok


async def test_clearing_is_not_gated(registry):
    """Consistent with drafts not gating on creation, and safe for a stronger
    reason: the outcome is recoverable by construction."""
    tool = next(t for t in registry if t.name == "clear_draft")

    assert tool.writes and not tool.confirm


async def test_the_default_listing_through_the_tool_hides_cleared(registry):
    await run(registry, "clear_draft", name="Telly")

    live = await run(registry, "list_own_files", folder="drafts")
    gone = await run(registry, "list_own_files", folder="drafts", cleared=True)

    assert "Telly follow up" not in live.content
    assert "Telly follow up" in gone.content


# -- filing and clearing stay separate --------------------------------------


async def test_filing_does_not_clear(registry, seeded):
    """A draft vanishing from the panel as a side effect of filing would be a
    surprise, and a surprise in a delete-shaped direction is the worst kind."""
    from ranger.marker import migrate

    migrate(None, seeded.vault.accounts)

    await run(registry, "file_to_account", account="Illes", note="filed it", source="draft")

    assert (seeded.vault.drafts / "2026-09-08-telly-follow-up.md").is_file()


async def test_file_then_clear_composes(registry, seeded):
    from ranger.marker import migrate, split

    migrate(None, seeded.vault.accounts)

    await run(registry, "read_own_file", folder="drafts", name="Telly")
    await run(registry, "file_to_account", account="Illes", note="Pricing Thursday",
              source="draft")
    await run(registry, "clear_draft", name="Telly")

    note = (seeded.vault.accounts / "Illes Foods.md").read_text(encoding="utf-8")
    assert "Pricing Thursday" in split(note)[1]
    assert (seeded.vault.drafts / CLEARED / "2026-09-08-telly-follow-up.md").is_file()


# -- the panel --------------------------------------------------------------


def test_the_panel_stops_showing_a_cleared_draft(vault, seeded):
    from ranger.panel import snapshot

    before = snapshot(seeded, vault)
    assert any("Telly" in item["title"] for item in before["drafts"])

    clear(vault, seeded, "Telly")

    after = snapshot(seeded, vault)
    assert not any("Telly" in item["title"] for item in after["drafts"])
    assert len(after["drafts"]) == 1


# -- the log ----------------------------------------------------------------


def test_the_audit_log_records_what_was_cleared_and_to_where(vault, seeded):
    from ranger.audit import AuditLog

    audit = AuditLog(vault, seeded.vault.log)

    clear(vault, seeded, "Telly", audit=audit)

    written = audit.read()
    assert "draft cleared" in written
    assert "2026-09-08-telly-follow-up.md" in written
    assert "cleared/2026-09-08-telly-follow-up.md" in written, "the log does not say where"


def test_a_log_that_fails_does_not_stop_the_clear(vault, seeded):
    class Broken:
        def write(self, *args, **kwargs):
            raise OSError("the disk went away")

    outcome, _ = clear(vault, seeded, "Telly", audit=Broken())

    assert outcome is not None
    assert (seeded.vault.drafts / CLEARED / "2026-09-08-telly-follow-up.md").is_file()
