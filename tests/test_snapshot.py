"""The vault's local history: the undo that replaced read-only `Accounts/`.

Ranger now appends to account notes and deletes nothing anywhere, so a bad
write has no way back unless something remembers what the file looked like
yesterday. A git repository inside the vault is that, and these tests run
against real repositories rather than a mock, because the whole value is in
what git actually does.

**The rule that is enforced rather than documented: no remote, ever.** The
vault holds customer email, pricing and material under NDA. It is a local undo
and nothing else, so the daily snapshot refuses outright the moment `git
remote` returns anything.
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
    is_repository,
    last_snapshot,
    remotes,
)

pytestmark = pytest.mark.skipif(not available()[0], reason="git is not installed")


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "Vault"
    (root / "Accounts").mkdir(parents=True)
    (root / "Ranger" / "log").mkdir(parents=True)
    (root / "Accounts" / "Illes Foods.md").write_text("# Illes\n", encoding="utf-8")
    (root / "Ranger" / "log" / "2026-09-08.md").write_text("noise\n", encoding="utf-8")
    return root


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True
    ).stdout


# -- setting it up ----------------------------------------------------------


def test_init_makes_the_vault_a_repository(vault):
    state, seen = initialise(vault)

    assert state == "created"
    assert is_repository(vault)
    assert remotes(vault) == []
    assert seen.count > 0, "init has to say what the first snapshot would hold"


def test_init_is_idempotent(vault):
    initialise(vault)

    assert initialise(vault)[0] == "already a repository"


def test_init_never_adds_a_remote(vault):
    initialise(vault)

    assert remotes(vault) == []


def test_the_log_folder_is_ignored(vault):
    """It is append-only and grows every turn, so committing it daily would
    make every diff unreadable for no recovery value: it is already the thing
    that survives."""
    initialise(vault)
    commit(vault, date(2026, 9, 8))

    tracked = git(vault, "ls-files")
    assert "Accounts/Illes Foods.md" in tracked
    assert "Ranger/log" not in tracked


def test_the_ignore_file_is_ranger_s_and_is_rewritten(vault):
    """It used to be appended to, so a hand-edit could leave a stale rule in
    place. The rule that let a browser profile through was exactly that kind of
    staleness, so this file is now Ranger's and is written whole every time.
    A vault that needs extra exclusions gets them in .git/info/exclude."""
    (vault / ".gitignore").write_text("# mine\n!Ranger/browser/\n", encoding="utf-8")

    initialise(vault)

    text = (vault / ".gitignore").read_text(encoding="utf-8")
    assert "!Ranger/browser/" not in text, "a hand-edit survived and re-included the profile"
    assert "ALLOW LIST" in text
    assert "Ranger/log/" in text


def test_init_refuses_a_repository_that_already_has_a_remote(vault):
    git(vault, "init", "-q")
    git(vault, "remote", "add", "origin", "https://example.invalid/vault.git")

    with pytest.raises(SnapshotRefused) as raised:
        initialise(vault)

    assert "must never be pushed" in str(raised.value)


# -- the daily commit -------------------------------------------------------


def test_a_snapshot_commits_what_changed(vault):
    initialise(vault)

    result = commit(vault, date(2026, 9, 8))

    assert result.taken, result.detail
    assert last_snapshot(vault) == "2026-09-08"


def test_a_second_snapshot_with_no_changes_does_nothing(vault):
    initialise(vault)
    commit(vault, date(2026, 9, 8))

    result = commit(vault, date(2026, 9, 9))

    assert not result.taken
    assert "nothing changed" in result.detail
    assert last_snapshot(vault) == "2026-09-08"


def test_an_appended_note_is_captured(vault):
    initialise(vault)
    commit(vault, date(2026, 9, 8))
    note = vault / "Accounts" / "Illes Foods.md"
    note.write_text(note.read_text(encoding="utf-8") + "### 2026-09-09 | x | call\n",
                    encoding="utf-8")

    assert commit(vault, date(2026, 9, 9)).taken

    # The undo, which is the entire point: yesterday's bytes are still there.
    # The date is the commit message, not a tag, so the previous commit is
    # addressed the way git addresses one.
    assert git(vault, "log", "--format=%s").split() == ["2026-09-09", "2026-09-08"]
    yesterday = git(vault, "show", "HEAD~1:Accounts/Illes Foods.md")
    assert "2026-09-09" not in yesterday
    assert "# Illes" in yesterday


def test_a_snapshot_of_a_folder_that_is_not_a_repository_says_so(vault):
    result = commit(vault, date(2026, 9, 8))

    assert not result.taken
    assert "ranger snapshot init" in result.detail


# -- no remote, ever --------------------------------------------------------


def test_a_remote_appearing_stops_the_snapshot_outright(vault):
    """Not a warning. A warning on a daily background job is a warning nobody
    reads, and the thing being warned about is a vault of customer pricing one
    push from leaving the machine."""
    initialise(vault)
    git(vault, "remote", "add", "origin", "https://example.invalid/vault.git")

    result = commit(vault, date(2026, 9, 8))

    assert not result.taken
    assert "REFUSING" in result.detail
    assert "origin" in result.detail
    assert last_snapshot(vault) == ""


def test_a_remote_added_later_stops_further_snapshots(vault):
    initialise(vault)
    commit(vault, date(2026, 9, 8))
    (vault / "Accounts" / "Rusty.md").write_text("# Rusty\n", encoding="utf-8")
    git(vault, "remote", "add", "backup", "https://example.invalid/x.git")

    result = commit(vault, date(2026, 9, 9))

    assert not result.taken
    assert "REFUSING" in result.detail
    assert last_snapshot(vault) == "2026-09-08"


def test_remotes_that_cannot_be_read_count_as_present(vault, monkeypatch):
    """Fail closed. A repository whose remotes are unknown is not a repository
    that has been shown to have none."""
    initialise(vault)

    real = subprocess.run

    def refuse(args, **kwargs):
        if args[:2] == ["git", "remote"]:
            return subprocess.CompletedProcess(args, 128, "", "fatal: not a git repository")
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", refuse)

    assert not commit(vault, date(2026, 9, 8)).taken


# -- through the heartbeat --------------------------------------------------


async def test_the_heartbeat_check_is_silent_when_it_works(vault):
    from ranger.heartbeat import VaultSnapshot

    initialise(vault)
    check = VaultSnapshot(root=vault)

    assert await check.run() is None
    assert last_snapshot(vault) == date.today().isoformat()


async def test_the_heartbeat_check_writes_a_notice_when_a_remote_appears(vault):
    """The one case worth interrupting for: there is no undo any more."""
    from ranger.heartbeat import VaultSnapshot

    initialise(vault)
    git(vault, "remote", "add", "origin", "https://example.invalid/x.git")

    notice = await VaultSnapshot(root=vault).run()

    assert notice is not None
    assert "remote" in notice.title
    assert "no undo" in notice.body


def test_it_is_not_due_twice_in_a_day(vault):
    """Read from the repository itself rather than a state file beside it. Both
    clock bugs in this build were a second record of something that already had
    one."""
    from datetime import datetime

    from ranger.heartbeat import VaultSnapshot

    initialise(vault)
    check = VaultSnapshot(root=vault)
    when = datetime(2026, 9, 8, 6, 0)

    assert check.due(when, None) is True
    commit(vault, date(2026, 9, 8))
    assert check.due(when, None) is False
    assert check.due(datetime(2026, 9, 9, 6, 0), None) is True


def test_it_is_not_due_before_the_vault_is_a_repository(vault):
    from datetime import datetime

    from ranger.heartbeat import VaultSnapshot

    assert VaultSnapshot(root=vault).due(datetime(2026, 9, 8, 6, 0), None) is False
