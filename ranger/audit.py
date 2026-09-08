"""The audit trail.

Append-only markdown in Ranger/log/, one file a day, readable in Obsidian. What
ran, what the heartbeat surfaced, what was confirmed or declined, and what it
cost. When something surprises the operator, this is how they find out what
happened.

Append-only is enforced by the vault, not by convention: Ranger has no way to
rewrite a log file even if it wanted to, because vault.write_new and
vault.overwrite both refuse inside the log folder.

Writing to the log must never be able to stop a turn, so every caller wraps it
and swallows failures. A lost log line is bad; a lost turn because the disk was
full is worse.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from .dates import human_datetime
from .vault import Vault

HEADER = """\
# Jarvis log

Append only. Jarvis cannot rewrite this file; the vault refuses.

| time | origin | kind | detail |
| ---- | ------ | ---- | ------ |
"""


def _cell(text: str) -> str:
    """Keep one entry on one row, whatever was in it."""
    return " ".join(str(text).split()).replace("|", "\\|")[:400]


@dataclass
class AuditLog:
    vault: Vault
    folder: Path
    now: Callable[[], datetime] = datetime.now

    def path_for(self, when: date) -> Path:
        return self.folder / f"{when.isoformat()}.md"

    def write(self, kind: str, detail: str, *, origin: str = "conversation") -> Path:
        when = self.now()
        target = self.path_for(when.date())
        line = f"| {when:%H:%M:%S} | {_cell(origin)} | {_cell(kind)} | {_cell(detail)} |\n"
        if not target.exists():
            return self.vault.append(target, HEADER + line)
        return self.vault.append(target, line)

    def read(self, when: date | None = None) -> str:
        target = self.path_for(when or self.now().date())
        try:
            return self.vault.read_text(target)
        except Exception:
            return ""

    def days(self) -> list[date]:
        found: list[date] = []
        for item in self.vault.list_markdown(self.folder):
            try:
                found.append(date.fromisoformat(item.path.stem))
            except ValueError:
                continue
        return sorted(found, reverse=True)
