"""The three Tier 2 tools, and nothing else.

Account recall, draft and hold, what went quiet. Each is one entry in the
registry. Anything later, CRM writes, quotes, proposals, transcript ingestion,
is another entry here and no change to the core.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Callable

from .accounts import (
    NoteScan,
    parse_note,
    quiet_report,
    render_digest,
    resolve_account,
    scan_note,
)
from .config import Config
from .drafts import DraftRejected, hold_draft
from .tools import Tool, ToolRegistry, ToolResult
from .untrusted import fence, scan
from .vault import Vault, VaultError


def _account_files(config: Config, vault: Vault) -> list[Any]:
    """Every account note, minus the things that are not account notes."""
    excluded = {name.lower() for name in config.accounts.exclude_files}
    return [
        item
        for item in vault.list_markdown(config.vault.accounts)
        if item.path.name.lower() not in excluded
    ]


def _ambiguous_result(query: str, candidates: tuple[str, ...]) -> ToolResult:
    listing = "\n".join(f"- {name}" for name in candidates)
    return ToolResult(
        ok=True,
        content=(
            f"{len(candidates)} accounts match {query!r}. Ask the operator which one "
            f"they mean and do not pick one yourself:\n{listing}"
        ),
        summary=f"{len(candidates)} matches, needs the operator",
    )


# -- 1. account recall ------------------------------------------------------


def _account_recall(config: Config, vault: Vault) -> Tool:
    async def handler(payload: dict[str, Any]) -> ToolResult:
        query = str(payload.get("account", "")).strip()
        if not query:
            return ToolResult(False, "No account name was given.", "no account named")

        files = _account_files(config, vault)
        names = [item.path.stem for item in files]
        resolution = resolve_account(query, names)

        if resolution.ambiguous:
            return _ambiguous_result(query, resolution.candidates)
        if not resolution.found:
            return ToolResult(
                ok=True,
                content=(
                    f"No account note matches {query!r}. There are {len(names)} accounts "
                    "in the vault. Say so rather than guessing at what they meant."
                ),
                summary="no match",
            )

        item = next(f for f in files if f.path.stem == resolution.match)
        text = vault.read_text(item.path)
        note = parse_note(text, resolution.match, item.path)

        detail = bool(payload.get("detail"))
        digest = render_digest(
            note,
            activities=config.recall.detail_activities if detail else config.recall.activities,
            section_chars=config.recall.section_chars,
            body_chars=config.recall.body_chars,
            max_chars=config.recall.max_chars,
        )

        # An account note holds pasted customer email and vendor text. It is
        # data, and it is fenced as data.
        findings = scan(digest)
        body = fence(item.relative, digest, findings=findings)
        note_of_size = (
            f"\n\nThis is a digest of a {note.size} character note, not the note itself. "
            "Answer in a sentence or two and offer detail if the operator wants it."
        )
        return ToolResult(
            ok=True,
            content=body + note_of_size,
            summary=f"{resolution.match} ({resolution.how} match, {len(note.activities)} activities)",
        )

    return Tool(
        name="account_recall",
        description=(
            "Read the operator's notes for one account and answer a question about it. "
            "Use this whenever they ask where something stands, what someone said, or "
            "what is happening with a company. Accepts a partial or spoken name such as "
            "'Illes' or 'Pegasus'. Returns a digest, not the whole note. Set detail true "
            "only when the operator asks for more than a sentence or two."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "account": {
                    "type": "string",
                    "description": "The account name as the operator said it. Partial is fine.",
                },
                "detail": {
                    "type": "boolean",
                    "description": "True for a longer activity history. Default false.",
                },
            },
            "required": ["account"],
        },
        handler=handler,
    )


# -- 2. draft and hold ------------------------------------------------------


def _draft_and_hold(config: Config, vault: Vault) -> Tool:
    async def handler(payload: dict[str, Any]) -> ToolResult:
        title = str(payload.get("title", "")).strip()
        body = str(payload.get("body", "")).strip()
        if not title or not body:
            return ToolResult(False, "A draft needs both a title and a body.", "incomplete")

        account = str(payload.get("account", "")).strip() or None
        if account:
            names = [item.path.stem for item in _account_files(config, vault)]
            resolution = resolve_account(account, names)
            if resolution.ambiguous:
                return _ambiguous_result(account, resolution.candidates)
            if resolution.found:
                account = resolution.match

        try:
            held = hold_draft(
                vault,
                config.drafts,
                config.vault.drafts,
                title=title,
                body=body,
                account=account,
                recipient=str(payload.get("recipient", "")).strip() or None,
            )
        except DraftRejected as exc:
            return ToolResult(False, str(exc), "rejected, writing rules")
        except VaultError as exc:
            return ToolResult(False, str(exc), "refused by the vault")

        return ToolResult(
            ok=True,
            content=(
                f"Draft held at {held.relative}. It has not been sent and Ranger cannot "
                "send it. Tell the operator where it is in one short line."
            ),
            summary=held.relative,
        )

    return Tool(
        name="draft_and_hold",
        description=(
            "Write a draft into the operator's drafts folder and hold it there. Use this "
            "for any follow-up, reply, summary or note they ask you to write. It is never "
            "sent; the operator reads it and sends it themselves. Write the body in their "
            "voice: plain language, short, warm and direct, and no dashes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short subject line for the draft."},
                "body": {
                    "type": "string",
                    "description": "The full draft, in the operator's writing style.",
                },
                "account": {"type": "string", "description": "Related account, if there is one."},
                "recipient": {"type": "string", "description": "Who it is addressed to."},
            },
            "required": ["title", "body"],
        },
        handler=handler,
        writes=True,
    )


# -- 3. what went quiet -----------------------------------------------------


def _what_went_quiet(config: Config, vault: Vault, today: Callable[[], date]) -> Tool:
    async def handler(payload: dict[str, Any]) -> ToolResult:
        raw_days = payload.get("days")
        try:
            days = int(raw_days) if raw_days is not None else config.accounts.quiet_after_days
        except (TypeError, ValueError):
            days = config.accounts.quiet_after_days
        days = max(1, days)

        scans: list[NoteScan] = []
        unreadable: list[str] = []
        for item in _account_files(config, vault):
            try:
                scans.append(scan_note(vault.read_text(item.path), item.path.stem, item.path))
            except Exception as exc:
                unreadable.append(f"{item.relative}: {exc}")

        report = quiet_report(
            scans,
            threshold_days=days,
            as_of=today(),
            skip_statuses=config.accounts.skip_statuses,
        )

        lines = [
            f"As of {report.as_of}, quiet means no logged activity for more than {days} days.",
            f"{report.considered} accounts checked, {report.active} still active.",
        ]

        if report.lapsed:
            lines += ["", f"Gone quiet ({len(report.lapsed)}), longest first:"]
            lines += [
                f"- {a.name}: {a.days} days, last activity {a.last_activity}"
                for a in report.lapsed
            ]
        else:
            lines += ["", "Nothing has gone quiet."]

        if report.never_touched:
            lines += [
                "",
                f"Never had any activity logged ({len(report.never_touched)}). These are a "
                "different problem from a lapsed account, so keep them separate when you "
                "say this out loud:",
            ]
            lines += [f"- {name}" for name in report.never_touched]

        if report.skipped_unconfirmed:
            lines += [
                "",
                f"{len(report.skipped_unconfirmed)} unconfirmed notes were left out; the "
                "operator is still cleaning those up.",
            ]
        if unreadable:
            lines += ["", "Could not read:"] + [f"- {u}" for u in unreadable]

        summary = f"{len(report.lapsed)} quiet, {len(report.never_touched)} never touched"
        return ToolResult(ok=True, content="\n".join(lines), summary=summary)

    return Tool(
        name="what_went_quiet",
        description=(
            "List the accounts with no logged activity for a while, longest first. Use this "
            "when the operator asks what is slipping, what has gone quiet, or who they have "
            "not spoken to. Accounts that have never had any activity are reported "
            "separately, because they are a different problem."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Override the quiet threshold in days. Omit to use the operator's setting.",
                }
            },
        },
        handler=handler,
    )


def build_registry(
    config: Config, vault: Vault, today: Callable[[], date] | None = None
) -> ToolRegistry:
    """Three tools. Adding a fourth is a Tier 2 scope decision, not a detail."""
    today = today or date.today
    return ToolRegistry(
        [
            _account_recall(config, vault),
            _draft_and_hold(config, vault),
            _what_went_quiet(config, vault, today),
        ]
    )
