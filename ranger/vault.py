"""The vault, and the wall around it.

Amendment D. Ranger reads the whole vault and writes only under <root>/Ranger.
That is enforced here, in code, so a confused model cannot talk its way past
it. There is deliberately no delete method and no rename method anywhere in
this file. Overwriting an existing note requires an explicit flag and is
refused outright in the append-only log folder.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .config import VaultConfig


class VaultError(Exception):
    """Base class for every refusal this module makes."""


class VaultPathDenied(VaultError):
    """The path is outside the boundary the operator set."""


class VaultWriteDenied(VaultError):
    """The path is readable but Ranger is not allowed to write it."""


def _real(path: Path) -> Path:
    """Resolve symlinks too.

    A symlink inside Ranger/ pointing at Accounts/ would otherwise be a hole
    straight through the wall.
    """
    return Path(os.path.realpath(path))


def _contains(root: Path, path: Path) -> bool:
    root_real = _real(root)
    path_real = _real(path)
    return path_real == root_real or root_real in path_real.parents


@dataclass(frozen=True)
class VaultFile:
    path: Path
    relative: str
    modified: datetime
    size: int


class Vault:
    """Every read and write Ranger makes goes through one of these methods."""

    def __init__(self, config: VaultConfig) -> None:
        self.config = config

    # -- boundaries ----------------------------------------------------

    def resolve_read(self, path: str | Path) -> Path:
        """Anywhere inside the vault is readable. Nothing outside it is."""
        candidate = self._absolute(path)
        if not _contains(self.config.root, candidate):
            raise VaultPathDenied(
                f"read refused: {candidate} is outside the vault ({self.config.root})"
            )
        return candidate

    def resolve_write(self, path: str | Path) -> Path:
        """Only Ranger's own folders are writable."""
        candidate = self._absolute(path)
        if not _contains(self.config.root, candidate):
            raise VaultPathDenied(
                f"write refused: {candidate} is outside the vault ({self.config.root})"
            )
        if not any(_contains(root, candidate) for root in self.config.writable_roots):
            allowed = ", ".join(str(p) for p in self.config.writable_roots)
            raise VaultWriteDenied(
                f"write refused: {candidate} is not one of Ranger's own folders ({allowed})"
            )
        return candidate

    def is_append_only(self, path: str | Path) -> bool:
        candidate = self._absolute(path)
        return any(_contains(root, candidate) for root in self.config.append_only_roots)

    def _absolute(self, path: str | Path) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.config.root / candidate
        # resolve() without strict so paths that do not exist yet still get
        # normalised; .. segments are collapsed before the boundary check.
        return candidate.resolve()

    # -- reading -------------------------------------------------------

    def read_text(self, path: str | Path) -> str:
        target = self.resolve_read(path)
        if not target.is_file():
            raise VaultError(f"no such note: {target}")
        return target.read_text(encoding="utf-8")

    def list_markdown(self, folder: str | Path, *, recursive: bool = True) -> list[VaultFile]:
        root = self.resolve_read(folder)
        if not root.is_dir():
            return []
        pattern = "**/*.md" if recursive else "*.md"
        files: list[VaultFile] = []
        for path in sorted(root.glob(pattern)):
            if not path.is_file():
                continue
            stat = path.stat()
            files.append(
                VaultFile(
                    path=path,
                    # Forward slashes on every platform. This string ends up
                    # in the prompt and in messages the operator reads, and
                    # Obsidian writes links this way too.
                    relative=path.relative_to(self.config.root).as_posix(),
                    modified=datetime.fromtimestamp(stat.st_mtime),
                    size=stat.st_size,
                )
            )
        return files

    # -- writing -------------------------------------------------------

    def write_new(self, path: str | Path, text: str) -> Path:
        """Create a note. Refuses if something is already there."""
        target = self.resolve_write(path)
        if self.is_append_only(target):
            raise VaultWriteDenied(f"{target} is in the append-only log folder; use append()")
        if target.exists():
            raise VaultWriteDenied(f"{target} already exists; Ranger does not overwrite notes")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def overwrite(self, path: str | Path, text: str, *, allow_overwrite: bool = False) -> Path:
        """Replace one of Ranger's own notes. Never used on operator notes."""
        target = self.resolve_write(path)
        if self.is_append_only(target):
            raise VaultWriteDenied(f"{target} is in the append-only log folder; use append()")
        if target.exists() and not allow_overwrite:
            raise VaultWriteDenied(
                f"{target} exists and allow_overwrite was not set"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def append(self, path: str | Path, text: str) -> Path:
        target = self.resolve_write(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(text)
        return target

    def move_within_ranger(self, source: str | Path, target: str | Path) -> Path:
        """Move one of Ranger's own files. **Not a delete.**

        Clearing a draft moves it out of the way; it never unlinks it. Delete
        never is the property the whole `Accounts/` append design rests on --
        it is why a snapshot had to exist before Ranger could write there at
        all -- and it is not going to be weakened so a panel looks tidier.

        Both ends must be under `Ranger/`, checked through `resolve_write`, so
        this cannot become a way to move an account note somewhere it can be
        edited freely. It refuses to land on an existing file for the same
        reason `write_new` does: a move that silently replaced something would
        be a delete wearing a different name.
        """
        origin = self.resolve_write(source)
        landing = self.resolve_write(target)
        if not origin.is_file():
            raise VaultError(f"no such file: {origin}")
        if self.is_append_only(origin) or self.is_append_only(landing):
            raise VaultWriteDenied(f"{origin} is in the append-only log folder")
        if landing.exists():
            raise VaultWriteDenied(
                f"{landing} already exists; moving onto it would be a delete"
            )
        landing.parent.mkdir(parents=True, exist_ok=True)
        origin.replace(landing)
        return landing

    # -- the one exception, and it is narrow ---------------------------

    def resolve_account(self, path: str | Path) -> Path:
        """An existing account note, and nothing else that exists.

        Amendment D revision 2 lets Ranger append below the marker in
        `Accounts/`. This is where that is decided, and it is deliberately not
        an entry in `writable_roots`: everything that reads that tuple would
        then treat account notes as ordinary Ranger files, which they are not.
        Nothing here may create a note, and nothing here may touch a folder.
        """
        candidate = self._absolute(path)
        if not _contains(self.config.root, candidate):
            raise VaultPathDenied(
                f"append refused: {candidate} is outside the vault ({self.config.root})"
            )
        if not _contains(self.config.accounts, candidate):
            raise VaultWriteDenied(
                f"append refused: {candidate} is not under {self.config.accounts}. "
                "Appending below the marker is allowed in account notes and nowhere else"
            )
        if candidate.suffix.lower() != ".md":
            raise VaultWriteDenied(f"append refused: {candidate} is not a note")
        if not candidate.is_file():
            raise VaultWriteDenied(
                f"append refused: {candidate} does not exist. This may only add to a "
                "note the export already wrote; it never creates one"
            )
        return candidate

    def append_below_marker(self, path: str | Path, text: str) -> Path:
        """Add to an account note under the marker. The only write into Accounts/.

        Everything above the marker is hashed before and after. Not because a
        bug here is expected, but because the promise being made to the operator
        is that a CRM export is never modified, and a promise worth making is
        worth checking rather than asserting.

        **Binary from end to end, and that is not a style choice.** The design
        is byte identity, so no step gets to reinterpret bytes. `write_text`
        opens in text mode and on Windows rewrites every `\n` as `\r\n`, which
        turns an existing `\r\n` into `\r\r\n`; `read_text` then folds that
        back to `\n\n`, the hashes disagree, and the guard refuses a file that
        was perfectly fine. Nine tests failed on Windows and none on Linux,
        because Linux translates nothing and the suite therefore agreed with
        itself and with nothing outside.

        The write is atomic: a new file is composed in full and moved over the
        old one, so an interrupted write leaves the original byte-identical
        rather than half of each. A partially rewritten account note is worse
        than a failed append, because it looks like a note.
        """
        from .marker import MarkerError, as_bytes, digest, newline_of, split_bytes

        target = self.resolve_account(path)
        original = target.read_bytes()
        before, below = split_bytes(original)

        was = digest(before)
        newline = newline_of(original)
        composed = (
            before
            + below.rstrip(b"\r\n")
            + newline
            + as_bytes(text, newline)
        )
        after, _ = split_bytes(composed)
        if digest(after) != was:
            # Unreachable by construction, which is the point of checking: the
            # cost of being wrong here is a silently rewritten export.
            raise MarkerError(
                f"append refused: composing the new {target.name} changed the bytes "
                "above the marker. Nothing was written."
            )

        scratch = target.with_name(target.name + ".ranger-tmp")
        try:
            scratch.write_bytes(composed)
            settled, _ = split_bytes(scratch.read_bytes())
            if digest(settled) != was:
                raise MarkerError(
                    f"append refused: {target.name} did not survive being written. "
                    "Nothing was changed."
                )
            scratch.replace(target)
        except Exception:
            scratch.unlink(missing_ok=True)
            if target.read_bytes() != original:
                # Belt and braces. If this ever fires, the atomic move is not
                # atomic on this filesystem and that is worth knowing loudly.
                target.write_bytes(original)
            raise
        return target

    # -- setup ---------------------------------------------------------

    def layout_dirs(self) -> list[Path]:
        """Every folder in the layout, read-only ones included.

        Used by 'ranger init' to stand up a brand new vault. Creating an empty
        Accounts or Knowledge folder is not the same act as writing a note into
        one: this makes directories and never touches a file, and the write
        methods above still refuse everything outside Ranger's own folders.
        """
        return [
            self.config.root,
            self.config.accounts,
            self.config.knowledge,
            *self.config.named_roots,
        ]

    def missing_dirs(self) -> list[Path]:
        """Ranger's own folders that do not exist yet."""
        return [p for p in self.config.named_roots if not p.is_dir()]

    def missing_layout_dirs(self) -> list[Path]:
        return [p for p in self.layout_dirs() if not p.is_dir()]

    def is_empty(self, folder: str | Path) -> bool:
        path = self.resolve_read(folder)
        return not path.is_dir() or not any(path.iterdir())

    def ensure_ranger_dirs(self) -> list[Path]:
        """Create Ranger's own folders. Only ever called by 'ranger init'."""
        return self._make(self.config.named_roots)

    def ensure_layout(self) -> list[Path]:
        """Create the whole layout. Only ever called by 'ranger init'."""
        return self._make(self.layout_dirs())

    def _make(self, paths: Iterable[Path]) -> list[Path]:
        created: list[Path] = []
        for path in paths:
            if not path.is_dir():
                path.mkdir(parents=True, exist_ok=True)
                created.append(path)
        return created
