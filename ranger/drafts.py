"""Draft and hold.

Ranger writes a draft into the vault and stops. It has no way to send anything
and will not get one: sending is on the operator's never-without-asking list,
and the split is that Ranger drafts and the operator sends.

The writing rules are the operator's, and one of them is enforced here rather
than left to the system prompt: no em dashes and no en dashes. A prompt rule is
followed most of the time; this is followed every time.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import DraftsConfig
from .vault import Vault

EM_DASH = "—"
EN_DASH = "–"
_BANNED = {EM_DASH: "em dash", EN_DASH: "en dash"}


class DraftRejected(Exception):
    """The draft breaks a writing rule. Rewrite it, do not save it."""


def slugify(text: str, limit: int = 60) -> str:
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = re.sub(r"[^\w\s-]", "", folded.lower())
    slug = re.sub(r"[\s_]+", "-", folded).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:limit].strip("-")
    return slug or "draft"


def check_writing_rules(text: str) -> list[str]:
    """Return the rule breaks worth refusing over.

    Only the two dash characters. Hyphens are left alone, because 'follow-up'
    and 'mid-market' are ordinary words and refusing those would be worse than
    the rule it enforces.
    """
    problems: list[str] = []
    for char, name in _BANNED.items():
        if char in text:
            line = text[: text.index(char)].count("\n") + 1
            problems.append(f"{name} ({char!r}) on line {line}")
    return problems


@dataclass(frozen=True)
class HeldDraft:
    path: Path
    relative: str
    title: str


def hold_draft(
    vault: Vault,
    config: DraftsConfig,
    drafts_dir: Path,
    *,
    title: str,
    body: str,
    account: str | None = None,
    recipient: str | None = None,
    today: date | None = None,
) -> HeldDraft:
    """Write a draft into the vault. Never sends, never overwrites."""
    problems = check_writing_rules(body) + check_writing_rules(title)
    if problems:
        raise DraftRejected(
            "The draft uses characters the operator's writing rules exclude: "
            + "; ".join(problems)
            + ". Rewrite it with a comma, a full stop, or two sentences, then save again."
        )

    today = today or date.today()
    stem = config.filename_format.format(date=today.isoformat(), slug=slugify(title))
    if not stem.endswith(".md"):
        stem += ".md"

    target = drafts_dir / stem
    counter = 2
    while target.exists():
        # Ranger never overwrites a note, so a second draft on the same subject
        # on the same day sits beside the first.
        target = drafts_dir / f"{stem[:-3]}-{counter}.md"
        counter += 1

    front = [
        "---",
        f"created: {today.isoformat()}",
        f"title: {title}",
    ]
    if account:
        front.append(f"account: {account}")
    if recipient:
        front.append(f"to: {recipient}")
    front += ["status: draft, not sent", "---", ""]

    written = vault.write_new(target, "\n".join(front) + body.strip() + "\n")
    return HeldDraft(
        path=written,
        relative=written.relative_to(vault.config.root).as_posix(),
        title=title,
    )
