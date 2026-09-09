"""A daily snapshot of the vault, so a bad append is recoverable.

`Accounts/` being read-only was the undo. Jarvis now appends there, and
delete-never means Jarvis cannot tidy up its own mistake either, so something
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
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

#: What the snapshot exists to protect, named positively.
#:
#: **This is an allow list, and it is one because the deny list failed.** The
#: first version ignored `Ranger/log/` because the plan named it, and nothing
#: else, because nothing else was named. The first `snapshot init` on a real
#: vault committed a live Chrome profile: cookies, autofill, account databases,
#: a gigabyte of browser internals, plus `.obsidian/`, an 11MB deck and every
#: PDF in a resources folder. Local repository, no remote, so nothing left the
#: machine — and it still had no business being in git history.
#:
#: A deny list can only exclude what somebody thought of. This vault has folders
#: nobody writing the plan knew about, and it will grow more. So the question is
#: not "what should be kept out" but "what is this backup for", and the answer
#: is: what Jarvis writes and cannot undo, plus the operator's own notes.
INCLUDED = (
    "Accounts/",
    "Knowledge/",
    "History/",
    "Ranger/memory/",
    "Ranger/drafts/",
    "Ranger/inbox/",
    # Hand-entered figures that exist nowhere else. Small, text, and the one
    # folder here whose contents cannot be reconstructed from the CRM.
    "Ranger/gp/",
    # Dropped files and their sidecars. They hold customer pricing and margin,
    # they stay local, and a dated import folder is exactly the kind of thing
    # that is gone for good if it is not in the only undo this vault has. The
    # per-file ceiling still applies, and a file over it is named in the log
    # rather than silently skipped.
    "Ranger/imports/",
    # Tables Python computed and the panel drew. Small, text, and the record of
    # what a figure was when it was quoted.
    "Ranger/analysis/",
)

#: Markdown sitting directly in `Ranger/`, not recursively: aliases.md,
#: paused.md, and the brief's own record of what it has seen. Hand-curated or
#: unrecoverable, and small. `Ranger/browser/` is a directory and is not caught
#: by this.
INCLUDED_FILES = ("Ranger/*.md",)

#: Excluded again inside the trees above. Redundant against the allow list by
#: design: the allow list is the rule, and this is the second thing that has to
#: fail before a browser profile is committed. Ordered as the operator asked.
EXCLUDED = (
    # A live Chrome profile with session material in it. By name, first.
    "Ranger/browser/",
    # Machine generated, binary, or a cache.
    ".obsidian/",
    "*.pma",
    "*.wasm",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
    "*.db-journal",
    "*.ldb",
    "*.log",
    "Cache/",
    "Code Cache/",
    "GPUCache/",
    "Service Worker/",
    "IndexedDB/",
    "Local Storage/",
    "Session Storage/",
    "*.tmp",
    "*.ranger-tmp",
    # Source material that belongs to the operator and does not change.
    "Associated Packaging Resources/",
    # As before: append only, grows every turn, and already the thing that
    # survives. Committing it daily makes every diff unreadable.
    "Ranger/log/",
)


def ignore_file() -> str:
    """The .gitignore, built from the allow list rather than from memory.

    git cannot re-include a file whose parent directory is excluded, so each
    level is opened explicitly: ignore everything at the root, un-ignore the
    named trees, then ignore everything in `Ranger/` and un-ignore the three
    folders inside it that matter.
    """
    lines = [
        "# Written by Jarvis. This repository is a local undo for the vault and has",
        "# no remote, by design: the vault holds customer email, pricing and",
        "# confidential material. Adding a remote stops the daily snapshot.",
        "#",
        "# This is an ALLOW LIST. Everything is ignored unless it is named below.",
        "# The deny-list version committed a live Chrome profile the first time it",
        "# ran, because it could only exclude what somebody had thought of.",
        "",
        "# Everything, to start with.",
        "/*",
        "!/.gitignore",
        "",
        "# What the snapshot is for.",
    ]
    for path in INCLUDED:
        if path.startswith("Ranger/"):
            continue
        lines.append(f"!/{path}")
    lines += [
        "",
        "# Ranger's own folder, opened one level at a time: a live Chrome profile",
        "# lives in here and must never be re-included by a wildcard.",
        "!/Ranger/",
        "/Ranger/*",
    ]
    for path in INCLUDED:
        if path.startswith("Ranger/"):
            lines.append(f"!/{path}")
    for pattern in INCLUDED_FILES:
        lines.append(f"!/{pattern}")
    lines += [
        "",
        "# Excluded again inside the trees above. Redundant on purpose: two things",
        "# have to fail before a browser profile reaches a commit.",
    ]
    lines.extend(EXCLUDED)
    return "\n".join(lines) + "\n"


class SnapshotRefused(Exception):
    """Something about the repository means today's snapshot is not taken."""


@dataclass
class Snapshot:
    """The result of a daily commit attempt, for the log and the operator."""

    taken: bool
    detail: str

    def describe(self) -> str:
        return self.detail


def _git(root: Path, *args: str, check: bool = True,
         stdin: str | None = None) -> subprocess.CompletedProcess:
    """Run git inside the vault. The cwd is always the vault, never this repo.

    **The encoding is stated, and it is not cosmetic here.** Text mode without
    one uses the platform default, which is cp1252 on the operator's Windows
    machine and UTF-8 on the machine these tests usually run on. git speaks
    UTF-8 in both directions.

    Both directions matter. Reading, a vault file called `Café.md` would come
    back through `git status --porcelain` mangled. Writing, the staging list is
    piped in on stdin as a pathspec file, so that same name would be *sent* to
    git as cp1252 bytes, git would not match any file, and the note would be
    quietly missing from the day's snapshot -- a backup with a hole in it and
    nothing on screen to say so.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        input=stdin,
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
            ["git", "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
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


@dataclass
class Survey:
    """What would be committed, before it is.

    A snapshot that quietly swallows a gigabyte is not a backup, it is a
    surprise. This is what `snapshot init` prints and what the size ceiling is
    checked against.
    """

    files: list[tuple[str, int]] = field(default_factory=list)
    oversized: list[tuple[str, int]] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.files)

    @property
    def total(self) -> int:
        return sum(size for _, size in self.files)

    def largest(self, how_many: int = 5) -> list[tuple[str, int]]:
        return sorted(self.files, key=lambda item: -item[1])[:how_many]

    def describe(self) -> list[str]:
        lines = [f"{self.count} files, {megabytes(self.total)}"]
        for name, size in self.largest():
            lines.append(f"    {megabytes(size):>10}  {name}")
        for name, size in self.oversized[:5]:
            lines.append(f"    SKIPPED {megabytes(size)}  {name}")
        return lines


def megabytes(size: int) -> str:
    return f"{size / 1_048_576:.1f} MB" if size >= 1_048_576 else f"{size / 1024:.0f} KB"


def _pending(root: Path) -> list[str]:
    """Every path git would stage, with the ignore rules already applied."""
    result = _git(root, "status", "--porcelain", "-z", "--untracked-files=all")
    paths: list[str] = []
    for entry in result.stdout.split("\0"):
        if len(entry) < 4:
            continue
        status, name = entry[:2], entry[3:]
        if status == "R " or " -> " in name:  # a rename carries two paths
            name = name.split(" -> ")[-1]
        paths.append(name)
    return paths


def survey(root: Path, max_file_bytes: int) -> Survey:
    """What is about to be committed, and what is too big to be.

    The ceiling is the half that matters. A pattern list only ever catches what
    somebody thought of; a ceiling catches the next thing nobody named, which
    on the evidence is the failure mode that actually happens.
    """
    found = Survey()
    for name in _pending(root):
        path = root / name
        try:
            size = path.stat().st_size if path.is_file() else 0
        except OSError:
            size = 0
        if not path.exists():
            found.files.append((name, 0))  # a deletion, which is still a change
            continue
        if size > max_file_bytes:
            found.oversized.append((name, size))
            continue
        found.files.append((name, size))
    return found


def initialise(root: Path, *, max_file_bytes: int = 5 * 1_048_576,
               max_total_bytes: int = 200 * 1_048_576) -> tuple[str, Survey]:
    """Make the vault a repository, once, and say what the first snapshot holds.

    Never adds a remote, and refuses rather than committing something the
    operator has not seen the size of.
    """
    ok, why = available()
    if not ok:
        raise SnapshotRefused(why)
    if not root.is_dir():
        raise SnapshotRefused(f"there is no vault at {root}")

    created = False
    if not is_repository(root):
        _git(root, "init", "-q")
        created = True

    # Written every time, not only on creation: an out-of-date ignore file is
    # how a browser profile gets committed by the version that knew better.
    (root / ".gitignore").write_text(ignore_file(), encoding="utf-8")

    found = remotes(root)
    if found:
        raise SnapshotRefused(
            f"the vault repository already has remotes ({', '.join(found)}). "
            "The vault holds customer email and pricing and must never be pushed "
            "anywhere. Remove them before Jarvis will snapshot it."
        )

    seen = survey(root, max_file_bytes)
    if seen.total > max_total_bytes:
        raise SnapshotRefused(
            f"REFUSING the first snapshot: it would hold {seen.count} files and "
            f"{megabytes(seen.total)}, over the {megabytes(max_total_bytes)} ceiling.\n"
            + "\n".join(f"    {megabytes(size):>10}  {name}" for name, size in seen.largest())
            + "\n\nThe repository exists and nothing has been committed. Either exclude "
            "what does not belong or raise vault.snapshot_max_total_mb."
        )
    return ("created" if created else "already a repository"), seen


def last_snapshot(root: Path) -> str:
    """The message on the most recent commit, which is a date. "" if none.

    Whether today's snapshot has been taken is read from the repository itself
    rather than from a state file beside it. One fewer thing to keep in step,
    and the two clock bugs in this build were both a second record of something
    that already had one.
    """
    result = _git(root, "log", "-1", "--format=%s", check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def repository_size(root: Path) -> tuple[int, int]:
    """(tracked files, bytes on disk). What `snapshot show` prints.

    So the operator can see what the repository actually holds without running
    git by hand, which is how a gigabyte went unnoticed the first time.
    """
    listed = _git(root, "ls-files", "-z", check=False)
    count = len([name for name in listed.stdout.split("\0") if name])
    sizes = _git(root, "count-objects", "-v", check=False)
    on_disk = 0
    for line in sizes.stdout.splitlines():
        if line.startswith(("size:", "size-pack:")):
            on_disk += int(line.split(":")[1].strip()) * 1024
    return count, on_disk


def commit(root: Path, when: date | None = None, *,
           max_file_bytes: int = 5 * 1_048_576) -> Snapshot:
    """Today's snapshot. Refuses on a remote, skips anything over the ceiling."""
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
        seen = survey(root, max_file_bytes)
        if not seen.files and not seen.oversized:
            return Snapshot(False, "nothing changed since the last snapshot")
        if not seen.files:
            return Snapshot(
                False,
                "nothing was small enough to snapshot. Skipped: "
                + ", ".join(f"{name} ({megabytes(size)})" for name, size in seen.oversized[:5]),
            )

        # Staged by name rather than with -A, so the ceiling is enforced here
        # and not left to the ignore file to have anticipated.
        _git(root, "add", "--pathspec-from-file=-", "--pathspec-file-nul",
             stdin="\0".join(name for name, _ in seen.files))
        stamp = (when or date.today()).isoformat()
        _git(root, "-c", "user.name=Jarvis", "-c", "user.email=ranger@localhost",
             "commit", "-q", "-m", stamp)
    except SnapshotRefused as exc:
        return Snapshot(False, str(exc))

    detail = f"snapshot {stamp}, {seen.count} files, {megabytes(seen.total)}"
    if seen.oversized:
        # Reported, never silently dropped. A file too big to snapshot is a
        # file with no undo, and the operator has to be able to know which.
        detail += "; skipped " + ", ".join(
            f"{name} ({megabytes(size)})" for name, size in seen.oversized[:5]
        )
        if len(seen.oversized) > 5:
            detail += f" and {len(seen.oversized) - 5} more"
    return Snapshot(True, detail)
