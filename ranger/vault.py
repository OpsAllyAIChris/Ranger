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
                    relative=str(path.relative_to(self.config.root)),
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

    # -- setup ---------------------------------------------------------

    def missing_dirs(self) -> list[Path]:
        return [p for p in self.config.writable_roots if not p.is_dir()]

    def ensure_ranger_dirs(self) -> list[Path]:
        """Create Ranger's own folders. Only ever called by 'ranger init'."""
        created: list[Path] = []
        for path in self.config.writable_roots:
            if not path.is_dir():
                path.mkdir(parents=True, exist_ok=True)
                created.append(path)
        return created
