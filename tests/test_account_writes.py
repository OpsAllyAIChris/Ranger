"""The only path that writes into `Accounts/`, and the wall around it.

Amendment D revision 2 lets Ranger append below the marker in an account note.
That is one narrow hole in a wall that was previously solid, and these are the
tests that keep it narrow.

The promise being made to the operator is that a CRM export is never modified.
That promise is worth checking rather than asserting, so the bytes above the
marker are hashed before and after every write, and several of these tests exist
only to prove the check is real by breaking it.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from ranger.marker import BLOCK, MarkerError, digest, entry, split
from ranger.vault import Vault, VaultError, VaultPathDenied, VaultWriteDenied

EXPORT = """\
# Illes Foods

- **Tier:** 1

## Activity

### 2026-09-04 | Call | Rod Illes
Talked about the retort line.
"""


@pytest.fixture
def vault(config):
    return Vault(config.vault)


@pytest.fixture
def note(config):
    """One migrated account note."""
    path = config.vault.accounts / "Illes Foods.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXPORT + "\n" + BLOCK, encoding="utf-8")
    return path


def above(path) -> bytes:
    """The export half, as bytes. Never as text: the whole promise is about
    bytes, and a helper that decoded first would be measuring what the reader
    did rather than what the writer did."""
    from ranger.marker import split_bytes

    return split_bytes(path.read_bytes())[0]


# -- the happy path, and what it must not disturb ---------------------------


def test_an_append_lands_below_the_marker(vault, note):
    vault.append_below_marker(note, entry("Rod wants pricing", "call", date(2026, 9, 8)))

    text = note.read_text(encoding="utf-8")
    _, below = split(text)
    assert "### 2026-09-08 | Rod wants pricing | call" in below


def test_a_successful_append_leaves_the_bytes_above_hash_identical(vault, note):
    """The promise, checked rather than asserted."""
    was = digest(above(note))

    for index in range(3):
        vault.append_below_marker(note, entry(f"note {index}", "call", date(2026, 9, 8)))
        assert digest(above(note)) == was


def test_appends_accumulate_rather_than_replacing(vault, note):
    vault.append_below_marker(note, entry("first", "call", date(2026, 9, 8)))
    vault.append_below_marker(note, entry("second", "email", date(2026, 9, 9)))

    text = note.read_text(encoding="utf-8")
    assert "first" in text and "second" in text


def test_the_marker_is_never_duplicated_by_appending(vault, note):
    for index in range(3):
        vault.append_below_marker(note, entry(f"note {index}", "call"))

    from ranger.marker import count

    assert count(note.read_text(encoding="utf-8")) == 1


# -- the refusals -----------------------------------------------------------


def test_a_note_with_no_marker_is_denied(vault, config):
    path = config.vault.accounts / "Unmarked.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXPORT, encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(MarkerError):
        vault.append_below_marker(path, entry("x", "call"))

    assert path.read_bytes() == before, "a denied write left something behind"


def test_a_note_with_two_markers_is_denied(vault, config):
    path = config.vault.accounts / "Doubled.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXPORT + BLOCK + BLOCK, encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(MarkerError):
        vault.append_below_marker(path, entry("x", "call"))

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "where",
    ["Knowledge/context.md", "Ranger/memory/facts.md", "History/x.md", "notes.md"],
)
def test_paths_outside_accounts_are_refused(vault, config, where):
    path = config.vault.root / where
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXPORT + "\n" + BLOCK, encoding="utf-8")

    with pytest.raises(VaultWriteDenied) as raised:
        vault.append_below_marker(path, entry("x", "call"))

    assert "account notes" in str(raised.value)


def test_a_path_outside_the_vault_is_refused(vault, tmp_path):
    outside = tmp_path / "elsewhere.md"
    outside.write_text(EXPORT + "\n" + BLOCK, encoding="utf-8")

    with pytest.raises(VaultPathDenied):
        vault.append_below_marker(outside, entry("x", "call"))


def test_traversal_out_of_accounts_is_refused(vault, config):
    (config.vault.knowledge).mkdir(parents=True, exist_ok=True)
    (config.vault.knowledge / "context.md").write_text(EXPORT + BLOCK, encoding="utf-8")

    with pytest.raises(VaultError):
        vault.append_below_marker(
            config.vault.accounts / ".." / "Knowledge" / "context.md", entry("x", "call")
        )


def test_a_note_that_does_not_exist_is_not_created(vault, config):
    """This may only add to what the export already wrote. Creating a new
    account is a different act and it gates."""
    missing = config.vault.accounts / "Brand New Co.md"
    missing.parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(VaultWriteDenied) as raised:
        vault.append_below_marker(missing, entry("x", "call"))

    assert "never creates one" in str(raised.value)
    assert not missing.exists()


def test_a_folder_is_not_a_note(vault, config):
    folder = config.vault.accounts / "Illes Foods"
    folder.mkdir(parents=True, exist_ok=True)

    with pytest.raises(VaultWriteDenied):
        vault.append_below_marker(folder, entry("x", "call"))


# -- the hash guard, proved by breaking it ----------------------------------


def test_a_write_that_would_change_the_bytes_above_is_refused(vault, note, monkeypatch):
    """The guard's whole reason for existing, exercised.

    Nothing in the real code path can produce this, which is exactly why it is
    worth checking: the cost of being wrong is a silently rewritten export, and
    a check that has never been seen to fire is a check nobody knows works.
    """
    before = note.read_bytes()
    calls = {"n": 0}

    from ranger.marker import split_bytes as real_split_bytes

    def drifting(data: bytes):
        """Honest to the real risk: composition silently loses a byte above
        the line. The first read is true, the composed one is not."""
        above_half, below = real_split_bytes(data)
        calls["n"] += 1
        if calls["n"] > 1:
            above_half = above_half.replace(b"Tier:** 1", b"Tier:** 4")
        return above_half, below

    monkeypatch.setattr("ranger.marker.split_bytes", drifting)

    with pytest.raises(MarkerError) as raised:
        vault.append_below_marker(note, entry("x", "call"))

    assert "above the marker" in str(raised.value)
    assert note.read_bytes() == before, "a refused write left something behind"


def test_a_failure_while_writing_leaves_the_note_byte_identical(vault, note, monkeypatch):
    """Not half of each. A partially rewritten account note is worse than a
    failed append, because it still looks like a note."""
    before = note.read_bytes()

    real_write = type(note).write_bytes

    def explode(self, *args, **kwargs):
        if self.name.endswith(".ranger-tmp"):
            real_write(self, *args, **kwargs)
            raise OSError("the disk went away mid-write")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(type(note), "write_bytes", explode)

    with pytest.raises(OSError):
        vault.append_below_marker(note, entry("x", "call"))

    assert note.read_bytes() == before


def test_the_note_itself_is_never_opened_for_writing(vault, note, monkeypatch):
    """Atomicity, tested as a mechanism rather than as an outcome.

    Deliberately breaking the atomic move and re-running this suite showed
    every other test still passing, because the restore in the failure path
    covered it. A restore only runs if the process is alive to run it: a
    machine losing power mid-write has no such luxury, and a half-rewritten
    account note is worse than a failed append because it still looks like a
    note. So this asserts the file is never the target of a write at all --
    only the scratch file is, and only a rename touches the note.
    """
    written: list[str] = []
    real_text = type(note).write_text
    real_bytes = type(note).write_bytes

    def record_text(self, *args, **kwargs):
        written.append(self.name + " (text mode!)")
        return real_text(self, *args, **kwargs)

    def record_bytes(self, *args, **kwargs):
        written.append(self.name)
        return real_bytes(self, *args, **kwargs)

    monkeypatch.setattr(type(note), "write_text", record_text)
    monkeypatch.setattr(type(note), "write_bytes", record_bytes)

    vault.append_below_marker(note, entry("x", "call"))

    assert written == [note.name + ".ranger-tmp"], written
    assert "Rod Illes" in note.read_text(encoding="utf-8")


def test_a_failed_append_leaves_no_scratch_file_behind(vault, note, monkeypatch):
    real_write = type(note).write_bytes

    def explode(self, *args, **kwargs):
        if self.name.endswith(".ranger-tmp"):
            real_write(self, *args, **kwargs)
            raise OSError("nope")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(type(note), "write_bytes", explode)

    with pytest.raises(OSError):
        vault.append_below_marker(note, entry("x", "call"))

    assert list(note.parent.glob("*.ranger-tmp")) == []


def test_a_check_that_raises_counts_as_denial(vault, note, monkeypatch):
    """Fail closed, the same posture as Hotword.listen()."""
    before = note.read_bytes()

    def broken(text):
        raise RuntimeError("the hash function fell over")

    monkeypatch.setattr("ranger.marker.digest", broken)

    with pytest.raises(RuntimeError):
        vault.append_below_marker(note, entry("x", "call"))

    assert note.read_bytes() == before


def test_a_note_that_is_not_utf8_is_refused(vault, config):
    path = config.vault.accounts / "Broken.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe not text at all")
    before = path.read_bytes()

    with pytest.raises(MarkerError):
        vault.append_below_marker(path, entry("x", "call"))

    assert path.read_bytes() == before


# -- through the tool -------------------------------------------------------


async def run(registry, name, **payload):
    return await registry.run(name, payload)


FIXTURES = Path(__file__).parent / "fixtures" / "vault"


@pytest.fixture
def seeded(vault_root, config):
    """The fixture vault copied into a temp vault, so tests can write to it."""
    for name in ("Accounts", "Knowledge"):
        shutil.rmtree(vault_root / name, ignore_errors=True)
        shutil.copytree(FIXTURES / name, vault_root / name)
    return config


@pytest.fixture
def registry(seeded):
    from ranger.toolset import build_registry

    return build_registry(seeded, Vault(seeded.vault), today=lambda: date(2026, 9, 8))


@pytest.fixture
def migrated(seeded):
    """The seeded vault, with every account note marked."""
    from ranger.marker import migrate

    migrate(None, seeded.vault.accounts)
    return seeded


async def test_the_tool_files_into_a_migrated_note(registry, migrated):
    result = await run(
        registry, "file_to_account",
        account="Illes", note="Rod wants pricing by Thursday", source="call",
    )

    assert result.ok, result.content
    text = (migrated.vault.accounts / "Illes Foods.md").read_text(encoding="utf-8")
    _, below = split(text)
    assert "Rod wants pricing by Thursday" in below


async def test_the_tool_refuses_an_unmigrated_note_rather_than_marking_it(registry, seeded):
    """Creating the marker on the fly is how a tool that may only append ends
    up deciding where an export ends."""
    before = (seeded.vault.accounts / "Illes Foods.md").read_bytes()

    result = await run(registry, "file_to_account", account="Illes", note="something")

    assert not result.ok
    assert "ranger accounts migrate" in result.content
    assert (seeded.vault.accounts / "Illes Foods.md").read_bytes() == before


async def test_the_tool_does_not_invent_an_account(registry, migrated):
    result = await run(
        registry, "file_to_account", account="Petmate", note="they called"
    )

    assert "No account note matches" in result.content
    assert not (migrated.vault.accounts / "Petmate.md").exists()


async def test_filing_uses_the_source_it_was_given(registry, migrated):
    await run(registry, "file_to_account", account="Illes", note="x", source="email")

    text = (migrated.vault.accounts / "Illes Foods.md").read_text(encoding="utf-8")
    assert "| x | email" in text


async def test_filing_with_no_source_still_records_where_it_came_from(registry, migrated):
    await run(registry, "file_to_account", account="Illes", note="x")

    text = (migrated.vault.accounts / "Illes Foods.md").read_text(encoding="utf-8")
    assert "| x | Jarvis" in text


async def test_the_tool_never_touches_the_export_half(registry, migrated):
    path = migrated.vault.accounts / "Illes Foods.md"
    was = digest(above(path))

    await run(registry, "file_to_account", account="Illes", note="one")
    await run(registry, "file_to_account", account="Illes", note="two")

    assert digest(above(path)) == was
