"""What the snapshot commits, against a vault shaped like the real one.

The first `snapshot init` on the operator's actual vault committed a live
Chrome profile: cookies, autofill, account databases, roughly a gigabyte of
browser internals, plus `.obsidian/`, an 11MB deck and every PDF in a resources
folder. Local repository with no remote, so nothing left the machine, and it
still had no business in git history.

The cause was an ignore list written from the build plan's wording rather than
from what is in a real vault. The plan named `Ranger/log/`, so `Ranger/log/`
was excluded and nothing else was, because nothing else was named. No test ever
looked at anything outside the repository. That is the same shape as the CRLF
bug and the flattened notebook: everything agreeing with everything except the
world.

**So this fixture is a vault, on disk, with the things that actually broke it in
it, and the assertions are made against a real git repository rather than a
mock.** The Chrome profile here is a handful of small files rather than a
gigabyte, because what is being tested is whether they are committed at all.
"""

from __future__ import annotations

import subprocess
from datetime import date

import pytest

from ranger.snapshot import (
    SnapshotRefused,
    available,
    commit,
    initialise,
    megabytes,
    repository_size,
    survey,
)

pytestmark = pytest.mark.skipif(not available()[0], reason="git is not installed")

MB = 1_048_576


@pytest.fixture
def vault(tmp_path):
    """A vault shaped like the operator's, including everything that broke."""
    root = tmp_path / "Ranger-Vault"

    # What the snapshot exists to protect.
    (root / "Accounts").mkdir(parents=True)
    for name in ("Illes Foods", "Rusty BBQ", "Pegasus Logistics"):
        (root / "Accounts" / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")
    (root / "Knowledge").mkdir()
    (root / "Knowledge" / "how-i-sell.md").write_text("voice\n", encoding="utf-8")
    (root / "History").mkdir()
    (root / "History" / "2026-09-08 call.md").write_text("notes\n", encoding="utf-8")
    for folder in ("memory", "drafts", "inbox", "log"):
        (root / "Ranger" / folder).mkdir(parents=True)
    (root / "Ranger" / "memory" / "facts.md").write_text("- fact\n", encoding="utf-8")
    (root / "Ranger" / "drafts" / "reply.md").write_text("draft\n", encoding="utf-8")
    (root / "Ranger" / "inbox" / "notice.md").write_text("notice\n", encoding="utf-8")
    (root / "Ranger" / "aliases.md").write_text("- A -> B\n", encoding="utf-8")
    (root / "Ranger" / "log" / "2026-09-08.md").write_text("audit\n", encoding="utf-8")

    # The live Chrome profile. This is the one that mattered.
    profile = root / "Ranger" / "browser" / "Default"
    (profile / "Cache" / "Cache_Data").mkdir(parents=True)
    (profile / "IndexedDB").mkdir(parents=True)
    (profile / "Cookies").write_bytes(b"SQLite format 3\x00" + b"\x00" * 4096)
    (profile / "Login Data").write_bytes(b"SQLite format 3\x00" + b"\x00" * 4096)
    (profile / "Web Data").write_bytes(b"SQLite format 3\x00" + b"\x00" * 2048)
    (profile / "History").write_bytes(b"SQLite format 3\x00" + b"\x00" * 2048)
    (profile / "Cache" / "Cache_Data" / "data_1").write_bytes(b"\x00" * 8192)
    (profile / "IndexedDB" / "store.ldb").write_bytes(b"\x00" * 1024)
    (root / "Ranger" / "browser" / "Local State").write_text("{}", encoding="utf-8")

    # Obsidian's own machinery.
    (root / ".obsidian").mkdir()
    (root / ".obsidian" / "workspace.json").write_text("{}", encoding="utf-8")
    (root / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")

    # The operator's source material.
    (root / "Associated Packaging Resources").mkdir()
    (root / "Associated Packaging Resources" / "line card.pdf").write_bytes(b"%PDF" + b"\x00" * 512)

    return root


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True
    ).stdout


def tracked(root) -> set[str]:
    return {name for name in git(root, "ls-files").splitlines() if name}


# -- the bug, directly ------------------------------------------------------


def test_the_chrome_profile_is_never_committed(vault):
    """The bug, stated as an assertion. Nothing under Ranger/browser/."""
    initialise(vault)
    commit(vault, date(2026, 9, 8))

    for name in tracked(vault):
        assert not name.startswith("Ranger/browser"), f"the Chrome profile was committed: {name}"


def test_no_cookie_or_account_database_is_committed(vault):
    """Named individually, because these are the files that make it serious
    rather than merely large."""
    initialise(vault)
    commit(vault, date(2026, 9, 8))

    committed = tracked(vault)
    for sensitive in ("Cookies", "Login Data", "Web Data"):
        assert not any(sensitive in name for name in committed), sensitive


def test_obsidian_is_never_committed(vault):
    initialise(vault)
    commit(vault, date(2026, 9, 8))

    assert not any(name.startswith(".obsidian") for name in tracked(vault))


def test_the_operators_source_material_is_never_committed(vault):
    initialise(vault)
    commit(vault, date(2026, 9, 8))

    assert not any("Associated Packaging" in name for name in tracked(vault))


def test_the_audit_log_is_never_committed(vault):
    """As before. It is append-only, grows every turn, and is already the thing
    that survives."""
    initialise(vault)
    commit(vault, date(2026, 9, 8))

    assert not any(name.startswith("Ranger/log") for name in tracked(vault))


# -- and the things it is actually for --------------------------------------


def test_everything_the_snapshot_is_for_is_committed(vault):
    initialise(vault)
    commit(vault, date(2026, 9, 8))

    committed = tracked(vault)
    for wanted in (
        "Accounts/Illes Foods.md",
        "Accounts/Rusty BBQ.md",
        "Accounts/Pegasus Logistics.md",
        "Knowledge/how-i-sell.md",
        "History/2026-09-08 call.md",
        "Ranger/memory/facts.md",
        "Ranger/drafts/reply.md",
        "Ranger/inbox/notice.md",
        "Ranger/aliases.md",
    ):
        assert wanted in committed, f"{wanted} was not snapshotted"


def test_an_account_note_can_be_recovered_from_yesterday(vault):
    """The whole point, end to end."""
    initialise(vault)
    commit(vault, date(2026, 9, 8))
    note = vault / "Accounts" / "Illes Foods.md"
    note.write_text("# Illes Foods\n### 2026-09-09 | wrong | x\n", encoding="utf-8")
    commit(vault, date(2026, 9, 9))

    assert "wrong" not in git(vault, "show", "HEAD~1:Accounts/Illes Foods.md")


def test_a_new_folder_nobody_named_is_not_committed(vault):
    """The allow list's reason for existing. A deny list would take this."""
    (vault / "Screen Recordings").mkdir()
    (vault / "Screen Recordings" / "demo.mp4").write_bytes(b"\x00" * 4096)

    initialise(vault)
    commit(vault, date(2026, 9, 8))

    assert not any("Screen Recordings" in name for name in tracked(vault))


# -- the ceiling, which catches what nobody named ---------------------------


def test_a_file_over_the_ceiling_is_skipped(vault):
    """A pattern list catches what somebody thought of. A ceiling catches the
    next thing nobody did, which on the evidence is what actually happens."""
    big = vault / "Accounts" / "Huge Account.md"
    big.write_bytes(b"x" * (2 * MB))

    initialise(vault, max_file_bytes=1 * MB)
    result = commit(vault, date(2026, 9, 8), max_file_bytes=1 * MB)

    assert result.taken
    assert "Accounts/Huge Account.md" not in tracked(vault)


def test_a_skipped_file_is_reported_not_silently_dropped(vault):
    """A file too big to snapshot is a file with no undo, and the operator has
    to be able to know which."""
    (vault / "Accounts" / "Huge Account.md").write_bytes(b"x" * (2 * MB))

    initialise(vault, max_file_bytes=1 * MB)
    result = commit(vault, date(2026, 9, 8), max_file_bytes=1 * MB)

    assert "skipped" in result.detail
    assert "Huge Account.md" in result.detail


def test_the_ordinary_notes_still_commit_alongside_a_skipped_one(vault):
    (vault / "Accounts" / "Huge Account.md").write_bytes(b"x" * (2 * MB))

    initialise(vault, max_file_bytes=1 * MB)
    commit(vault, date(2026, 9, 8), max_file_bytes=1 * MB)

    assert "Accounts/Illes Foods.md" in tracked(vault)


# -- init reports before it commits -----------------------------------------


def test_init_reports_what_the_first_snapshot_would_hold(vault):
    state, seen = initialise(vault)

    assert state == "created"
    assert seen.count > 0
    assert any("Accounts/Illes Foods.md" == name for name, _ in seen.files)
    assert not any("browser" in name for name, _ in seen.files)


def test_init_refuses_a_first_snapshot_over_the_total_ceiling(vault):
    """A snapshot that quietly swallows a gigabyte is not a backup, it is a
    surprise. This is the check that would have stopped it."""
    (vault / "Accounts" / "Big.md").write_bytes(b"x" * (3 * MB))

    with pytest.raises(SnapshotRefused) as raised:
        initialise(vault, max_file_bytes=10 * MB, max_total_bytes=2 * MB)

    assert "REFUSING the first snapshot" in str(raised.value)
    assert "Big.md" in str(raised.value)
    assert "nothing has been committed" in str(raised.value)


def test_a_refused_init_leaves_the_repository_empty(vault):
    (vault / "Accounts" / "Big.md").write_bytes(b"x" * (3 * MB))

    with pytest.raises(SnapshotRefused):
        initialise(vault, max_file_bytes=10 * MB, max_total_bytes=2 * MB)

    assert tracked(vault) == set()


def test_the_ignore_file_is_rewritten_on_every_init(vault):
    """An out-of-date ignore file is how a browser profile gets committed by
    the version that knew better."""
    initialise(vault)
    (vault / ".gitignore").write_text("# someone edited this\n", encoding="utf-8")

    initialise(vault)

    assert "ALLOW LIST" in (vault / ".gitignore").read_text(encoding="utf-8")


def test_show_reports_the_repository_size(vault):
    initialise(vault)
    commit(vault, date(2026, 9, 8))

    count, on_disk = repository_size(vault)

    assert count == len(tracked(vault))
    assert on_disk >= 0


def test_megabytes_reads_as_a_size(vault):
    assert megabytes(2 * MB) == "2.0 MB"
    assert megabytes(2048) == "2 KB"


# -- the survey is what init and commit both consult ------------------------


def test_the_survey_sees_only_what_would_be_staged(vault):
    initialise(vault)

    seen = survey(vault, 5 * MB)

    names = {name for name, _ in seen.files}
    assert "Accounts/Illes Foods.md" in names
    assert not any(name.startswith("Ranger/browser") for name in names)
    assert not any(name.startswith(".obsidian") for name in names)
