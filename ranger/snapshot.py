"""A daily snapshot of the vault, so a bad append is recoverable.

`Accounts/` being read-only was the undo. Ranger now appends there, and
delete-never means Ranger cannot tidy up its own mistake either, so something
has to be able to say what the file looked like yesterday. A git repository in
the vault is the cheapest version of that: invisible until it is needed, and
the thing the operator will want the one time this goes sideways.

**No remote, ever, and that is enforced rather than documented.** The vault
holds customer email, pricing and material under NDA. It is a local history and
nothing more, so the daily commit refuses to run at all if `git remote` returns
anything, and says why. This module never runs git anywhere but inside the
vault, and no path from this repository is ever passed to one of these commands.

Nothing here deletes, rewrites history, or resets. `git commit` and the reads it
needs, and that is the whole surface.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

#: What never belongs in a snapshot. The audit log is append-only and grows
#: every turn, so committing it daily would make every diff unreadable and the
#: repository large for no recovery value: it is already the thing that survives.
IGNORED = """\
# Written by Ranger. This repository is a local undo for the vault and has no
# remote, by design: the vault holds customer email, pricing and confidential
# material. Adding a remote will stop the daily snapshot from running.

Ranger/log/
Ranger/server.json
.obsidian/workspace*.json
*.ranger-tmp
"""


class SnapshotRefused(Exception):
    """Something about the repository means today's snapshot is not taken."""


@dataclass
class Snapshot:
    """The result of a daily commit attempt, for the log and the operator."""

    taken: bool
    detail: str

    def describe(self) -> str:
        return self.detail


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run git inside the vault. The cwd is always the vault, never this repo."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if check and result.returncode != 0:
        raise SnapshotRefused(
            f"git {' '.join(args)} failed in the vault: "
            f"{(result.stderr or result.stdout).strip()[:300]}"
        )
    return result


def available() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, timeout=30
        )
    except Exception as exc:
        return False, f"git is not runnable ({type(exc).__name__}: {exc})"
    if result.returncode != 0:
        return False, "git is not runnable"
    return True, result.stdout.strip()


def is_repository(root: Path) -> bool:
    return (root / ".git").is_dir()


def remotes(root: Path) -> list[str]:
    """Whatever `git remote` says. Empty is the only acceptable answer."""
    result = _git(root, "remote", check=False)
    if result.returncode != 0:
        # A repository whose remotes cannot be read is a repository whose
        # remotes are unknown, and unknown is not empty. Fail closed.
        raise SnapshotRefused(
            "could not read the vault's git remotes, so it cannot be shown to have "
            f"none: {(result.stderr or result.stdout).strip()[:200]}"
        )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def initialise(root: Path) -> str:
    """Make the vault a repository, once. Never adds a remote."""
    ok, why = available()
    if not ok:
        raise SnapshotRefused(why)
    if not root.is_dir():
        raise SnapshotRefused(f"there is no vault at {root}")

    created = False
    if not is_repository(root):
        _git(root, "init", "-q")
        created = True

    ignore = root / ".gitignore"
    if not ignore.exists():
        ignore.write_text(IGNORED, encoding="utf-8")
    elif "Ranger/log/" not in ignore.read_text(encoding="utf-8"):
        with ignore.open("a", encoding="utf-8") as handle:
            handle.write("\n" + IGNORED)

    found = remotes(root)
    if found:
        raise SnapshotRefused(
            f"the vault repository already has remotes ({', '.join(found)}). "
            "The vault holds customer email and pricing and must never be pushed "
            "anywhere. Remove them before Ranger will snapshot it."
        )
    return "created" if created else "already a repository"


def last_snapshot(root: Path) -> str:
    """The message on the most recent commit, which is a date. "" if none.

    Whether today's snapshot has been taken is read from the repository itself
    rather than from a state file beside it. One fewer thing to keep in step,
    and the two clock bugs in this build were both a second record of something
    that already had one.
    """
    result = _git(root, "log", "-1", "--format=%s", check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def commit(root: Path, when: date | None = None) -> Snapshot:
    """Today's snapshot. Refuses on a remote, and does nothing when nothing changed."""
    ok, why = available()
    if not ok:
        return Snapshot(False, why)
    if not is_repository(root):
        return Snapshot(False, f"{root} is not a git repository. Run: ranger snapshot init")

    try:
        found = remotes(root)
    except SnapshotRefused as exc:
        return Snapshot(False, str(exc))

    if found:
        # The one hard refusal. Not a warning, because a warning on a daily
        # background job is a warning nobody reads.
        return Snapshot(
            False,
            f"REFUSING to snapshot: the vault repository has a remote "
            f"({', '.join(found)}). This vault holds customer email, pricing and "
            "confidential material and is a local undo only. Remove the remote.",
        )

    try:
        status = _git(root, "status", "--porcelain")
        if not status.stdout.strip():
            return Snapshot(False, "nothing changed since the last snapshot")

        _git(root, "add", "-A")
        stamp = (when or date.today()).isoformat()
        _git(root, "-c", "user.name=Ranger", "-c", "user.email=ranger@localhost",
             "commit", "-q", "-m", stamp)
    except SnapshotRefused as exc:
        return Snapshot(False, str(exc))

    changed = len([line for line in status.stdout.splitlines() if line.strip()])
    return Snapshot(True, f"snapshot {stamp}, {changed} files")
