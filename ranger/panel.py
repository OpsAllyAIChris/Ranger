"""Tier 7c. What the activity panel shows, read from the vault.

The panel is a view of real folders, not a second copy of the state. Inbox is
`Ranger/inbox`, Drafts is `Ranger/drafts`, and dismissing a notice in the
browser rewrites the note in the vault the same way `ranger inbox dismiss`
does. Close the browser and the truth is still on disk; open Obsidian and it
is the same file.

Two things this module will not do, and both are the vault's rules rather than
the panel's:

- **Nothing is deleted.** `Inbox.dismiss` marks a notice dismissed and leaves
  it readable. The vault has no delete path and is not getting one.
- **Drafts are listed, never removed.** A draft is a note, and removing an
  existing note is on the operator's never-without-asking list. The panel shows
  them and opens them; taking one away is done in Obsidian, by a person.

The panel also carries the dashlets, and they arrive the same way everything
else here does: read off the disk, computed in Python, on the push. See
`dashlets.py` for why a dashlet is never an agent turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config
from .dashlets import readings
from .dates import human_datetime
from .heartbeat import Inbox, Notice
from .vault import Vault, VaultError
from .ownfiles import CLEARED

#: HoldingGate writes these when there is nobody at a keyboard: voice turns and
#: anything the heartbeat starts. They are the panel's Awaiting Confirmation
#: section, alongside any card open in this browser right now.
AWAITING_KIND = "awaiting-confirmation"

#: Enough to see, not enough to read out. The full note is one click away in
#: Obsidian, which is the right tool for reading a note.
PREVIEW_CHARS = 220


@dataclass(frozen=True)
class Item:
    """One row in the panel."""

    id: str
    title: str
    detail: str
    when: str

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "detail": self.detail, "when": self.when}


def _preview(text: str) -> str:
    flat = " ".join(text.split())
    return flat[:PREVIEW_CHARS] + ("..." if len(flat) > PREVIEW_CHARS else "")


def _identify(vault: Vault, path: Path) -> str:
    """A row's id is its path inside the vault, so a click can find the file.

    Vault relative rather than absolute: it never leaves the machine, but it is
    also never a full path in a page the operator might screenshot, and it is
    the same string Obsidian shows.
    """
    return path.relative_to(vault.config.root).as_posix()


def _notice_item(vault: Vault, notice: Notice) -> Item:
    return Item(
        id=_identify(vault, notice.path) if notice.path else "",
        title=notice.title or notice.kind,
        detail=_preview(notice.body),
        when=human_datetime(notice.created),
    )


def _draft_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("title:"):
            return line[len("title:") :].strip() or fallback
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback


def snapshot(config: Config, vault: Vault) -> dict[str, Any]:
    """Everything the panel draws, in one read of the vault."""
    inbox = Inbox(vault, config.vault.inbox)
    try:
        pending = inbox.pending()
    except VaultError:
        pending = []

    waiting = [n for n in pending if n.kind == AWAITING_KIND]
    ordinary = [n for n in pending if n.kind != AWAITING_KIND]

    drafts: list[Item] = []
    try:
        for item in vault.list_markdown(config.vault.drafts):
            # Cleared drafts are moved into drafts/cleared/, not deleted. The
            # panel is the reason clearing exists, so it is the one place that
            # must not show them.
            if CLEARED in item.path.parts:
                continue
            try:
                text = vault.read_text(item.path)
            except VaultError:
                continue
            drafts.append(
                Item(
                    id=_identify(vault, item.path),
                    title=_draft_title(text, item.path.stem),
                    detail=_preview(text.split("---")[-1]),
                    when=human_datetime(datetime.fromtimestamp(item.path.stat().st_mtime)),
                )
            )
    except VaultError:
        pass
    drafts.sort(key=lambda item: item.title)

    return {
        "inbox": [_notice_item(vault, n).as_dict() for n in ordinary],
        "drafts": [item.as_dict() for item in drafts],
        "awaiting": [_notice_item(vault, n).as_dict() for n in waiting],
        # Python-computed reads, not agent turns. The panel redraws on every
        # push; nothing that redraws that often may cost a model call.
        "dashlets": [reading.as_dict() for reading in readings(config, vault)],
    }


def dismiss(config: Config, vault: Vault, item_id: str) -> str | None:
    """Clear one notice in the vault. Returns its title, or None if it is gone.

    The id came from a browser, so it is treated as one: it is resolved through
    the vault's write wall, and then checked to be a notice in the inbox rather
    than any other writable file. Amendment D enforced in code, at the point a
    string from outside becomes a path.
    """
    if not item_id:
        return None
    try:
        target = vault.resolve_write(config.vault.root / item_id)
    except VaultError:
        return None
    if target.parent != config.vault.inbox.resolve() and target.parent != config.vault.inbox:
        return None

    inbox = Inbox(vault, config.vault.inbox)
    for notice in inbox.all():
        if notice.path == target and not notice.dismissed:
            inbox.dismiss(notice)
            return notice.title or notice.kind
    return None
