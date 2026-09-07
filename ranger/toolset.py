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
    scan_all,
)
from .config import Config
from .drafts import DraftRejected, hold_draft
from .memory import append_fact, find_fact, load_memory
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

        scans, unreadable = scan_all(
            vault, config.vault.accounts, config.accounts.exclude_files
        )

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


# -- 4. remember -----------------------------------------------------------

#: Phrases that mean this belongs in the account note, which the CRM export
#: owns and overwrites. Memory is for facts about the operator.
_ACCOUNT_SHAPED = (
    "annual packaging spend", "revenue tier", "decision structure",
    "is the decision maker", "opportunity", "stage is", "close date",
)


def _remember(config: Config, vault: Vault) -> Tool:
    async def handler(payload: dict[str, Any]) -> ToolResult:
        fact = " ".join(str(payload.get("fact", "")).split()).strip()
        if not fact:
            return ToolResult(False, "There is no fact there to remember.", "empty")
        if len(fact) > 300:
            return ToolResult(
                False,
                "That is too long for one fact. Memory holds one plain statement per "
                "entry; split it up or shorten it.",
                "too long",
            )

        lowered = fact.lower()
        for phrase in _ACCOUNT_SHAPED:
            if phrase in lowered:
                return ToolResult(
                    False,
                    f"That reads like an account fact ({phrase!r}), and account facts live "
                    "in the account note, which the CRM export owns. Storing it here would "
                    "duplicate the note and drift from it. Tell the operator it belongs in "
                    "the note instead.",
                    "belongs in the account note",
                )

        try:
            written = append_fact(
                vault,
                config.vault.memory,
                fact,
                topic=str(payload.get("topic", "")).strip(),
                filename=config.memory.file,
            )
        except (ValueError, VaultError) as exc:
            return ToolResult(False, str(exc), "refused")

        return ToolResult(
            ok=True,
            content=(
                f"Remembered, in {written.name}. It will be there next time Ranger starts, "
                "and the operator can correct or delete the line in Obsidian."
            ),
            summary=fact[:60],
        )

    return Tool(
        name="remember",
        description=(
            "Store one durable fact about the operator so it survives a restart. Use it for "
            "preferences, standing decisions, how they work, what their words mean, and "
            "anything they ask you to remember. One plain statement per call. Do NOT use it "
            "for facts about a company: those belong in the account note, which their CRM "
            "export owns. Do not store the play-by-play of a conversation."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "fact": {
                    "type": "string",
                    "description": "One plain statement, as it should read a year from now.",
                },
                "topic": {
                    "type": "string",
                    "description": "Optional grouping, such as Preferences or Vocabulary.",
                },
            },
            "required": ["fact"],
        },
        handler=handler,
        writes=True,
    )


# -- 5. forget -------------------------------------------------------------


def _forget(config: Config, vault: Vault) -> Tool:
    async def handler(payload: dict[str, Any]) -> ToolResult:
        # Not reachable until the Tier 6 gate exists: the core refuses a tool
        # flagged confirm before the handler runs. Written so the gate has
        # something real to gate.
        query = str(payload.get("fact", "")).strip()
        context = load_memory(vault, config.vault.memory, config.memory.reserve_chars)
        matches = find_fact(list(context.facts), query)
        if not matches:
            return ToolResult(False, f"Nothing in memory matches {query!r}.", "no match")
        listing = chr(10).join(f"- {f.render()}" for f in matches)
        return ToolResult(
            ok=False,
            content=(
                "Removing a fact rewrites a file, which needs the operator's yes. "
                f"These would go:{chr(10)}{listing}"
            ),
            summary=f"{len(matches)} facts",
        )

    return Tool(
        name="forget",
        description=(
            "Remove a stored fact that is wrong or out of date. Needs the operator's "
            "explicit yes every time, because it rewrites a file. They can also just "
            "delete the line themselves in Obsidian, which is usually quicker."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "A phrase from the fact to remove."}
            },
            "required": ["fact"],
        },
        handler=handler,
        writes=True,
        confirm=True,
    )


def build_registry(
    config: Config, vault: Vault, today: Callable[[], date] | None = None
) -> ToolRegistry:
    """Tier 2's three, plus Tier 4's two. Adding one is a scope decision."""
    today = today or date.today
    return ToolRegistry(
        [
            _account_recall(config, vault),
            _draft_and_hold(config, vault),
            _what_went_quiet(config, vault, today),
            _remember(config, vault),
            _forget(config, vault),
        ]
    )
