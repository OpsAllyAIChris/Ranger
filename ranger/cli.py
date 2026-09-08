"""The terminal. The first caller of the core, and permanently the debug path.

There is no agent logic in this file. It reads a line, hands it to
Ranger.turn(), and renders the events that come back. When the browser front
end arrives it does the same thing with a websocket instead of a terminal.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import os
import sys
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .audio import AudioError, resolve_device
from .audiocheck import format_devices, run_check
from .compare import compare
from .cost import TurnCost
from .dates import human_datetime
from .config import Config, ConfigError, load_config, require_api_key
from .core import Ranger
from .events import Notice, State, StateChanged, TextDelta, ToolCalled, ToolFinished, TurnComplete
from .knowledge import KnowledgeLoader
from .provider import build_provider
from .toolset import build_registry
from .vault import Vault

DIM = "\033[2m"
BOLD = "\033[1m"
TEAL = "\033[38;5;43m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"

HELP = """\
  /help     this
  /state    what Jarvis thinks it is doing
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


def _build_agent(config: Config, *, gate=None, origin: str = "conversation") -> Ranger:
    """The terminal's agent. The gate is TerminalGate because there is a keyboard.

    Every other caller wires its own, and one that wires none gets DenyingGate.
    """
    from .assembly import build_agent
    from .gate import TerminalGate

    return build_agent(config, gate=gate or TerminalGate(out=sys.stdout), origin=origin)


def _describe_config(config: Config) -> str:
    schedule = config.schedule
    local = (
        f"{config.local_path}  ({len(config.overrides)} override"
        f"{'' if len(config.overrides) == 1 else 's'})"
        if config.local_path
        else "none, so every setting comes from the tracked file"
    )
    return "\n".join(
        [
            f"  config file      {config.source_path}",
            f"  local overrides  {local}",
            f"  model            {config.model.name} via {config.model.provider}",
            f"  max tokens       {config.model.max_tokens}, effort {config.model.effort or 'unset'}",
            f"  vault root       {config.vault.root}",
            f"  morning surface  {schedule.morning_hour:02d}:00",
            f"  quiet hours      {schedule.quiet_start_hour:02d}:00 to {schedule.quiet_end_hour:02d}:00",
            f"  quiet after      {config.accounts.quiet_after_days} days",
            f"  standing context  {config.context.budget_chars} chars total, "
            f"memory reserves {config.memory.reserve_chars}",
            *(f"    {key}" for key in config.overrides),
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


SESSION_COST = TurnCost()


async def _run_turn(agent: Ranger, text: str, paint, show_state: bool) -> None:
    global SESSION_COST
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
            if event.usage:
                turn = TurnCost.from_usage(event.usage)
                SESSION_COST = SESSION_COST + turn
                line = turn.render(agent.config.model)
                if line:
                    session = SESSION_COST.render(agent.config.model)
                    print(paint(f"  [{line}]  session: {session}", DIM), file=sys.stderr)

    for line in for_stderr:
        print(line, file=sys.stderr)


async def voice_loop(config: Config, show_state: bool) -> int:
    """Tier 3d. The same core the typed REPL uses, with ears and a mouth."""
    from .audio import AudioError, SoundDeviceBackend
    from .stt import build_transcriber
    from .trigger import TriggerError, build_trigger
    from .tts import build_speaker
    from .voiceloop import VoiceLoop

    paint = _colour(sys.stdout.isatty())
    try:
        # A spoken yes is not consent, so voice holds and the inbox carries it.
        from .gate import HoldingGate

        agent = _build_agent(
            config, gate=HoldingGate(_inbox(config)), origin="voice"
        )
        deepgram = require_api_key("DEEPGRAM_API_KEY")
        elevenlabs = require_api_key("ELEVENLABS_API_KEY")
    except ConfigError as exc:
        print(paint(f"  cannot start: {exc}", RED), file=sys.stderr)
        return 1

    if not config.tts.voice_id:
        print(paint("  tts.voice_id is not set. Run 'ranger voices', then put the id in", RED), file=sys.stderr)
        print(paint(f"  {_local_config_path(config)}, which is git-ignored.", RED), file=sys.stderr)
        return 1

    try:
        backend = SoundDeviceBackend()
        devices = backend.devices()
        input_device = resolve_device(config.voice.input_device, devices, kind="input")
        output_device = resolve_device(config.voice.output_device, devices, kind="output")
        trigger = build_trigger(config.voice.trigger, config.voice.key)
    except (AudioError, TriggerError) as exc:
        print(paint(f"  {exc}", RED), file=sys.stderr)
        return 1

    plan = _keyterm_plan(config)
    stt = config.stt
    if plan.terms:
        from dataclasses import replace

        stt = replace(stt, keyterms=plan.terms)

    print(paint("Jarvis", BOLD + TEAL) + paint(f"  {config.model.name}, voice {config.tts.voice_id}", DIM))
    print(paint(f"  {len(plan.terms)} vocabulary hints, {config.stt.model}", DIM))
    print(paint("  the typed interface is still there: run 'ranger' with no flags", DIM))
    _announce_inbox(config, paint)
    print()

    loop = VoiceLoop(
        agent=agent,
        config=config,
        trigger=trigger,
        backend=backend,
        transcriber=build_transcriber(stt, deepgram),
        speaker=build_speaker(config.tts, elevenlabs),
        out=sys.stdout,
        input_device=input_device,
        output_device=output_device,
        paint=paint,
    )
    return await loop.run()


async def repl(config: Config, show_state: bool) -> int:
    paint = _colour(sys.stdout.isatty())

    try:
        agent = _build_agent(config)
    except ConfigError as exc:
        print(paint(f"cannot start: {exc}", RED), file=sys.stderr)
        return 1

    print(paint("Jarvis", BOLD + TEAL) + paint(f"  {config.model.name}", DIM))
    for warning in config.warnings:
        print(paint(f"  note: {warning}", YELLOW))
    knowledge = agent.knowledge()
    if knowledge.docs:
        print(paint(f"  knowledge: {len(knowledge.docs)} file(s), {knowledge.total_chars} chars", DIM))
    _announce_inbox(config, paint)
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

    if config.tts.voice_id:
        print("  ok       tts.voice_id is set")
    else:
        print("  todo     tts.voice_id is not set. Run 'ranger voices', then put the id in")
        print(f"           {_local_config_path(config)}, which is git-ignored and survives a pull.")

    from .aliases import AliasFile as _AliasFile

    try:
        _scans, _ = __import__("ranger.accounts", fromlist=["scan_all"]).scan_all(
            Vault(config.vault), config.vault.accounts, config.accounts.exclude_files
        )
        _aliases = _AliasFile(Vault(config.vault), config.vault.ranger).load(
            [s.name for s in _scans]
        )
    except Exception:
        _aliases = None

    if _aliases is not None and _aliases.stale:
        problems += len(_aliases.stale)
        for problem in _aliases.stale:
            # Loud, because the symptom of ignoring it is an account quietly
            # splitting back into two after a CRM refresh.
            print(f"  problem  stale alias: {problem}")
    elif _aliases is not None and _aliases.pairs:
        print(f"  ok       {len(_aliases.pairs)} accounts folded together by aliases.md")

    # Surfacing broke on a rename and the only symptom was a not_found in the
    # audit log after a wake firing. That is too late and does not say what it
    # was looking at, so the answer is here, before the microphone is armed.
    from .desktop import find_report, on_windows

    if on_windows():
        window = find_report()
        if window["found"]:
            print(f"  ok       the interface window is findable: {window['title']!r}")
        else:
            problems += 1
            print("  problem  the interface window cannot be found, so the wake word")
            print("           cannot bring it forward and cannot put it away")
            print(f"           looking for: {', '.join(window['looking_for'])}")
            for title in window["titles"][:12]:
                print(f"             open: {title!r}")
            if len(window["titles"]) > 12:
                print(f"             ... and {len(window['titles']) - 12} more")
    else:
        print("  ok       window surfacing is a Windows feature and this is not Windows")

    if config.wake.enabled:
        from .wake import available, missing_models

        ready, why = available()
        gaps: list[str] = []
        if ready:
            try:
                gaps = missing_models(config.wake.model)
            except Exception as exc:  # a broken install, not a missing one
                ready, why = False, str(exc)

        if ready and not gaps:
            print(f"  ok       hands free is on, listening for {config.wake.phrase!r}")
            print(f"           model {config.wake.model}, threshold {config.wake.threshold}, "
                  f"auto off after {config.wake.idle_disarm_minutes:.0f} minutes")
        elif ready:
            # The point the operator asked for: say it here, when hands free is
            # switched on, rather than letting them arm something with nothing
            # to listen with. openWakeWord ships no models in the wheel.
            problems += 1
            print("  problem  hands free cannot work: no model is installed")
            for gap in gaps:
                print(f"           missing {gap}")
            if any(not Path(gap).suffix for gap in gaps):
                print("           Run: ranger wake install")
            else:
                print("           Train it: see scripts/train_wake_word.py")
        else:
            # Not a problem, by design. It is an optional extra and the rest of
            # Jarvis is unaffected by it not being there.
            print(f"  todo     wake.enabled is true but {why}")
    else:
        print("  ok       hands free is off. Set wake.enabled to offer it")

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


def _knowledge_report(config: Config, vault: Vault) -> list[str]:
    """What actually reaches the model, by name.

    Not what is expected to exist: any .md in the folder is loaded, whatever it
    is called. knowledge.priority only decides the order they are loaded in, so
    the most important survive when the budget bites.
    """
    from .knowledge import KnowledgeLoader
    from .memory import load_memory

    memory = load_memory(vault, config.vault.memory, config.memory.reserve_chars)
    remaining = max(0, config.context.budget_chars - memory.total_chars)
    context = KnowledgeLoader(vault, config.vault, config.knowledge).load(budget=remaining)

    lines: list[str] = []
    if not context.docs and not context.omitted:
        lines.append("  todo     Knowledge is empty. Jarvis has no business context.")
        lines.append("  note     any .md in that folder is loaded. The names in")
        lines.append("           knowledge.priority only set the order, they are not required.")
        return lines

    lines.append(
        f"  ok       {len(context.docs)} knowledge file(s) reach the model, "
        f"{context.total_chars} chars of {remaining} available:"
    )
    for doc in context.docs:
        lines.append(f"             loaded   {doc.relative}")
    for name in context.omitted:
        lines.append(f"  DROPPED  not sent  {name}")
    if context.omitted:
        lines.append(
            f"  note     {len(context.omitted)} file(s) did not fit. Raise "
            "context.budget_chars, or split them up."
        )
    return lines


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

    loaded = _knowledge_report(config, vault)
    lines.extend(loaded)

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
        elif path in config.vault.named_roots:
            note = "  (Jarvis's)"
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
        print("Knowledge is empty, so Jarvis knows nothing about the business yet.")
        print("See docs/vault-conventions.md for what to put there.")
    return 0


def cmd_audio_devices(config: Config) -> int:
    """Tier 3a. What PortAudio can see, and which ones Jarvis will use."""
    from .audio import SoundDeviceBackend

    try:
        devices = SoundDeviceBackend().devices()
    except AudioError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1

    voice = config.voice
    problems: list[str] = []
    resolved: dict[str, int | None] = {}
    for kind, spec in (("input", voice.input_device), ("output", voice.output_device)):
        try:
            resolved[kind] = resolve_device(spec, devices, kind=kind)
        except AudioError as exc:
            resolved[kind] = None
            problems.append(str(exc))

    print(format_devices(devices, resolved.get("input"), resolved.get("output")))
    print()
    for kind in ("input", "output"):
        spec = getattr(voice, f"{kind}_device")
        print(f"  voice.{kind}_device = {spec!r}" + ("   (system default)" if not spec else ""))
    if problems:
        print()
        for problem in problems:
            print(f"  {problem}")
        return 1
    print()
    print("  Set voice.input_device to part of a device name rather than an index.")
    print("  Indices move when a USB microphone or a headset connects.")
    return 0


def cmd_audio_check(config: Config, args) -> int:
    from .audio import SoundDeviceBackend

    try:
        backend = SoundDeviceBackend()
    except AudioError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1

    keep = Path(args.keep).expanduser().resolve() if args.keep else None
    return run_check(
        config,
        backend,
        seconds=args.seconds,
        use_trigger=args.hold,
        keep=keep,
        out=sys.stdout,
    )


def _keyterm_plan(config: Config):
    """The hint list actually sent: config terms plus derived account names."""
    from datetime import date

    from .accounts import scan_all
    from .keyterms import derive_keyterms
    from .vault import Vault

    if not config.stt.keyterms_from_accounts:
        from .keyterms import KeytermPlan

        terms = config.stt.keyterms[: config.stt.max_hints]
        return KeytermPlan(terms=terms, cap=config.stt.max_hints)

    scans, _ = scan_all(Vault(config.vault), config.vault.accounts, config.accounts.exclude_files)
    return derive_keyterms(
        scans,
        config.stt.keyterms,
        cap=config.stt.max_hints,
        as_of=date.today(),
        skip_statuses=config.accounts.skip_statuses,
    )


def _local_config_path(config: Config) -> Path:
    from .config import LOCAL_SUFFIX

    if config.local_path:
        return config.local_path
    source = config.source_path or Path("ranger.toml")
    return source.with_name(source.stem + LOCAL_SUFFIX)


def _announce_inbox(config: Config, paint) -> None:
    """Catch-up on return: what was surfaced while nobody was looking."""
    try:
        pending = _inbox(config).pending()
    except Exception:
        return
    if not pending:
        return
    word = "notice" if len(pending) == 1 else "notices"
    print(paint(f"  {len(pending)} {word} waiting: {pending[0].title}", YELLOW))
    if len(pending) > 1:
        print(paint(f"  and {len(pending) - 1} more. 'ranger inbox' to read them.", DIM))
    else:
        print(paint("  'ranger inbox' to read it.", DIM))


def _inbox(config: Config):
    from .heartbeat import Inbox
    from .vault import Vault

    return Inbox(Vault(config.vault), config.vault.inbox)


def cmd_inbox(config: Config, args) -> int:
    """Tier 5. What was surfaced while you were away, held until you clear it."""
    paint = _colour(sys.stdout.isatty())
    inbox = _inbox(config)
    notices = inbox.all() if args.all else inbox.pending()

    if args.dismiss is not None:
        pending = inbox.pending()
        if not 1 <= args.dismiss <= len(pending):
            print(f"  there is no pending notice {args.dismiss}.", file=sys.stderr)
            return 1
        chosen = pending[args.dismiss - 1]
        inbox.dismiss(chosen)
        print(f"  dismissed: {chosen.title}")
        print(paint(f"  cleared in {chosen.path}, not just on screen.", DIM))
        return 0

    if not notices:
        print("  nothing waiting.")
        return 0

    for index, notice in enumerate(notices, start=1):
        mark = paint(" (dismissed)", DIM) if notice.dismissed else ""
        print(f"  {index}. {paint(notice.title, BOLD)}{mark}")
        stamp = human_datetime(notice.created)
        print(paint(f"     {stamp}  {notice.path.name}", DIM))
        for line in notice.body.splitlines()[: 3 if not args.full else 10_000]:
            print(f"     {line}")
        if not args.full and len(notice.body.splitlines()) > 3:
            print(paint("     ...", DIM))
        print()
    if not args.all:
        print(paint("  'ranger inbox dismiss N' clears one in the vault.", DIM))
    return 0


def _kill_switch(config: Config):
    from .heartbeat import KILL_SWITCH_FILE, KillSwitch
    from .vault import Vault

    return KillSwitch(Vault(config.vault), config.vault.ranger / KILL_SWITCH_FILE)


def cmd_pause(config: Config, args) -> int:
    """The kill switch. Stops everything proactive, leaves conversation alone."""
    switch = _kill_switch(config)
    path = switch.set(paused=True)
    print("  Proactive behaviour is paused. The heartbeat will surface nothing.")
    print("  You can still talk to Jarvis normally.")
    print(f"  {path}")
    print("  Resume with 'ranger resume'.")
    return 0


def cmd_resume(config: Config, args) -> int:
    switch = _kill_switch(config)
    path = switch.set(paused=False)
    print(f"  Proactive behaviour is running again.  {path}")
    return 0


def cmd_log(config: Config, args) -> int:
    """The audit trail. What ran, why, and what it cost."""
    from .audit import AuditLog
    from .vault import Vault

    log = AuditLog(Vault(config.vault), config.vault.log)
    days = log.days()
    if not days:
        print("  nothing logged yet.")
        return 0
    from datetime import date as _date

    when = _date.fromisoformat(args.day) if args.day else days[0]
    text = log.read(when)
    if not text:
        print(f"  nothing logged on {when}.")
        return 1
    print(text.rstrip())
    if len(days) > 1:
        print()
        print(f"  other days: {', '.join(d.isoformat() for d in days[1:6])}")
    return 0


class _Tee:
    """Write to a stream and to a file at once, a line at a time.

    A scheduled task has no terminal, and every Windows bug in this build
    surfaced because there was terminal output to read. This is what replaces
    it: the same words, in a file, with the date on the front so a week of runs
    is still readable.
    """

    def __init__(self, stream, handle) -> None:
        self.stream = stream
        self.handle = handle
        self._fresh = True

    def write(self, text: str) -> int:
        self.stream.write(text)
        for part in text.splitlines(keepends=True):
            if self._fresh and part.strip():
                self.handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  ")
            self.handle.write(part)
            self._fresh = part.endswith("\n")
        # Flushed on every write, not left to the 8KB buffer. A server runs for
        # hours and then exits, so an unflushed banner means the log is empty
        # for the entire time it would have been useful. The cost is a syscall
        # per line on a file nothing writes to in a tight loop.
        self.handle.flush()
        return len(text)

    def flush(self) -> None:
        self.stream.flush()
        self.handle.flush()

    def isatty(self) -> bool:
        # False, so nothing downstream decides to emit colour escapes into a
        # file the operator is going to read in Notepad.
        return False


@contextmanager
def _logging_to(path: str | None):
    """Send stdout and stderr to a file as well, if one was asked for."""
    if not path:
        yield
        return

    target = Path(path).expanduser()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = target.open("a", encoding="utf-8")
    except OSError as exc:
        print(f"  cannot write the log at {target}: {exc}", file=sys.stderr)
        yield
        return

    out, err = sys.stdout, sys.stderr
    sys.stdout = _Tee(out, handle)
    sys.stderr = _Tee(err, handle)
    try:
        yield
    finally:
        sys.stdout, sys.stderr = out, err
        handle.close()


def cmd_heartbeat(config: Config, args) -> int:
    """Tier 5. The loop, or one pass of it."""
    with _logging_to(getattr(args, "log", None)):
        return _run_heartbeat(config, args)


def _run_heartbeat(config: Config, args) -> int:
    import asyncio

    from .heartbeat import Heartbeat, build_checks
    from .toolset import build_registry
    from .vault import Vault

    paint = _colour(sys.stdout.isatty())
    if not config.heartbeat.enabled and not args.once and not args.force:
        print("  heartbeat.enabled is false, so the loop will not start.")
        return 0

    from .audit import AuditLog

    vault = Vault(config.vault)
    switch = _kill_switch(config)
    if switch.engaged() and not args.force:
        print("  paused: the kill switch is engaged. 'ranger resume' to start again.")
        return 0
    beat = Heartbeat(
        config,
        _inbox(config),
        build_checks(config, build_registry(config, vault), vault),
        kill_switch=switch,
        audit=AuditLog(vault, config.vault.log),
    )

    known = {c.name for c in beat.checks}
    forced = tuple(args.force or ())
    unknown = [n for n in forced if n.lower() not in known and n.lower() != "all"]
    if unknown:
        print(f"  no such check: {', '.join(unknown)}", file=sys.stderr)
        print(f"  there is {', '.join(sorted(known))}.", file=sys.stderr)
        return 1

    async def once() -> int:
        report = await beat.tick(force=forced)
        if not report.outcomes:
            print("  no checks are registered.")
            return 0
        for outcome in report.outcomes:
            colour = {
                "surfaced": TEAL, "timed_out": YELLOW, "failed": RED,
            }.get(outcome.state, DIM)
            print(f"  {paint(outcome.render(), colour)}")
        if not report.ran and not forced:
            print()
            print(paint("  Nothing ran. Use --force to run one now, ignoring both the", DIM))
            print(paint("  schedule and quiet hours: ranger heartbeat --once --force morning", DIM))
        return 0

    if args.once:
        return asyncio.run(once())

    schedule = config.schedule
    print(paint("Jarvis heartbeat", BOLD + TEAL))
    print(paint(
        f"  morning surface at {schedule.morning_hour:02d}:00, quiet "
        f"{schedule.quiet_start_hour:02d}:00 to {schedule.quiet_end_hour:02d}:00", DIM))
    print(paint(f"  waking every {config.heartbeat.interval_seconds}s. Ctrl-C to stop.", DIM))
    print(paint("  notices go to Ranger/inbox/ and wait there until you clear them.", DIM))
    try:
        asyncio.run(beat.run())
    except KeyboardInterrupt:
        print()
    return 0


def cmd_memory(config: Config, args) -> int:
    """Tier 4. What is remembered, and how much of the budget it is using."""
    from .memory import load_memory
    from .vault import Vault

    paint = _colour(sys.stdout.isatty())
    vault = Vault(config.vault)
    context = load_memory(vault, config.vault.memory, config.memory.reserve_chars)

    target = config.vault.memory / config.memory.file
    print(f"  {target}")
    print()
    if context.empty:
        print("  nothing remembered yet.")
        print("  Jarvis writes here when you tell it something worth keeping, and you")
        print("  can add lines by hand in the same format.")
        return 0

    by_topic: dict[str, list] = {}
    for fact in context.facts:
        by_topic.setdefault(fact.topic or "General", []).append(fact)
    for topic in sorted(by_topic):
        print(f"  {paint(topic, BOLD)}")
        for fact in by_topic[topic]:
            when = fact.learned.isoformat() if fact.learned else "  by hand "
            print(f"    {paint(when, DIM)}  {fact.text}")
        print()

    used = context.total_chars
    knowledge_left = max(0, config.context.budget_chars - used)
    print(paint(
        f"  {len(context.facts)} facts, {used} of {config.memory.reserve_chars} reserved chars.",
        DIM,
    ))
    print(paint(
        f"  knowledge gets the remaining {knowledge_left} of "
        f"{config.context.budget_chars}.", DIM,
    ))
    for warning in context.warnings:
        print(paint(f"  {warning}", YELLOW))
    return 0


def cmd_keyterms(config: Config, args) -> int:
    """Show exactly which vocabulary hints would be sent, and why."""
    paint = _colour(sys.stdout.isatty())
    plan = _keyterm_plan(config)

    print(f"  {len(plan.terms)} hints, cap {plan.cap}, parameter "
          f"{__import__('ranger.stt', fromlist=['x']).hint_parameter(config.stt.model)!r} "
          f"for model {config.stt.model}")
    print(f"  {plan.accounts_considered} account notes considered"
          + (f", {plan.skipped_unconfirmed} unconfirmed skipped" if plan.skipped_unconfirmed else ""))
    print()
    limit = None if args.all else 25
    for candidate in plan.kept[:limit]:
        score = "always" if candidate.score == float("inf") else f"{candidate.score:5.2f}"
        print(f"  {score}  {candidate.term:<24} {paint(candidate.reason, DIM)}")
    if limit and len(plan.kept) > limit:
        print(paint(f"  ... and {len(plan.kept) - limit} more, use --all", DIM))

    if plan.over_cap:
        print()
        print(paint(f"  {len(plan.cut)} names did not fit and are not hinted:", YELLOW))
        for candidate in plan.cut[:10]:
            print(paint(f"    {candidate.term:<24} {candidate.reason}", DIM))
        if len(plan.cut) > 10:
            print(paint(f"    ... and {len(plan.cut) - 10} more", DIM))
        print()
        print("  These are the least recently worked accounts. Raise stt.max_hints to")
        print("  include them, but watch the latency: hints are not free.")
    return 0


def cmd_voices(config: Config, args) -> int:
    """Tier 3c. What the account has, so a voice can be chosen."""
    import asyncio

    from .tts import SpeechError, build_speaker

    try:
        api_key = require_api_key("ELEVENLABS_API_KEY")
    except ConfigError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1

    speaker = build_speaker(config.tts, api_key)
    try:
        voices = asyncio.run(speaker.voices())
    except SpeechError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1

    if not voices:
        print("  the account has no voices on it.")
        return 1

    paint = _colour(sys.stdout.isatty())
    for voice in voices:
        marker = "  <- tts.voice_id" if voice.voice_id == config.tts.voice_id else ""
        print(f"  {voice.voice_id}  {voice.name:<18} {paint(voice.describe(), DIM)}{marker}")
    print()
    print(f"  Put the id in {_local_config_path(config)}, which is git-ignored so it")
    print("  survives every pull:")
    print()
    print("    [tts]")
    print('    voice_id = "<the id above>"')
    print()
    print("  then:")
    print('    ranger say "Rod owes you confirmed volumes before you can price the changeover."')
    return 0


def cmd_say(config: Config, args) -> int:
    """Tier 3c. Text in, sound out. No microphone, no transcription."""
    import asyncio
    import time

    from .audio import AudioError, SoundDeviceBackend, resolve_device, write_wav
    from .tts import SpeechError, build_speaker, decode

    text = " ".join(args.text).strip()
    if not text:
        print("  nothing to say.", file=sys.stderr)
        return 1

    try:
        api_key = require_api_key("ELEVENLABS_API_KEY")
    except ConfigError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1

    tts = config.tts
    if args.voice:
        from dataclasses import replace

        tts = replace(tts, voice_id=args.voice)

    print(f'  saying: "{text}"')
    print(f"  voice {tts.voice_id or '(unset)'}, model {tts.model_id}, {tts.output_format}")

    speaker = build_speaker(tts, api_key)

    async def collect() -> tuple[bytes, int]:
        chunks: list[bytes] = []
        first_at: float | None = None
        started = time.monotonic()
        async for chunk in speaker.stream(text):
            if first_at is None:
                first_at = time.monotonic() - started
            chunks.append(chunk)
        return b"".join(chunks), int((first_at or 0) * 1000)

    try:
        audio, first_ms = asyncio.run(collect())
    except SpeechError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1

    if not audio:
        print("  ElevenLabs returned no audio.", file=sys.stderr)
        return 1

    try:
        pcm, rate = decode(audio, tts.output_format)
    except SpeechError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1

    seconds = len(pcm) / (2 * rate) if rate else 0
    print(f"  {len(audio)} bytes, {seconds:.1f}s, first chunk after {first_ms} ms")

    if args.keep:
        try:
            written = write_wav(Path(args.keep).expanduser().resolve(), pcm, samplerate=rate, channels=1)
            print(f"  saved {written}")
        except OSError as exc:
            print(f"  could not save: {exc}")

    if args.no_play:
        return 0

    try:
        backend = SoundDeviceBackend()
        device = resolve_device(config.voice.output_device, backend.devices(), kind="output")
        print("  playing ...")
        backend.play(pcm, samplerate=rate, channels=1, device=device)
    except AudioError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_transcribe(config: Config, args) -> int:
    """Tier 3b. A WAV in, a transcript out. No microphone involved."""
    import asyncio

    from .audio import AudioError, read_wav
    from .stt import TranscriptionError, build_transcriber

    paint = _colour(sys.stdout.isatty())
    path = Path(args.file).expanduser().resolve()
    if not path.is_file():
        print(f"  no such file: {path}", file=sys.stderr)
        return 1

    try:
        pcm, rate, channels = read_wav(path)
    except AudioError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1

    stt = config.stt
    if args.model:
        from dataclasses import replace

        stt = replace(stt, model=args.model)

    try:
        api_key = require_api_key("DEEPGRAM_API_KEY")
    except ConfigError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1

    seconds = len(pcm) / (2 * max(1, channels) * rate) if rate else 0
    hints = not args.no_hints
    print(f"  {path.name}, {seconds:.1f}s, {rate} Hz, {channels} channel")
    hint_count = len(_keyterm_plan(config).terms) if hints else 0
    print(f"  model {stt.model}, {hint_count} hints" if hint_count else f"  model {stt.model}, hinting off")
    print()

    plan = _keyterm_plan(config)
    if hints and plan.terms:
        from dataclasses import replace as _replace

        stt = _replace(stt, keyterms=plan.terms)
    transcriber = build_transcriber(stt, api_key)
    try:
        transcript = asyncio.run(transcriber.transcribe(pcm_wav_bytes(path), hints=hints))
    except TranscriptionError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1

    if transcript.empty:
        print(paint("  Deepgram heard nothing at all.", YELLOW))
        print("  The audio may be silent. Run 'ranger audio check' to measure it.")
        return 1

    print(paint("  heard:", BOLD), transcript.text)
    print()
    print(
        f"  confidence {transcript.confidence:.0%}, {transcript.latency_ms} ms"
        + (f", {len(transcript.hinted)} hints sent" if transcript.hinted else "")
    )

    shaky = transcript.shaky_words
    if shaky:
        listing = ", ".join(f"{w.text} ({w.confidence:.0%})" for w in shaky[:8])
        print(paint(f"  least certain: {listing}", DIM))

    if args.expect:
        result = compare(args.expect, transcript.text)
        print()
        print(paint("  said: ", BOLD), args.expect)
        if result.perfect:
            print(paint("  every word matched.", TEAL))
        else:
            print(
                paint(
                    f"  {result.errors} of {len(result.expected)} words differ "
                    f"({result.word_error_rate:.0%} word error rate)",
                    YELLOW,
                )
            )
            print(result.render())
            print()
            print(paint("  If a named account is wrong, add it to stt.keyterms and", DIM))
            print(paint("  run this again with the same file to see whether it helped.", DIM))
        return 0 if result.perfect else 0
    return 0


def pcm_wav_bytes(path: Path) -> bytes:
    """Deepgram wants the container, not the raw frames."""
    return path.read_bytes()


def cmd_schedule(config: Config, args: Any) -> int:
    """Register the heartbeat with Task Scheduler, or say what it would do."""
    from .schedule import (
        DEFAULT_INTERVAL_MINUTES,
        INTERESTING,
        INTERFACE_TASK_NAME,
        ScheduleError,
        TASK_NAME,
        build_interface_plan,
        build_plan,
        install,
        on_windows,
        remove,
        status,
        to_xml,
    )

    paint = _colour(sys.stdout.isatty())
    action = getattr(args, "schedule_command", None) or "show"
    interface = getattr(args, "interface", False) or getattr(args, "interface_first", False)
    name = INTERFACE_TASK_NAME if interface else TASK_NAME

    if action == "remove":
        try:
            gone = remove(name)
        except ScheduleError as exc:
            print(paint(f"  {exc}", RED), file=sys.stderr)
            return 1
        print(
            paint(f"  {name} removed.", TEAL)
            if gone
            else paint(f"  there was no task called {name}.", DIM)
        )
        return 0

    try:
        if interface:
            plan = build_interface_plan(
                config,
                log_path=Path(args.log).expanduser() if getattr(args, "log", None) else None,
                windowless=getattr(args, "windowless", False),
            )
        else:
            plan = build_plan(
                config,
                interval_minutes=getattr(args, "every", None) or DEFAULT_INTERVAL_MINUTES,
                log_path=Path(args.log).expanduser() if getattr(args, "log", None) else None,
                windowless=getattr(args, "windowless", False),
            )
    except ScheduleError as exc:
        print(paint(f"  {exc}", RED), file=sys.stderr)
        return 1

    if interface and not getattr(args, "windowless", False) and on_windows():
        print(paint("  ranger.exe is a console program and 'ranger ui' never exits, so", YELLOW))
        print(paint("  this leaves a console window open for the whole session. Closing", YELLOW))
        print(paint("  it stops the server. --windowless runs it through pythonw instead.", YELLOW))
        print()

    if action == "xml":
        print(to_xml(plan))
        return 0

    if action == "install":
        try:
            install(plan)
        except ScheduleError as exc:
            print(paint(f"  {exc}", RED), file=sys.stderr)
            return 1
        print(paint(f"Registered {plan.name}", BOLD))
        for line in plan.describe():
            print(f"  {line}")
        print()
        if plan.at_logon:
            print(paint("  It starts twenty seconds after you log on and stays up, so a", DIM))
            print(paint("  pinned icon only has to open a window at a server already there.", DIM))
        else:
            print(paint("  It runs whether or not a terminal is open, only while you are", DIM))
            print(paint("  logged on, and a run missed while the machine was asleep happens", DIM))
            print(paint("  shortly after it wakes rather than being skipped.", DIM))
        print()
        print(paint(f"  Watch it:  Get-Content -Wait '{plan.log_path}'", DIM))
        flag = " --interface" if plan.at_logon else ""
        print(paint(f"  Check it:  ranger schedule{flag}", DIM))
        print(paint(f"  Remove it: ranger schedule remove{flag}", DIM))
        return 0

    # show
    print(paint(f"{plan.name}", BOLD))
    for line in plan.describe():
        print(f"  {line}")
    print()

    if not on_windows():
        print(paint("  Task Scheduler is Windows only, so nothing is registered here.", DIM))
        print(paint("  'ranger schedule xml' prints the definition anywhere.", DIM))
        return 0

    current = status(name)
    if current is None:
        print(paint("  not registered. 'ranger schedule install' to register it.", YELLOW))
        return 0

    print(paint("As Task Scheduler has it", BOLD))
    for field in INTERESTING:
        if field in current:
            print(f"  {field:<20} {current[field]}")
    result = current.get("Last Result", "").strip()
    if result and result not in {"0", "267011"}:
        # 267011 is "has not run yet", which is not a failure.
        print()
        print(paint(f"  Last Result is {result}, so the last run did not end cleanly.", YELLOW))
        print(paint(f"  The log says why: {plan.log_path}", YELLOW))
    return 0


def cmd_alias(config: Config, args: Any) -> int:
    """Accounts the CRM exports twice, folded into one at read time."""
    from .accounts import scan_all
    from .aliases import AliasFile, suggest

    paint = _colour(sys.stdout.isatty())
    vault = Vault(config.vault)
    scans, _ = scan_all(vault, config.vault.accounts, config.accounts.exclude_files)
    names = [scan.name for scan in scans]
    store = AliasFile(vault, config.vault.ranger)
    action = getattr(args, "alias_command", None) or "show"

    if action == "suggest":
        current = store.load()
        mapped = {a.variant.casefold() for a in current.pairs} | {
            a.canonical.casefold() for a in current.pairs
        }
        found = [
            s for s in suggest(names)
            if s.left.casefold() not in mapped and s.right.casefold() not in mapped
        ]
        if not found:
            print(paint("  nothing looks like the same account written twice.", DIM))
            return 0

        print(paint(f"{len(found)} pairs that might be one account", BOLD))
        print(paint("  Jarvis never merges these. Pick the name to keep and run:", DIM))
        print(paint("    ranger alias add \"<other name>\" \"<name to keep>\"", DIM))
        print()
        for item in found:
            print(f"  {item.describe()}")
        return 0

    if action in {"add", "remove"}:
        variant = " ".join(args.variant).strip()
        if action == "remove":
            print(
                paint(f"  {variant} is a separate account again.", TEAL)
                if store.remove(variant)
                else paint(f"  {variant} was not mapped to anything.", DIM)
            )
            return 0

        canonical = " ".join(args.canonical).strip()
        if not variant or not canonical:
            print("usage: ranger alias add \"<other name>\" \"<name to keep>\"", file=sys.stderr)
            return 2
        if canonical.casefold() not in {n.casefold() for n in names}:
            print(paint(f"  {canonical!r} is not an account in the vault.", RED), file=sys.stderr)
            return 1
        if variant.casefold() == canonical.casefold():
            print(paint("  those are the same name.", YELLOW), file=sys.stderr)
            return 1

        if store.add(variant, canonical):
            print(paint(f"  {variant} now reads as {canonical}.", TEAL))
            print(paint("  Their activity is added together, so this account may move up", DIM))
            print(paint("  the morning brief. That is the point of it.", DIM))
        else:
            print(paint(f"  {variant} is already mapped.", DIM))
        print(paint(f"  {store.path}, hand-editable", DIM))
        return 0

    aliases = store.load(names)
    if aliases.empty and not aliases.stale:
        print(paint("  nothing is folded together.", DIM))
        print(paint("  'ranger alias suggest' looks for accounts exported twice.", DIM))
        return 0

    if aliases.pairs:
        print(paint(f"{len(aliases.pairs)} folded into one account at read time", BOLD))
        for alias in sorted(aliases.pairs, key=lambda a: a.canonical.lower()):
            print(f"  {alias.variant}  ->  {alias.canonical}")
    for problem in aliases.stale:
        print(paint(f"  stale: {problem}", YELLOW))
    print(paint(f"  {store.path}", DIM))
    return 0


def cmd_wake(config: Config, args: Any) -> int:
    """Install the hotword models, or say what is missing.

    openWakeWord's wheel contains no models: not the hotwords, and not the two
    feature models every hotword runs on. They come from the project's own
    GitHub release on first use, and that is a network fetch of a binary that
    then listens to a room, so it is a command the operator runs rather than
    something that happens quietly the first time they arm.
    """
    from .wake import (
        MODEL_SOURCE,
        PUBLISHED,
        WakeUnavailable,
        available,
        find_model,
        missing_models,
        models_folder,
        install_models,
    )

    paint = _colour(sys.stdout.isatty())

    ready, why = available()
    if not ready:
        print(paint(f"  {why}", YELLOW))
        return 1

    action = getattr(args, "wake_command", None) or "show"
    name = getattr(args, "model", None) or config.wake.model

    if action == "install":
        if Path(name).suffix:
            print(paint(f"  wake.model is {name}, which is a file you trained.", YELLOW))
            print("  Nothing to download for it. Installing the shared feature models only.")
            name = "hey_jarvis"
        try:
            for path in install_models(name, log=lambda line: print(f"  {line}")):
                print(paint(f"  ok  {path.name}  {path.stat().st_size // 1024} KB", TEAL))
        except WakeUnavailable as exc:
            print(paint(f"  {exc}", YELLOW))
            return 1
        print()
        print(f"  Set wake.model = \"{name}\" and wake.enabled = true, then 'ranger doctor'.")
        return 0

    print(f"  models live in {models_folder()}")
    print(f"  they come from {MODEL_SOURCE}")
    print()
    try:
        gaps = missing_models(name)
    except WakeUnavailable as exc:
        print(paint(f"  {exc}", YELLOW))
        return 1

    found = find_model(name) if not Path(name).suffix else Path(name)
    if not gaps:
        print(paint(f"  ok  hands free has everything it needs for {config.wake.phrase!r}", TEAL))
        print(f"      {found}")
        return 0

    print(paint("  hands free cannot work: no model is installed", YELLOW))
    for gap in gaps:
        print(f"      missing {gap}")
    print()
    if name in PUBLISHED:
        print("  Run: ranger wake install")
    else:
        print(f"  {name!r} is not one openWakeWord publishes ({', '.join(PUBLISHED)}),")
        print("  so it has to be trained: see scripts/train_wake_word.py")
    return 1


def cmd_mic(config: Config, args: Any) -> int:
    """What the microphone check sees. The way to confirm it works at all.

    Hands free refuses to arm while another application holds the microphone,
    and that check reads a Windows registry key which cannot be exercised
    anywhere without one. This makes it checkable in one command: open a call,
    run this, and see whether Jarvis sees what the taskbar sees.
    """
    from .micuse import describe, may_arm

    paint = _colour(sys.stdout.isatty())
    for line in describe():
        print(f"  {line}" if line else "")
    print()

    verdict = may_arm()
    if verdict.allowed:
        print(paint("  hands free could arm: " + verdict.reason, TEAL))
    else:
        print(paint("  hands free would refuse: " + verdict.reason, YELLOW))
    return 0


def cmd_dormant(config: Config, args: Any) -> int:
    """Answer the morning brief's one decision, and see the answers so far."""
    from .brief import BriefStore

    paint = _colour(sys.stdout.isatty())
    vault = Vault(config.vault)
    store = BriefStore(vault, config.vault.ranger, config.brief)

    if not args.account:
        names = sorted(store.dormant_lines())
        if not names:
            print(paint("  nothing is dormant. The morning brief considers every account.", DIM))
        else:
            print(paint(f"{len(names)} dormant, not surfaced in the morning brief", BOLD))
            for name in names:
                print(f"  {name}")
        print(paint(f"  {store.dormant_path}", DIM))
        return 0

    name = " ".join(args.account).strip()
    try:
        if args.remove:
            changed = store.wake(name)
            print(
                paint(f"  {name} is back in the morning brief.", TEAL)
                if changed
                else paint(f"  {name} was not dormant.", DIM)
            )
        else:
            changed = store.sleep(name)
            print(
                paint(f"  {name} will not be surfaced again.", TEAL)
                if changed
                else paint(f"  {name} was already dormant.", DIM)
            )
    except Exception as exc:
        print(paint(f"  could not write {store.dormant_path}: {exc}", RED), file=sys.stderr)
        return 1
    print(paint(f"  {store.dormant_path}, hand-editable", DIM))
    return 0


def cmd_drafts(config: Config, args: Any) -> int:
    """List drafts, clear one, or see what has been cleared.

    Clearing MOVES the draft to Ranger/drafts/cleared/. It never unlinks it:
    delete-never is the property the Accounts/ append design rests on, and it
    is not being weakened so a panel looks tidier.
    """
    from .audit import AuditLog
    from .ownfiles import clear, listing
    from .vault import Vault

    paint = _colour(sys.stdout.isatty())
    vault = Vault(config.vault)
    action = getattr(args, "drafts_command", None) or "show"

    if action == "clear":
        name = " ".join(getattr(args, "name", []) or []).strip()
        if not name:
            print("usage: ranger drafts clear <name>", file=sys.stderr)
            return 2
        outcome, candidates = clear(
            vault, config, name, audit=AuditLog(vault, config.vault.log)
        )
        if outcome is None:
            if candidates:
                print(paint(f"  {name!r} matches {len(candidates)} drafts:", YELLOW))
                for item in candidates[:8]:
                    print(f"    {item.name}")
                print(paint("  nothing was cleared. Name one of them.", DIM))
                return 1
            print(paint(f"  no draft matches {name!r}", YELLOW))
            return 1
        print(paint(f"  {outcome.describe()}", TEAL))
        if not outcome.already:
            print(paint("  moved, not deleted. 'ranger drafts cleared' lists it", DIM))
        return 0

    cleared = action == "cleared"
    files = listing(vault, config, "drafts", cleared=cleared)
    where = "cleared" if cleared else "held"
    if not files:
        print(paint(f"  no {where} drafts", DIM))
        return 0
    print(paint(f"  {len(files)} {where}", BOLD))
    for item in files:
        when = f"{item.created}  " if item.created else ""
        print(f"  {when}{item.name}")
        if item.title:
            print(paint(f"      {item.title}", DIM))
    return 0


def cmd_gp(config: Config, args: Any) -> int:
    """The GP tracker at the terminal. The same figures the panel draws.

    Show, add and history run through `gp.summary` and `gp.record`, which is
    what the panel calls too. One computation, so the number on screen and the
    number in the terminal cannot disagree -- and neither of them was produced
    by a model.
    """
    from . import gp
    from .vault import Vault

    paint = _colour(sys.stdout.isatty())
    vault = Vault(config.vault)
    action = getattr(args, "gp_command", None) or "show"
    today = date.today()
    symbol = config.gp.currency

    if action == "add":
        amount = " ".join(getattr(args, "amount", []) or []).strip()
        try:
            entry = gp.record(
                config,
                vault,
                amount,
                period=getattr(args, "period", "") or "",
                note=getattr(args, "note", "") or "",
            )
        except gp.BadEntry as exc:
            print(paint(f"  {exc}", YELLOW), file=sys.stderr)
            return 2
        ledger, _ = gp.summary(config, vault, today)
        supersedes = ledger.corrections(entry.period)
        print(paint(f"  {entry.period}  {gp.money(entry.amount, symbol)}", TEAL))
        if supersedes:
            was = "figure" if supersedes == 1 else "figures"
            print(paint(
                f"  corrects the previous {was} for {entry.period}. "
                "Nothing was overwritten; the earlier entry is still there.", DIM
            ))
        if entry.path:
            print(paint(f"  {entry.path.name}", DIM))
        return 0

    ledger, total = gp.summary(config, vault, today)

    if action == "history":
        year = str(getattr(args, "year", "") or "").strip()
        entries = sorted(ledger.entries, key=lambda e: (e.period, gp.order(e)))
        if year:
            entries = [e for e in entries if e.year == year]
        if not entries:
            print(paint("  nothing entered yet", DIM))
            return 0
        current = {e.path for e in ledger.current().values()}
        print(paint(f"  {len(entries)} entries, superseded ones included", BOLD))
        for entry in entries:
            mark = " " if entry.path in current else paint("superseded", DIM)
            stamp = entry.recorded.strftime("%Y-%m-%d %H:%M")
            print(f"  {entry.period}  {gp.money(entry.amount, symbol):>14}  {stamp}  {mark}")
            if entry.note:
                print(paint(f"      {entry.note.splitlines()[0][:80]}", DIM))
        return 0

    # show
    if total.empty:
        # Absence, in words. Never a nought: a zero looks like a figure that
        # was measured and it would get acted on.
        print(paint("  no GP entered yet", DIM))
        print(paint("  ranger gp add 48250            records this month", DIM))
        print(paint("  ranger gp add 44000 --period 2026-08", DIM))
        return 0

    if total.ytd is None:
        print(paint(f"  nothing entered for {total.year} yet", YELLOW))
    else:
        months = "month" if total.months_counted == 1 else "months"
        print(paint(
            f"  {total.year} YTD  {gp.money(total.ytd, symbol)}"
            f"   over {total.months_counted} {months}", BOLD
        ))
    if total.mtd is None:
        print(paint(f"  {total.month}      not entered", DIM))
    else:
        print(f"  {total.month}      {gp.money(total.mtd, symbol)}")
    if total.last_year_total is not None:
        print(paint(
            f"  {total.last_year}       {gp.money(total.last_year_total, symbol)}", DIM
        ))

    age = total.days_old(today)
    if total.as_of:
        stale = age is not None and age > config.gp.stale_after_days
        line = f"  as of {human_datetime(total.as_of)}"
        if age == 0:
            line += " (today)"
        elif age is not None:
            line += f" ({age} days ago)" if age != 1 else " (1 day ago)"
        print(paint(line, YELLOW if stale else DIM))
        if stale:
            print(paint(
                f"  older than gp.stale_after_days ({config.gp.stale_after_days}). "
                "The panel marks it stale too.", YELLOW
            ))
    if total.corrections:
        print(paint(f"  {total.corrections} correction(s) for {total.month}", DIM))
    if ledger.unreadable:
        print(paint(f"  {len(ledger.unreadable)} file(s) in Ranger/gp could not be read:", YELLOW))
        for line in ledger.unreadable[:5]:
            print(paint(f"      {line}", DIM))
    return 0


def cmd_snapshot(config: Config, args: Any) -> int:
    """Set up the vault's local history, or take today's snapshot by hand."""
    from .snapshot import (
        SnapshotRefused,
        commit,
        initialise,
        is_repository,
        last_snapshot,
        megabytes,
        remotes,
        repository_size,
        survey,
    )

    paint = _colour(sys.stdout.isatty())
    root = config.vault.root
    vault = config.vault
    action = getattr(args, "snapshot_command", None) or "show"

    if action == "init":
        try:
            state, seen = initialise(
                root,
                max_file_bytes=vault.snapshot_max_file_bytes,
                max_total_bytes=vault.snapshot_max_total_bytes,
            )
        except SnapshotRefused as exc:
            print(paint(f"  {exc}", YELLOW))
            return 1
        print(paint(f"  {root}: {state}", TEAL))
        # Said before anything is committed, because a snapshot that quietly
        # swallows a gigabyte is not a backup, it is a surprise.
        print()
        print("  the first snapshot would hold:")
        for line in seen.describe():
            print(f"    {line}")
        print()
        print(paint("  no remote, and the daily snapshot refuses to run if one appears", DIM))
        print(paint("  nothing has been committed yet. Run: ranger snapshot now", DIM))
        return 0

    if action == "now":
        result = commit(root, datetime.now().date(),
                        max_file_bytes=vault.snapshot_max_file_bytes)
        print(paint(f"  {result.describe()}", TEAL if result.taken else YELLOW))
        return 0 if result.taken else 1

    if not is_repository(root):
        print(paint(f"  {root} is not a git repository. Run: ranger snapshot init", YELLOW))
        return 1
    try:
        found = remotes(root)
    except SnapshotRefused as exc:
        print(paint(f"  {exc}", YELLOW))
        return 1
    if found:
        print(paint(f"  REMOTE PRESENT: {', '.join(found)}. Snapshots are refused.", YELLOW))
        print("  The vault holds customer email and pricing. Remove the remote.")
        return 1

    count, on_disk = repository_size(root)
    print(paint(f"  ok  {root} is a local repository with no remote", TEAL))
    print(f"      {count} files tracked, {megabytes(on_disk)} on disk")
    print(f"      last snapshot: {last_snapshot(root) or 'none yet'}")
    pending = survey(root, vault.snapshot_max_file_bytes)
    if pending.files or pending.oversized:
        print(f"      waiting: {pending.count} changed, {megabytes(pending.total)}")
    for name, size in pending.oversized[:5]:
        print(paint(f"      too big to snapshot: {name} ({megabytes(size)})", YELLOW))
    return 0


def cmd_accounts_migrate(config: Config, args: Any) -> int:
    """Put the marker in every account note, once.

    Idempotent by construction: a note is migrated only when it has exactly
    zero markers, so a second run finds one everywhere and changes nothing.
    """
    from .marker import migrate

    paint = _colour(sys.stdout.isatty())
    folder = config.vault.accounts
    if not folder.is_dir():
        print(f"  no accounts folder at {folder}", file=sys.stderr)
        return 1

    dry = bool(getattr(args, "dry_run", False))
    report = migrate(None, folder, dry_run=dry)

    if dry:
        from .marker import survey_endings

        print(paint("  dry run, nothing written", DIM))
        # What is about to be migrated, before it is. Jarvis never converts
        # what is already on disk, so this decides nothing -- but the vault
        # came out of four separate CRM exports and a mixed count above zero
        # is worth knowing beforehand rather than afterwards.
        census = survey_endings(folder)
        for line in census.lines():
            print(paint(line, YELLOW) if "mixed" in line or "unreadable" in line else line)
        print()
    print(f"  {len(report.migrated)} migrated, {len(report.already)} already marked")
    for name, why in report.refused:
        print(paint(f"  refused {name}: {why}", YELLOW))
    if report.migrated and not dry:
        print(paint(f"  {folder}", DIM))
    return 1 if report.refused else 0


def cmd_vault_guard(config: Config, args: Any) -> int:
    """What a rebuild of the vault would destroy.

    build_vault.py regenerates account notes from the CRM export. Everything
    above the marker is its to replace; everything below is Jarvis's and is not
    regenerable from anything. This is the check that script has to make before
    it writes, runnable on its own until it can be wired in.

    Exit code 1 means a rebuild would destroy something, so a script can use it
    directly: `ranger vault-guard || exit 1`.
    """
    from .marker import notes_with_context, rebuild_refusal

    paint = _colour(sys.stdout.isatty())
    folder = config.vault.accounts
    if not folder.is_dir():
        print(f"  no accounts folder at {folder}", file=sys.stderr)
        return 1

    refusal = rebuild_refusal(folder)
    if not refusal:
        total = len(list(folder.rglob("*.md")))
        print(paint(f"  ok  no account note holds Ranger context ({total} notes)", TEAL))
        print(paint("      a rebuild would destroy nothing", DIM))
        return 0

    print(paint(refusal, YELLOW))
    print()
    print(paint(f"  {len(notes_with_context(folder))} notes, in {folder}", DIM))
    return 1


def cmd_accounts_survey(config: Config, args: Any) -> int:
    """What is actually in the vault, so nobody has to guess at the vocabulary.

    Tier values and opportunity stages are the operator's, set by their CRM
    export. Ranking accounts by either one means knowing what the values are
    and what order they go in, and asking for that in conversation produced a
    template pasted back with the placeholders still in it. This reads them.
    """
    from collections import Counter

    from .accounts import scan_all

    paint = _colour(sys.stdout.isatty())
    vault = Vault(config.vault)
    scans, errors = scan_all(vault, config.vault.accounts, config.accounts.exclude_files)

    if not scans:
        print(paint(f"  no account notes under {config.vault.accounts}", DIM))
        return 1

    print(paint(f"{len(scans)} account notes", BOLD))
    print()

    tiers = Counter((scan.tier.strip() or "(no Tier line)") for scan in scans)
    print(paint("Tier", BOLD))
    ordered = config.accounts.tier_order
    for value, count in sorted(tiers.items(), key=lambda pair: (-pair[1], pair[0])):
        known = value in ordered
        rank = f"rank {ordered.index(value) + 1}" if known else "unranked, sorts last"
        print(f"  {count:>3}  {value:<28} {paint(rank, DIM if known else YELLOW)}")
    if not ordered:
        print(paint("  accounts.tier_order is empty, so tier does not affect ranking yet.", YELLOW))
        print(paint("  Put these values in it, best first.", DIM))
    print()

    with_opps = sum(1 for scan in scans if scan.opportunity_stages)
    print(paint(f"{with_opps} accounts carry opportunities", DIM))
    print()

    _survey_opportunity_shape(config, vault, scans, paint)

    counted = sum(1 for scan in scans if not scan.unconfirmed)
    print(paint(f"{counted} notes count toward the morning brief, "
                f"{len(scans) - counted} are UNCONFIRMED and set aside", DIM))
    for error in errors:
        print(paint(f"  unreadable: {error}", YELLOW), file=sys.stderr)
    return 0


#: A field with more distinct values than this is content, not a category, so
#: the survey counts it and does not print it. Amounts, dates and free text all
#: fall on that side; a stage or a status does not.
CATEGORY_LIMIT = 12

#: Even a low cardinality field is not printed if it looks like money or a long
#: number. In the operator's real export quote amounts vary and would be
#: suppressed by cardinality alone, but a column where every deal happens to
#: carry the same figure would not be, and pricing is the one thing they have
#: said twice must never leave the vault.
_LOOKS_LIKE_MONEY = re.compile(
    r"[$£€]|\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d{2}\b|\b\d{4,}\b"
)


def _survey_opportunity_shape(config: Config, vault: Vault, scans, paint) -> None:
    """What the opportunity meta line actually contains.

    build_vault.py writes each opportunity as a heading and then one
    pipe-delimited line:

        ### Wexxar Case Sealers (5 Units)
        Proposal | 40000 | Q1 2026 | 60 probability

    Any of the four may be absent, so nothing past the first element can be
    read by position. Only the stage is ever printed. The second element is a
    dollar amount and the operator has said twice that pricing does not leave
    the vault, so the other segments are counted and never shown, which is a
    guarantee of shape rather than a pattern that might not match.
    """
    from collections import Counter

    from .accounts import opportunity_shapes, stage_from_meta

    stages: Counter = Counter()
    labels: Counter = Counter()
    values: dict[str, Counter] = {}
    widths: Counter = Counter()
    total = 0
    stageless = 0

    for scan in scans:
        try:
            text = vault.read_text(scan.path)
        except Exception:
            continue
        for meta, fields in opportunity_shapes(text):
            total += 1
            if meta:
                widths[len(meta)] += 1
                stage = stage_from_meta(" | ".join(meta))
                if stage:
                    stages[stage] += 1
                else:
                    stageless += 1
            else:
                stageless += 1
            for label, value in fields:
                labels[label] += 1
                values.setdefault(label, Counter())[value or "(empty)"] += 1

    if not total:
        return

    print(paint("Opportunity meta line", BOLD))
    shape = ", ".join(f"{count} with {width} elements" for width, count in widths.most_common())
    print(paint(f"  {total} opportunities. {shape or 'none carry a meta line'}", DIM))
    print(paint("  stage, value, expected close, probability. Only the stage is shown:", DIM))
    print(paint("  the second element is a dollar amount.", DIM))

    if stages:
        closed = {value.strip().casefold() for value in config.accounts.closed_stages}
        for value, count in sorted(stages.items(), key=lambda pair: (-pair[1], pair[0])):
            if _LOOKS_LIKE_MONEY.search(value):
                mark = paint("looks like an amount, not printed", DIM)
                value = "(withheld)"
            elif value.strip().casefold() in closed:
                mark = paint("counts as finished", DIM)
            else:
                mark = paint("counts as live", TEAL)
            print(f"  {count:>3}  {value:<28} {mark}")
    if stageless:
        print(paint(f"  {stageless} with no stage in first position", YELLOW))
    if not config.accounts.closed_stages and stages:
        print(paint("  accounts.closed_stages is empty, so every opportunity counts as live.", YELLOW))
        print(paint("  Put the finished stages in it and the rest still count.", YELLOW))

    if labels:
        print(paint(f"  labelled fields as well:", DIM))
        for label, count in labels.most_common():
            counts = values[label]
            if any(_LOOKS_LIKE_MONEY.search(value) for value in counts):
                print(paint(f"  {count:>3}  {label:<20} looks like an amount, not printed", DIM))
            elif len(counts) <= CATEGORY_LIMIT:
                shown = ", ".join(f"{v} ({n})" for v, n in counts.most_common())
                print(f"  {count:>3}  {label:<20} {shown}")
            else:
                print(paint(f"  {count:>3}  {label:<20} {len(counts)} distinct, not a category", DIM))
    print()


def cmd_ui(config: Config, args: Any) -> int:
    """Serve the front end. No agent logic passes through here."""
    from dataclasses import replace

    from .server import serve

    if args.port:
        config = replace(config, server=replace(config.server, port=args.port))
    with _logging_to(getattr(args, "log", None)):
        return serve(config, open_browser=args.open, verbose=args.verbose)


def ui_log(config: Config) -> Path:
    """Where a windowless server's output goes. Named in every reply, because
    a shortcut that opens nothing has to leave something to read."""
    return config.vault.log / "ui.log"


def cmd_open(config: Config, args: Any) -> int:
    """What the taskbar shortcut runs. One server, one window.

    Three cases, in order. A server is already up and somebody is looking at
    it: bring that window forward. A server is up and nobody is: open a window
    at it. Nothing is up: start one, wait for it to answer, then open a window.
    """
    log = ui_log(config)
    with _logging_to(str(log)):
        return _open(config, args, log)


def _open(config: Config, args: Any, log: Path) -> int:
    from .desktop import focus_window, open_window, start_server
    from .server import describe, probe

    paint = _colour(sys.stdout.isatty())

    running = probe(config)
    url = describe(config, (running or {}).get("port"))
    if running is None:
        if config.source_path is None:
            print(paint("  the loaded config has no path, so a server cannot be started", RED),
                  file=sys.stderr)
            return 1
        print(paint(f"  starting Jarvis, logging to {log}", DIM))
        start_server(Path(config.source_path).resolve(), log)

        import time

        deadline = time.monotonic() + float(getattr(args, "wait", 20) or 20)
        while time.monotonic() < deadline:
            running = probe(config, timeout=0.4)
            if running is not None:
                break
            time.sleep(0.25)

        url = describe(config, (running or {}).get("port"))
        if running is None:
            print(paint(f"  Jarvis did not come up at {url}.", RED), file=sys.stderr)
            print(paint(f"  The log says why: {log}", RED), file=sys.stderr)
            return 1

    elif running.get("sessions", 0) > 0 and not getattr(args, "new_window", False):
        result = focus_window(topmost=config.wake.surface_topmost)
        if result.surfaced:
            # A flash is not focus, and saying so is the point: on this machine
            # the refusal is probably the common path and nobody knows until
            # it is reported honestly.
            print(paint(f"  {result.describe()}", DIM))
            return 0
        # Not an error, and not worth mentioning twice: a second window is a
        # much smaller problem than a shortcut that refuses to do anything.
        print(paint(f"  could not focus the existing window ({result.detail})", DIM))

    how = open_window(url, profile_dir=_browser_profile(config))
    print(paint(f"Jarvis  {url}", BOLD) + paint(f"  {how}", DIM))
    print(paint(f"  log  {log}", DIM))
    return 0


def _browser_profile(config: Config) -> Path:
    """Its own browser profile, so Jarvis's window is not a tab in the
    operator's work browser and closing that browser does not close this."""
    return config.vault.ranger / "browser"


def cmd_stop(config: Config, args: Any) -> int:
    """Stop a server started by the shortcut, which has no window to close."""
    import os
    import signal

    from .server import probe, read_lock

    paint = _colour(sys.stdout.isatty())
    running = probe(config)
    if running is None:
        print(paint("  nothing is running.", DIM))
        return 0

    pid = running.get("pid") or (read_lock(config) or {}).get("pid")
    if not pid:
        print(paint("  it is running but did not say which process it is.", YELLOW))
        return 1
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (OSError, ValueError) as exc:
        print(paint(f"  could not stop process {pid}: {exc}", RED), file=sys.stderr)
        return 1
    print(paint(f"  stopped Jarvis (pid {pid}).", TEAL))
    return 0


#: Where Chrome puts the shortcut for a page installed as an app. Dragging that
#: file onto the taskbar is the one pinning path that still works on Windows 11.
CHROME_APPS = r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Chrome Apps"


def cmd_shortcut(config: Config, args: Any) -> int:
    """Retired. It created a .lnk that silently did nothing when double clicked.

    The target and its arguments were right; what was wrong is that pythonw has
    no stdout, so anything that failed before the server started left nothing
    anywhere to read. `ranger open` now logs before it can fail, but the
    shortcut is not worth reviving: Chrome's own "Install page as app" produces
    a better window than a .lnk ever did, with its own title bar and its own
    taskbar entry.
    """
    paint = _colour(sys.stdout.isatty())
    url = f"http://localhost:{config.server.port}/"

    print(paint("ranger shortcut is retired.", BOLD))
    print()
    print("  Install the page as an app instead, which gives a real window:")
    print(paint(f"    1. Open {url} in Chrome", DIM))
    print(paint("    2. Menu, Cast Save and Share, Install page as app", DIM))
    print()
    print("  To pin it to the taskbar on Windows 11, drag the shortcut from")
    print(paint(f"    {CHROME_APPS}", DIM))
    print("  onto the taskbar. Right-click Pin to taskbar was removed for")
    print("  anything that is not an installed application, so dragging is the")
    print("  path that still works.")
    print()
    print(paint("  'ranger schedule install --interface' starts the server at logon,", DIM))
    print(paint("  so the icon only has to open a window that is already there.", DIM))
    return 0


def _retired_shortcut(config: Config, args: Any) -> int:
    """Create the thing that gets pinned to the taskbar."""
    from .desktop import Shortcut, create_shortcut, on_windows
    from .icon import write as write_icon
    from .schedule import ScheduleError

    paint = _colour(sys.stdout.isatty())
    if config.source_path is None:
        print(paint("  the loaded config has no path", RED), file=sys.stderr)
        return 1

    settings = Path(config.source_path).resolve()
    root = settings.parent
    icon = write_icon(root / "ranger.ico")

    launcher = Path(sys.executable)
    windowless = launcher.with_name("pythonw.exe")
    if windowless.exists():
        launcher = windowless
    elif on_windows():
        print(paint(f"  no pythonw beside {launcher}, so a console window will appear", YELLOW))

    target = Path(args.path).expanduser() if args.path else _desktop_dir() / "Jarvis.lnk"
    shortcut = Shortcut(
        path=target,
        target=launcher,
        arguments=f'-m ranger -c "{settings}" open',
        working_directory=root,
        icon=icon,
        description="Jarvis",
    )

    if not on_windows():
        print(paint("  shortcuts are Windows only. This is what would be created:", DIM))
        print(f"  path       {shortcut.path}")
        print(f"  target     {shortcut.target}")
        print(f"  arguments  {shortcut.arguments}")
        print(f"  icon       {shortcut.icon}")
        return 0

    try:
        create_shortcut(shortcut)
    except OSError as exc:
        print(paint(f"  {exc}", RED), file=sys.stderr)
        return 1

    print(paint(f"Created {shortcut.path}", BOLD))
    print(paint(f"  icon  {icon}", DIM))
    print()
    print("  Right-click it and choose Pin to taskbar.")
    print(paint(f"  Clicking it starts Jarvis if it is not running and opens {config.server.host}"
                f":{config.server.port} in its own window.", DIM))
    print(paint(f"  Output goes to {ui_log(config)}. 'ranger stop' stops it.", DIM))
    return 0


def _desktop_dir() -> Path:
    """The Desktop, wherever OneDrive has moved it to."""
    candidates = [Path.home() / "Desktop"]
    profile = os.environ.get("USERPROFILE")
    if profile:
        candidates.append(Path(profile) / "Desktop")
        candidates.append(Path(profile) / "OneDrive" / "Desktop")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return Path.home()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ranger", description="Jarvis, a voice-first assistant.")
    parser.add_argument("-c", "--config", help="path to ranger.toml")
    parser.add_argument(
        "--quiet-state",
        action="store_true",
        help="hide the state stream the core emits",
    )
    parser.add_argument(
        "--voice",
        action="store_true",
        help="Tier 3d: push to talk instead of typing. The typed path stays available.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("chat", help="talk to Jarvis in the terminal (default)")
    sub.add_parser("doctor", help="check the config, the vault and the environment")
    init = sub.add_parser("init", help="create Ranger's own folders in the vault")
    init.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")

    audio = sub.add_parser("audio", help="Tier 3a: check the microphone and speaker")
    audio_sub = audio.add_subparsers(dest="audio_command")
    audio_sub.add_parser("devices", help="list the audio devices PortAudio can see")
    check = audio_sub.add_parser("check", help="record, measure, save and play back")
    check.add_argument(
        "-s", "--seconds", type=float, default=3.0, help="how long to record (default 3)"
    )
    check.add_argument(
        "--hold",
        action="store_true",
        help="use the push-to-talk key instead of a fixed duration",
    )
    check.add_argument("--keep", help="write the recording to this path")

    transcribe = sub.add_parser(
        "transcribe", help="Tier 3b: transcribe a WAV file with Deepgram"
    )
    transcribe.add_argument("file", help="path to a 16 bit WAV, e.g. from 'ranger audio check --keep'")
    transcribe.add_argument(
        "--expect", help="what you actually said, to see exactly which words came back wrong"
    )
    transcribe.add_argument(
        "--no-hints", action="store_true", help="send no vocabulary hints, to A/B whether they help"
    )
    transcribe.add_argument("--model", help="override stt.model for this run")

    sub.add_parser("memory", help="Tier 4: show what Jarvis remembers")

    inbox = sub.add_parser("inbox", help="Tier 5: notices waiting for you")
    inbox.add_argument("dismiss", nargs="?", type=int, help="clear notice N in the vault")
    inbox.add_argument("--all", action="store_true", help="include dismissed notices")
    inbox.add_argument("--full", action="store_true", help="print each notice in full")

    sub.add_parser("pause", help="Tier 6: the kill switch. Stop all proactive behaviour")
    sub.add_parser("resume", help="Tier 6: start proactive behaviour again")
    log_cmd = sub.add_parser("log", help="Tier 6: the audit trail")
    log_cmd.add_argument("day", nargs="?", help="a date, YYYY-MM-DD. Defaults to today")

    beat = sub.add_parser("heartbeat", help="Tier 5: run the background loop")
    beat.add_argument("--once", action="store_true", help="one pass, then exit")
    beat.add_argument(
        "--force",
        nargs="+",
        metavar="CHECK",
        help="run these now whatever the schedule and quiet hours say, or 'all'",
    )
    beat.add_argument(
        "--log",
        metavar="PATH",
        help="append everything printed to this file as well. A scheduled task has no terminal",
    )
    keyterms = sub.add_parser("keyterms", help="show the vocabulary hints that would be sent")
    keyterms.add_argument("--all", action="store_true", help="show every hint, not the top 25")

    ui = sub.add_parser("ui", help="Tier 7: serve the browser front end")
    ui.add_argument("--open", action="store_true", help="open the browser as well")
    ui.add_argument("--verbose", action="store_true", help="log every request")
    ui.add_argument("--port", type=int, help="override [server] port for this run")
    ui.add_argument("--log", metavar="PATH", help="append everything printed to this file as well")

    opened = sub.add_parser("open", help="open Jarvis in its own window, starting it if needed")
    opened.add_argument("--new-window", action="store_true", help="always open another window")
    opened.add_argument("--wait", type=float, default=20, help="seconds to wait for it to come up")

    sub.add_parser("stop", help="stop a Jarvis server started by the shortcut")

    shortcut = sub.add_parser("shortcut", help="retired: how to install and pin the app instead")
    shortcut.add_argument("path", nargs="?", help=argparse.SUPPRESS)

    schedule = sub.add_parser(
        "schedule", help="run the heartbeat on a schedule, with no terminal open"
    )
    # Accepted before or after the subcommand, under two dests, because
    # argparse lets a subparser's default overwrite the parent's value and
    # 'ranger schedule --interface' silently meaning the heartbeat would be a
    # nasty way to remove the wrong task.
    schedule.add_argument(
        "--interface", dest="interface_first", action="store_true",
        help="the 'ranger ui' task that starts at logon, not the heartbeat",
    )
    schedule_sub = schedule.add_subparsers(dest="schedule_command")
    for name, help_text in (
        ("show", "what is registered, and what Task Scheduler thinks of it"),
        ("install", "register it, replacing any existing one"),
        ("remove", "unregister it"),
        ("xml", "print the task definition without registering anything"),
    ):
        step = schedule_sub.add_parser(name, help=help_text)
        step.add_argument(
            "--interface", action="store_true",
            help="the 'ranger ui' task that starts at logon, not the heartbeat",
        )
        if name in {"install", "xml", "show"}:
            step.add_argument(
                "--every", type=int, metavar="MINUTES",
                help="how often to check. Jarvis decides what is due (default 60)",
            )
            step.add_argument("--log", metavar="PATH", help="where the output goes")
            step.add_argument(
                "--windowless", action="store_true",
                help="run through pythonw so no console window appears",
            )

    alias = sub.add_parser("alias", help="accounts the CRM exports under two names")
    alias_sub = alias.add_subparsers(dest="alias_command")
    alias_sub.add_parser("show", help="what is folded together, and anything stale")
    alias_sub.add_parser("suggest", help="pairs that look like one account. Never merges")
    add = alias_sub.add_parser("add", help="fold one name into another")
    add.add_argument("variant", nargs="+", help="the name to fold in")
    add.add_argument("canonical", nargs="+", help="the name to keep")
    drop = alias_sub.add_parser("remove", help="stop folding a name in")
    drop.add_argument("variant", nargs="+", help="the name to separate again")

    wake = sub.add_parser(
        "wake", help="the hands free hotword models: what is installed, and installing it"
    )
    wake_sub = wake.add_subparsers(dest="wake_command")
    wake_sub.add_parser("show", help="what is installed and where it came from")
    wake_install = wake_sub.add_parser(
        "install", help="download the hotword models from the openWakeWord project"
    )
    wake_install.add_argument(
        "--model", help="which published hotword. Defaults to wake.model in config"
    )

    sub.add_parser(
        "mic", help="what the microphone check sees, and whether hands free could arm"
    )

    dormant = sub.add_parser(
        "dormant", help="stop surfacing an account in the morning brief, or list those set aside"
    )
    dormant.add_argument("account", nargs="*", help="the account name. Omit to list")
    dormant.add_argument("--remove", action="store_true", help="bring it back instead")

    accounts = sub.add_parser("accounts", help="what is in the account notes")
    accounts_sub = accounts.add_subparsers(dest="accounts_command")
    migrate_cmd = accounts_sub.add_parser(
        "migrate", help="put the ranger:below marker in every account note, once"
    )
    migrate_cmd.add_argument(
        "--dry-run", action="store_true", help="say what would change and write nothing"
    )
    drafts = sub.add_parser("drafts", help="the drafts Jarvis is holding")
    drafts_sub = drafts.add_subparsers(dest="drafts_command")
    drafts_sub.add_parser("show", help="what is held right now")
    drafts_sub.add_parser("cleared", help="what has been cleared. Moved, never deleted")
    clear_cmd = drafts_sub.add_parser(
        "clear", help="move a draft out of the panel. It is not deleted"
    )
    clear_cmd.add_argument("name", nargs="+", help="file name, or part of the title")

    gp = sub.add_parser(
        "gp", help="gross profit: figures you enter, totals Python computes"
    )
    gp_sub = gp.add_subparsers(dest="gp_command")
    gp_sub.add_parser("show", help="year to date and month to date, with an as-of date")
    gp_add = gp_sub.add_parser("add", help="record a figure. Never edits an existing one")
    gp_add.add_argument("amount", nargs="+", help="the figure, e.g. 48250 or $48,250.00")
    gp_add.add_argument(
        "--period", default="", help="the month it is for, as 2026-09. Defaults to this month"
    )
    gp_add.add_argument("--note", default="", help="anything worth remembering about it")
    gp_history = gp_sub.add_parser(
        "history", help="every entry ever made, superseded ones included"
    )
    gp_history.add_argument("--year", default="", help="only this year, e.g. 2026")

    snapshot = sub.add_parser(
        "snapshot", help="the vault's local history: the undo for account writes"
    )
    snapshot_sub = snapshot.add_subparsers(dest="snapshot_command")
    snapshot_sub.add_parser("show", help="whether the vault has a repository, and a remote")
    snapshot_sub.add_parser("init", help="make the vault a local git repository. No remote")
    snapshot_sub.add_parser("now", help="take today's snapshot by hand")

    sub.add_parser(
        "vault-guard",
        help="what a rebuild of the vault would destroy. Exit 1 if anything would",
    )

    accounts_sub.add_parser(
        "survey", help="the Tier values and opportunity stages actually in the vault"
    )

    sub.add_parser("voices", help="Tier 3c: list the ElevenLabs voices on the account")
    say = sub.add_parser("say", help="Tier 3c: speak a line aloud")
    say.add_argument("text", nargs="+", help="what to say")
    say.add_argument("--voice", help="override tts.voice_id for this run")
    say.add_argument("--keep", help="save the spoken audio to this WAV path")
    say.add_argument("--no-play", action="store_true", help="fetch but do not play")

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
    if args.command == "audio":
        if args.audio_command == "devices":
            return cmd_audio_devices(config)
        if args.audio_command == "check":
            return cmd_audio_check(config, args)
        print("usage: ranger audio devices | ranger audio check", file=sys.stderr)
        return 2
    if args.command == "transcribe":
        return cmd_transcribe(config, args)
    if args.command == "memory":
        return cmd_memory(config, args)
    if args.command == "inbox":
        return cmd_inbox(config, args)
    if args.command == "heartbeat":
        return cmd_heartbeat(config, args)
    if args.command == "pause":
        return cmd_pause(config, args)
    if args.command == "resume":
        return cmd_resume(config, args)
    if args.command == "log":
        return cmd_log(config, args)
    if args.command == "keyterms":
        return cmd_keyterms(config, args)
    if args.command == "schedule":
        return cmd_schedule(config, args)
    if args.command == "alias":
        return cmd_alias(config, args)
    if args.command == "wake":
        return cmd_wake(config, args)
    if args.command == "mic":
        return cmd_mic(config, args)
    if args.command == "dormant":
        return cmd_dormant(config, args)
    if args.command == "drafts":
        return cmd_drafts(config, args)
    if args.command == "gp":
        return cmd_gp(config, args)
    if args.command == "snapshot":
        return cmd_snapshot(config, args)
    if args.command == "vault-guard":
        return cmd_vault_guard(config, args)
    if args.command == "accounts":
        if args.accounts_command == "survey":
            return cmd_accounts_survey(config, args)
        if args.accounts_command == "migrate":
            return cmd_accounts_migrate(config, args)
        print("usage: ranger accounts survey|migrate", file=sys.stderr)
        return 2
    if args.command == "open":
        return cmd_open(config, args)
    if args.command == "stop":
        return cmd_stop(config, args)
    if args.command == "shortcut":
        return cmd_shortcut(config, args)
    if args.command == "ui":
        return cmd_ui(config, args)
    if args.command == "voices":
        return cmd_voices(config, args)
    if args.command == "say":
        return cmd_say(config, args)

    try:
        if args.voice:
            return asyncio.run(voice_loop(config, show_state=not args.quiet_state))
        return asyncio.run(repl(config, show_state=not args.quiet_state))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
