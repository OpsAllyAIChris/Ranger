"""Reading back what Ranger wrote. The other half of its own folder.

Ranger could write a draft and could not read one. Asked to file the Telly
draft into the account it said, correctly, that it had no way to pull the text
and offered to have the operator read it aloud. Honest, and a hole: the write
path landed and the read path was never wired, which is the same shape as
`parse_note` scoping activities to `## Activity` while `scan_note` scanned the
whole note.

Three folders, all under `Ranger/`, all things Ranger produced: drafts, inbox
notices, and memory. Read only. **No gate**, because reading its own output is
not a consequential act — nothing leaves the machine and nothing changes.

**What comes back is still fenced as untrusted content.** Ranger wrote the
draft, but a draft may quote a customer email the operator pasted in, and a
notice summarises account content that came from a CRM export. *Ranger having
written the file earlier does not make its contents trusted when read back.*
Trust attaches to the path the bytes travelled, not to whose hand last touched
the file, and a laundering step — write it, read it back, treat it as ours — is
exactly how a fence gets walked around.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

#: The folders Jarvis may read back, and what to call them. Deliberately a
#: fixed set rather than a path: a folder argument that could name any path is
#: a directory traversal waiting for a model to be talked into one, and the
#: vault wall should be the second line of defence here rather than the first.
FOLDERS = ("drafts", "inbox", "memory")

#: Where a cleared draft goes. It is moved here, never unlinked: delete-never
#: is the property the whole `Accounts/` append design rests on, and it is not
#: being weakened so a panel looks tidier.
CLEARED = "cleared"

#: Enough of a draft to choose between them, never the whole thing. Listing
#: twenty drafts in full would spend the context the answer needs.
SUMMARY_CHARS = 120

_FRONT_MATTER = re.compile(r"\A---\n(?P<body>.*?)\n---\n", re.DOTALL)
_FIELD = re.compile(r"^(?P<key>[a-z_]+):[ \t]*(?P<value>.*?)[ \t]*$", re.MULTILINE)
_HEADING = re.compile(r"^#[ \t]+(?P<title>.+?)[ \t]*$", re.MULTILINE)


class UnknownFolder(Exception):
    """Not one of Ranger's own folders."""


@dataclass(frozen=True)
class OwnFile:
    """One file Jarvis wrote, described well enough to pick from a list."""

    name: str
    path: Path
    relative: str
    title: str
    created: str
    summary: str
    size: int

    def line(self) -> str:
        when = f"{self.created}  " if self.created else ""
        return f"{when}{self.name}\n    {self.title or self.summary}"


def folder_path(config: Any, folder: str) -> Path:
    """The configured path for a name, or UnknownFolder."""
    name = str(folder or "").strip().lower()
    if name not in FOLDERS:
        raise UnknownFolder(
            f"{folder!r} is not one of Ranger's own folders. Known: {', '.join(FOLDERS)}."
        )
    return getattr(config.vault, name)


def describe(path: Path, root: Path) -> OwnFile:
    """Read the front matter and the first heading, and nothing more.

    Both drafts and notices are written with front matter and a `# Title`, so
    this covers everything Jarvis produces. A memory file has neither, and
    falls back to its first non-empty line, which is what a list needs anyway.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return OwnFile(path.name, path, path.name, "", "", f"could not be read: {exc}", 0)

    fields: dict[str, str] = {}
    body = text
    front = _FRONT_MATTER.match(text)
    if front:
        fields = {
            m.group("key"): m.group("value") for m in _FIELD.finditer(front.group("body"))
        }
        body = text[front.end():]

    heading = _HEADING.search(body)
    lines = [line.strip() for line in body.splitlines() if line.strip() and not line.startswith("#")]
    summary = lines[0][:SUMMARY_CHARS] if lines else ""

    return OwnFile(
        name=path.name,
        path=path,
        relative=path.relative_to(root).as_posix(),
        title=fields.get("title") or (heading.group("title") if heading else ""),
        created=fields.get("created", "")[:10],
        summary=summary,
        size=len(text),
    )


def listing(vault: Any, config: Any, folder: str, *, cleared: bool = False) -> list[OwnFile]:
    """Everything in one of Ranger's folders, newest first.

    Newest first because the question is almost always about something written
    recently: "the Telly draft" means today's, not the one from March.

    Cleared drafts are excluded by default and listable on request. Nothing
    becomes unreachable by being cleared -- that is the difference between
    moving a file and deleting one, and it is the whole point.
    """
    root = folder_path(config, folder)
    files = vault.list_markdown(root, recursive=True)
    described = [
        describe(item.path, config.vault.root)
        for item in files
        if (CLEARED in item.path.parts) == cleared
    ]
    described.sort(key=lambda item: (item.created, item.name), reverse=True)
    return described


def find(files: list[OwnFile], query: str) -> tuple[OwnFile | None, list[OwnFile]]:
    """One file, or the candidates if the answer is not one.

    Matched against the file name and the title, because the operator says
    "the Telly draft" and the file is called `2026-09-08-telly-follow-up.md`
    with `title: Telly follow up`. Exact name first, so a precise answer is
    never turned into a guess.
    """
    wanted = " ".join(str(query or "").split()).casefold()
    if not wanted:
        return None, files

    for item in files:
        # The vault-relative path is exact too. It is what the panel uses as a
        # row id, so a button click resolves without the browser having to know
        # anything about how names are matched.
        if wanted in (item.name.casefold(), item.path.stem.casefold(), item.relative.casefold()):
            return item, []

    matches = [
        item
        for item in files
        if wanted in item.name.casefold() or wanted in item.title.casefold()
    ]
    if len(matches) == 1:
        return matches[0], []
    if matches:
        return None, matches

    # Fall back to matching every word, so "telly follow up" finds a file whose
    # name has them in a different order or with different punctuation.
    words = wanted.split()
    loose = [
        item
        for item in files
        if all(word in f"{item.name} {item.title}".casefold() for word in words)
    ]
    if len(loose) == 1:
        return loose[0], []
    return None, loose


def read(vault: Any, config: Any, folder: str, name: str) -> tuple[OwnFile | None, str, list[OwnFile]]:
    """(the file, its text, the candidates when it was not one file).

    Falls back to the cleared drafts when nothing active matches, so clearing
    a draft never puts it out of reach.
    """
    files = listing(vault, config, folder)
    found, candidates = find(files, name)
    if found is None and not candidates and folder == "drafts":
        found, candidates = find(listing(vault, config, folder, cleared=True), name)
    if found is None:
        return None, "", candidates
    return found, vault.read_text(found.path), candidates


@dataclass(frozen=True)
class Cleared:
    """What a clear did, for the caller and for the log."""

    name: str
    was: str
    now: str
    already: bool = False

    def describe(self) -> str:
        if self.already:
            return f"{self.name} was already cleared, at {self.now}"
        return f"{self.name}: {self.was} -> {self.now}"


def clear(
    vault: Any, config: Any, name: str, *, audit: Any = None
) -> tuple[Cleared | None, list[OwnFile]]:
    """Move a draft out of the way. Never unlink it.

    `(what happened, candidates)`. When the name matches more than one draft
    nothing is cleared and the candidates come back instead: clearing the wrong
    draft is worse than asking which one.

    Idempotent. A draft that is already cleared reports that rather than
    failing, because the second click on a button is not an error.

    The audit line is written here rather than in each of the three callers, so
    the tool, the panel button and the CLI record the same thing. The core also
    logs every tool call, so a clear through the model appears twice, from two
    different vantage points, which is what an audit log is for.
    """
    active = listing(vault, config, "drafts")
    found, candidates = find(active, name)

    if found is None:
        already, _ = find(listing(vault, config, "drafts", cleared=True), name)
        if already is not None:
            return Cleared(already.name, already.relative, already.relative, already=True), []
        return None, candidates

    target = folder_path(config, "drafts") / CLEARED / found.name
    landed = vault.move_within_ranger(found.path, target)
    outcome = Cleared(
        name=found.name,
        was=found.relative,
        now=landed.relative_to(config.vault.root).as_posix(),
    )
    if audit is not None:
        try:
            audit.write("draft cleared", outcome.describe())
        except Exception:
            pass  # the log must never be able to stop the work
    return outcome, []
