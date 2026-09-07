"""The terminal. The first caller of the core, and permanently the debug path.

There is no agent logic in this file. It reads a line, hands it to
Ranger.turn(), and renders the events that come back. When the browser front
end arrives it does the same thing with a websocket instead of a terminal.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from .config import Config, ConfigError, load_config, require_api_key
from .core import Ranger
from .events import Notice, State, StateChanged, TextDelta, ToolCalled, ToolFinished, TurnComplete
from .knowledge import KnowledgeLoader
from .provider import build_provider
from .tools import build_registry
from .vault import Vault

DIM = "\033[2m"
BOLD = "\033[1m"
TEAL = "\033[38;5;43m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"

HELP = """\
  /help     this
  /state    what Ranger thinks it is doing
  /config   the loaded configuration
  /tools    the tool registry
  /vault    vault paths and whether they exist
  /reset    forget this conversation
  /quit     leave
"""


def _colour(enabled: bool):
    def paint(text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if enabled else text

    return paint


def _build_agent(config: Config) -> Ranger:
    api_key = require_api_key()
    vault = Vault(config.vault)
    return Ranger(
        config=config,
        provider=build_provider(config.model, api_key),
        registry=build_registry(config, vault),
        vault=vault,
        knowledge_loader=KnowledgeLoader(vault, config.vault, config.knowledge),
    )


def _describe_config(config: Config) -> str:
    schedule = config.schedule
    return "\n".join(
        [
            f"  config file      {config.source_path}",
            f"  model            {config.model.name} via {config.model.provider}",
            f"  max tokens       {config.model.max_tokens}, effort {config.model.effort or 'unset'}",
            f"  vault root       {config.vault.root}",
            f"  morning surface  {schedule.morning_hour:02d}:00",
            f"  quiet hours      {schedule.quiet_start_hour:02d}:00 to {schedule.quiet_end_hour:02d}:00",
            f"  quiet after      {config.accounts.quiet_after_days} days",
            f"  knowledge budget {config.knowledge.budget_chars} chars",
        ]
    )


def _describe_vault(config: Config) -> str:
    rows = [
        ("root", config.vault.root, "read"),
        ("accounts", config.vault.accounts, "read only"),
        ("knowledge", config.vault.knowledge, "read only"),
        ("memory", config.vault.memory, "read + write"),
        ("inbox", config.vault.inbox, "read + write"),
        ("drafts", config.vault.drafts, "read + write"),
        ("log", config.vault.log, "append only"),
    ]
    return "\n".join(
        f"  {name:<10} {'ok ' if path.is_dir() else 'missing'}  {access:<12} {path}"
        for name, path, access in rows
    )


async def _run_turn(agent: Ranger, text: str, paint, show_state: bool) -> None:
    printed_any = False
    for_stderr: list[str] = []

    async for event in agent.turn(text):
        if isinstance(event, StateChanged):
            if show_state:
                print(paint(f"[{event.state.value}]", DIM), file=sys.stderr)
        elif isinstance(event, TextDelta):
            printed_any = True
            print(event.text, end="", flush=True)
        elif isinstance(event, ToolCalled):
            print(paint(f"\n  -> {event.name} {event.input}", DIM), flush=True)
        elif isinstance(event, ToolFinished):
            mark = "ok" if event.ok else "failed"
            print(paint(f"  <- {event.name} {mark}: {event.summary}", DIM), flush=True)
        elif isinstance(event, Notice):
            colour = RED if event.level == "alert" else YELLOW if event.level == "warn" else DIM
            for_stderr.append(paint(f"[{event.level}] {event.message}", colour))
        elif isinstance(event, TurnComplete):
            if printed_any:
                print()
            if show_state and event.usage:
                print(paint(f"[tokens {event.usage}]", DIM), file=sys.stderr)

    for line in for_stderr:
        print(line, file=sys.stderr)


async def repl(config: Config, show_state: bool) -> int:
    paint = _colour(sys.stdout.isatty())

    try:
        agent = _build_agent(config)
    except ConfigError as exc:
        print(paint(f"cannot start: {exc}", RED), file=sys.stderr)
        return 1

    print(paint("Ranger", BOLD + TEAL) + paint(f"  {config.model.name}", DIM))
    for warning in config.warnings:
        print(paint(f"  note: {warning}", YELLOW))
    knowledge = agent.knowledge()
    if knowledge.docs:
        print(paint(f"  knowledge: {len(knowledge.docs)} file(s), {knowledge.total_chars} chars", DIM))
    print(paint("  /help for commands, /quit to leave", DIM))
    print()

    loop = asyncio.get_running_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, lambda: input("> "))
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        text = line.strip()
        if not text:
            continue
        if text in {"/quit", "/exit"}:
            return 0
        if text == "/help":
            print(HELP)
            continue
        if text == "/state":
            print(f"  {agent.state.value}")
            continue
        if text == "/config":
            print(_describe_config(config))
            continue
        if text == "/vault":
            print(_describe_vault(config))
            continue
        if text == "/tools":
            names = agent.registry.names()
            print("  " + (", ".join(names) if names else "none registered (Tier 2 adds three)"))
            continue
        if text == "/reset":
            agent.reset()
            print(paint("  conversation cleared", DIM))
            continue

        try:
            await _run_turn(agent, text, paint, show_state)
        except KeyboardInterrupt:
            print(paint("\n  interrupted", DIM))
        except Exception as exc:
            print(paint(f"\n  {type(exc).__name__}: {exc}", RED), file=sys.stderr)


def cmd_doctor(config: Config) -> int:
    print("configuration")
    print(_describe_config(config))
    print("\nvault")
    print(_describe_vault(config))

    problems = 0
    print("\nchecks")
    try:
        require_api_key()
        print("  ok       ANTHROPIC_API_KEY is set")
    except ConfigError as exc:
        problems += 1
        print(f"  problem  {exc}")

    schedule = config.schedule
    print(
        f"  ok       morning surface at {schedule.morning_hour:02d}:00 sits outside quiet hours"
    )

    if not config.vault.root.is_dir():
        problems += 1
        print(f"  problem  vault root does not exist: {config.vault.root}")
    else:
        print("  ok       vault root exists")

    vault = Vault(config.vault)
    missing = vault.missing_dirs()
    if missing:
        print(f"  todo     {len(missing)} of Ranger's folders are missing. Run 'ranger init'.")
    else:
        print("  ok       Ranger's folders exist")

    print("\nseeding")
    print(_describe_seeding(config, vault))

    return 1 if problems else 0


def _describe_seeding(config: Config, vault: Vault) -> str:
    """What is still missing before Tier 2 has anything to read."""
    lines: list[str] = []

    accounts = vault.list_markdown(config.vault.accounts) if config.vault.accounts.is_dir() else []
    if accounts:
        lines.append(f"  ok       {len(accounts)} account note(s)")
    else:
        lines.append("  todo     Accounts is empty. Account recall has nothing to read.")

    if not config.vault.knowledge.is_dir():
        lines.append("  todo     Knowledge does not exist. Run 'ranger init'.")
        return "\n".join(lines)

    present = {f.path.name.lower() for f in vault.list_markdown(config.vault.knowledge)}
    wanted = [name for name in config.knowledge.priority]
    absent = [name for name in wanted if name.lower() not in present]
    extra = len(present) - (len(wanted) - len(absent))

    if not present:
        lines.append("  todo     Knowledge is empty. Ranger has no business context.")
    elif absent:
        lines.append(f"  partial  Knowledge has {len(present)} file(s); still missing:")
        for name in absent:
            lines.append(f"             {name}")
    else:
        found = f"{len(wanted)} expected file(s)"
        if extra > 0:
            found += f" plus {extra} more"
        lines.append(f"  ok       Knowledge has {found}")

    if absent or not present:
        lines.append("  note     see docs/vault-conventions.md for the shapes Tier 2 reads")
    return "\n".join(lines)


def cmd_init(config: Config, assume_yes: bool) -> int:
    vault = Vault(config.vault)
    fresh = not config.vault.root.is_dir()
    missing = vault.missing_layout_dirs() if fresh else vault.missing_dirs()

    if not missing:
        print("The layout already exists. Nothing to do.")
        return 0

    if fresh:
        print(f"No vault at {config.vault.root} yet, so this creates the whole layout:")
    else:
        print("About to create these folders and nothing else:")

    for path in missing:
        note = ""
        if path == config.vault.accounts or path == config.vault.knowledge:
            note = "  (yours, read only, seed it by hand)"
        elif path in config.vault.writable_roots:
            note = "  (Ranger's)"
        print(f"  {path}{note}")

    print("Empty folders only. No notes are written, and nothing outside")
    print("Ranger's own folders is ever written to afterwards.")

    if not assume_yes:
        answer = input("Create them? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Nothing created.")
            return 1

    for path in (vault.ensure_layout() if fresh else vault.ensure_ranger_dirs()):
        print(f"  created {path}")

    if vault.is_empty(config.vault.knowledge):
        print()
        print("Knowledge is empty, so Ranger knows nothing about the business yet.")
        print("See docs/vault-conventions.md for what to put there.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ranger", description="Ranger, a voice-first assistant.")
    parser.add_argument("-c", "--config", help="path to ranger.toml")
    parser.add_argument(
        "--quiet-state",
        action="store_true",
        help="hide the state stream the core emits",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("chat", help="talk to Ranger in the terminal (default)")
    sub.add_parser("doctor", help="check the config, the vault and the environment")
    init = sub.add_parser("init", help="create Ranger's own folders in the vault")
    init.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")

    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.command == "doctor":
        return cmd_doctor(config)
    if args.command == "init":
        return cmd_init(config, args.yes)

    try:
        return asyncio.run(repl(config, show_state=not args.quiet_state))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
