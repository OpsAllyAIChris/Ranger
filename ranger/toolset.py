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
    merge_notes,
    render_digest,
    resolve_account,
    scan_all,
)
from .config import Config
from .drafts import DraftRejected, hold_draft
from .memory import append_fact, find_fact, load_memory
from .tools import Tool, ToolRegistry, ToolResult
from .ownfiles import FOLDERS
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


def _aliases(config: Config, vault: Vault, names: list[str]):
    """The alias map, checked against the accounts that actually exist."""
    from .aliases import AliasFile

    return AliasFile(vault, config.vault.ranger).load(names)


def _resolve(query: str, names: list[str], aliases):
    """Resolve a spoken name, then fold it onto its canonical account.

    Two steps rather than one: the fuzzy matcher still does the work, and the
    alias map only decides which of two real notes the answer belongs to. That
    keeps "BWI Compny" working, which a straight lookup in the map would not.
    """
    from .accounts import resolve_account

    resolution = resolve_account(query, names)
    if not resolution.found:
        return resolution

    canonical = aliases.canonical_for(resolution.match)
    if canonical == resolution.match or canonical not in names:
        return resolution
    from dataclasses import replace

    return replace(resolution, match=canonical)


def _notes_for(account: str, files, aliases) -> list:
    """Every note belonging to one account: the canonical one and its variants.

    This is the half that was easy to miss. Folding the scans makes the morning
    brief right; folding the *reads* is what makes an answer right, because an
    account split across two files was answering from half its own history.
    """
    wanted = {account.casefold(), *(v.casefold() for v in aliases.variants_of(account))}
    return [item for item in files if item.path.stem.casefold() in wanted]


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
        aliases = _aliases(config, vault, names)
        resolution = _resolve(query, names, aliases)

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

        parts = _notes_for(resolution.match, files, aliases)
        item = next(f for f in files if f.path.stem == resolution.match)
        notes = [
            parse_note(vault.read_text(f.path), f.path.stem, f.path) for f in parts
        ] or [parse_note(vault.read_text(item.path), resolution.match, item.path)]

        note = merge_notes(resolution.match, notes) if len(notes) > 1 else notes[0]

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
        label = item.relative if len(notes) == 1 else (
            f"{len(notes)} notes for {resolution.match}"
        )
        body = fence(label, digest, findings=findings)
        folded = (
            ""
            if len(notes) == 1
            else (
                f"\n\nThis account is exported under {len(notes)} names and they are read "
                "together. Answer as one account."
            )
        )
        note_of_size = (
            f"\n\nThis is a digest of a {note.size} character note, not the note itself. "
            "Answer in a sentence or two and offer detail if the operator wants it."
        )
        return ToolResult(
            ok=True,
            content=body + folded + note_of_size,
            summary=(
                f"{resolution.match} ({resolution.how} match, {len(note.activities)} activities"
                + (f", {len(notes)} notes)" if len(notes) > 1 else ")")
            ),
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
            files = _account_files(config, vault)
            names = [item.path.stem for item in files]
            resolution = _resolve(account, names, _aliases(config, vault, names))
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
        full = bool(payload.get("full", False))

        scans, unreadable = scan_all(
            vault, config.vault.accounts, config.accounts.exclude_files
        )
        # One account per real account, with the halves added together. An
        # account worked last week under one name is not quiet because the
        # other name has been silent for a year.
        from .aliases import fold

        scans = fold(scans, _aliases(config, vault, [s.name for s in scans]))

        if not full:
            from dataclasses import replace as _replace

            from .brief import BriefStore, build

            store = BriefStore(vault, config.vault.ranger, config.brief)
            # The threshold can be overridden per question, so the brief is
            # built against what was actually asked for.
            accounts_config = (
                config.accounts
                if days == config.accounts.quiet_after_days
                else _replace(config.accounts, quiet_after_days=days)
            )
            brief, _ = build(
                scans,
                accounts=accounts_config,
                brief=config.brief,
                as_of=today(),
                seen=store.seen(),
                dormant=store.dormant(),
            )
            # Deliberately not written back. Being told on demand must not
            # consume tomorrow's "just went quiet".
            summary = (
                f"{len(brief.slipping)} slipping, {len(brief.deals)} deals, "
                f"{brief.withheld} withheld"
            )
            return ToolResult(ok=True, content=brief.render(), summary=summary)

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
            "What is slipping. Use this when the operator asks what has gone quiet, what is "
            "slipping, or who they have not spoken to. By default it returns a short brief: "
            "what has just gone quiet, which open deals have stalled, and one older account "
            "to make a decision about, with a count of everything held back. Pass full=true "
            "only when the operator asks for the whole list. Accounts that have never had "
            "any activity are reported separately, because they are a different problem."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Override the quiet threshold in days. Omit to use the operator's setting.",
                },
                "full": {
                    "type": "boolean",
                    "description": (
                        "Every quiet account, longest first, instead of the brief. Only when "
                        "the operator asks for the full list."
                    ),
                },
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
        # Only reached once the gate has been through and the operator said
        # yes. The gate runs before this, in core.turn.
        query = str(payload.get("fact", "")).strip()
        context = load_memory(vault, config.vault.memory, config.memory.reserve_chars)
        matches = find_fact(list(context.facts), query)
        if not matches:
            return ToolResult(False, f"Nothing in memory matches {query!r}.", "no match")

        target = config.vault.memory / config.memory.file
        try:
            text = vault.read_text(target)
        except VaultError as exc:
            return ToolResult(False, str(exc), "could not read memory")

        gone = {f.text for f in matches}
        kept, removed = [], 0
        for line in text.splitlines():
            body = line.strip()
            if body.startswith("-") and any(g in body for g in gone):
                removed += 1
                continue
            kept.append(line)
        if not removed:
            return ToolResult(False, "Could not find those lines to remove.", "no lines matched")

        vault.overwrite(target, chr(10).join(kept) + chr(10), allow_overwrite=True)
        listing = chr(10).join(f"- {f.text}" for f in matches)
        return ToolResult(
            ok=True,
            content=f"Removed {removed} fact(s):{chr(10)}{listing}",
            summary=f"removed {removed}",
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
        describe=lambda payload: (
            "Permanently remove from memory every fact matching "
            f"{str(payload.get('fact', '')).strip()!r}. This rewrites "
            "Ranger/memory and cannot be undone from here."
        ),
    )


# -- 6. file into an account ------------------------------------------------


def _file_to_account(config: Config, vault: Vault, today: Callable[[], date]) -> Tool:
    """Append Ranger's own context to an account note, below the marker.

    The tool that closes the gap between Ranger having the context and Ranger
    being able to put it anywhere but a drafts folder the operator pastes from
    by hand.

    **It does not gate, and that was decided rather than skipped.** Filing a
    note into an account that already exists happens often enough that a
    confirmation card would become a reflex inside a week, and a card clicked
    without reading manufactures a record of review that did not happen.
    Creating a *new* account is a different act and still gates. What makes the
    ungated version safe is that this can only ever add, only ever below the
    marker, and only ever to a note the export already wrote.
    """

    async def handler(payload: dict[str, Any]) -> ToolResult:
        from .marker import MarkerError, entry
        from .vault import VaultError

        account = str(payload.get("account", "")).strip()
        note = str(payload.get("note", "")).strip()
        source = str(payload.get("source", "")).strip() or "Ranger"

        if not account:
            return ToolResult(False, "An account name is needed to file anything.",
                              "no account named")
        if not note:
            return ToolResult(False, "There was no note text to append.", "nothing to file")

        files = _account_files(config, vault)
        names = [item.path.stem for item in files]
        aliases = _aliases(config, vault, names)
        resolution = _resolve(account, names, aliases)

        if resolution.ambiguous:
            return _ambiguous_result(account, resolution.candidates)
        if not resolution.found:
            return ToolResult(
                ok=True,
                content=(
                    f"No account note matches {account!r}, so nothing was filed. There "
                    f"are {len(names)} accounts in the vault. Say so rather than "
                    "guessing at which they meant, and never create one to file into."
                ),
                summary="no match",
            )

        item = next(f for f in files if f.path.stem == resolution.match)

        try:
            vault.append_below_marker(item.path, entry(note, source, today()))
        except (MarkerError, VaultError) as exc:
            # Fail closed, and say which refusal it was. A refusal the operator
            # cannot act on is a refusal they will work around.
            return ToolResult(False, str(exc), "not filed")
        except Exception as exc:  # a check that raises counts as denial
            return ToolResult(
                False,
                f"{type(exc).__name__}: {exc}. Nothing was written.",
                "not filed",
            )

        return ToolResult(
            ok=True,
            content=(
                f"Filed to {resolution.match}, appended below the marker in "
                f"{item.path.name}. Nothing above the marker was touched."
            ),
            summary=f"filed to {resolution.match}",
        )

    return Tool(
        name="file_to_account",
        description=(
            "Append a note to an account's own file, below the Ranger Context marker, "
            "so it becomes part of that account's permanent record. Use this whenever "
            "the operator tells you something about an account that is worth keeping: "
            "what was said on a call, what a contact wants, what was agreed, what "
            "changed. Prefer this over a draft for anything that belongs in the account "
            "history rather than in an email. Nothing above the marker is ever touched."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "account": {
                    "type": "string",
                    "description": "Account name. A partial or spoken name is fine.",
                },
                "note": {
                    "type": "string",
                    "description": (
                        "What to record, in one or two sentences. Written for the "
                        "operator to read in six months, not for you to read back."
                    ),
                },
                "source": {
                    "type": "string",
                    "description": (
                        "Where it came from: 'call', 'email', 'Chris', 'meeting'. "
                        "Defaults to Ranger."
                    ),
                },
            },
            "required": ["account", "note"],
        },
        handler=handler,
        writes=True,
    )


# -- 7 and 8. reading back what Ranger wrote --------------------------------


def _list_own_files(config: Config, vault: Vault) -> Tool:
    """What is in Ranger's own folders. The half that was missing.

    Two parameterised tools rather than six named ones. Every tool description
    is in the prompt on every turn, so `list_drafts`, `read_draft`,
    `list_notices`, `read_notice`, `list_memory`, `read_memory` would be six
    descriptions carrying one idea. The folder is an enum, which keeps the
    choice as concrete for the model as a name would be.
    """

    async def handler(payload: dict[str, Any]) -> ToolResult:
        from .ownfiles import UnknownFolder, listing

        folder = str(payload.get("folder", "drafts")).strip().lower()
        try:
            files = listing(vault, config, folder)
        except UnknownFolder as exc:
            return ToolResult(False, str(exc), "unknown folder")
        except Exception as exc:
            return ToolResult(False, f"{type(exc).__name__}: {exc}", "could not list")

        if not files:
            return ToolResult(
                ok=True,
                content=f"There is nothing in Ranger/{folder} yet.",
                summary=f"{folder} is empty",
            )

        limit = 40
        shown = files[:limit]
        body = "\n".join(item.line() for item in shown)
        if len(files) > limit:
            body += f"\n\n({len(files) - limit} older ones not listed.)"
        return ToolResult(
            ok=True,
            content=(
                f"{len(files)} in Ranger/{folder}, newest first. This is a list, not the "
                f"contents: use read_own_file to open one.\n\n{body}"
            ),
            summary=f"{len(files)} in {folder}",
        )

    return Tool(
        name="list_own_files",
        description=(
            "List what Ranger has written into its own folders: drafts it is holding, "
            "notices in the inbox, or what it remembers. Use this when the operator "
            "refers to something you wrote earlier and you need to find which file it "
            "is, before reading it. Returns names, dates and one line each, never the "
            "contents."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "enum": list(FOLDERS),
                    "description": (
                        "drafts for held drafts, inbox for notices waiting to be seen, "
                        "memory for what Ranger remembers about the operator."
                    ),
                },
            },
            "required": ["folder"],
        },
        handler=handler,
    )


def _read_own_file(config: Config, vault: Vault) -> Tool:
    """Open one of Ranger's own files, in full.

    Fenced as untrusted content on the way back in. Ranger wrote the draft, but
    a draft may quote a customer email the operator pasted, and a notice
    summarises a CRM export. Having written the file earlier does not make its
    contents trusted when read back: trust attaches to the path the bytes
    travelled, and write-then-read-back is exactly how a fence gets walked
    around.
    """

    async def handler(payload: dict[str, Any]) -> ToolResult:
        from .ownfiles import UnknownFolder, read

        folder = str(payload.get("folder", "drafts")).strip().lower()
        name = str(payload.get("name", "")).strip()
        if not name:
            return ToolResult(False, "Which file? Use list_own_files to see them.",
                              "no name given")
        try:
            found, text, candidates = read(vault, config, folder, name)
        except UnknownFolder as exc:
            return ToolResult(False, str(exc), "unknown folder")
        except Exception as exc:
            return ToolResult(False, f"{type(exc).__name__}: {exc}", "could not read")

        if found is None:
            if candidates:
                names = "\n".join(f"  {item.name}" for item in candidates[:8])
                return ToolResult(
                    ok=True,
                    content=(
                        f"{name!r} matches {len(candidates)} files in Ranger/{folder}. "
                        f"Ask which one rather than picking:\n{names}"
                    ),
                    summary=f"{len(candidates)} match {name!r}",
                )
            return ToolResult(
                ok=True,
                content=(
                    f"Nothing in Ranger/{folder} matches {name!r}. Say so rather than "
                    "guessing at what they meant."
                ),
                summary="no match",
            )

        findings = scan(text)
        return ToolResult(
            ok=True,
            content=fence(found.relative, text, findings=findings),
            summary=f"read {found.name}",
        )

    return Tool(
        name="read_own_file",
        description=(
            "Read one of Ranger's own files in full: a held draft, an inbox notice, or "
            "the memory file. Use this when the operator asks you to do something with "
            "a draft you wrote, such as filing it into an account, or when they ask "
            "what a notice said. The name can be partial: 'Telly' finds the Telly "
            "draft. Reading a draft does not send or delete it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "enum": list(FOLDERS),
                    "description": "drafts, inbox, or memory.",
                },
                "name": {
                    "type": "string",
                    "description": (
                        "File name or part of the title, as the operator said it."
                    ),
                },
            },
            "required": ["folder", "name"],
        },
        handler=handler,
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
            _file_to_account(config, vault, today),
            _list_own_files(config, vault),
            _read_own_file(config, vault),
        ]
    )
