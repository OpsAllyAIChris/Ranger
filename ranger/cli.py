"""The terminal. The first caller of the core, and permanently the debug path.

There is no agent logic in this file. It reads a line, hands it to
Ranger.turn(), and renders the events that come back. When the browser front
end arrives it does the same thing with a websocket instead of a terminal.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from .audio import AudioError, resolve_device
from .audiocheck import format_devices, run_check
from .compare import compare
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
            f"  knowledge budget {config.knowledge.budget_chars} chars",
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


async def voice_loop(config: Config, show_state: bool) -> int:
    """Tier 3d. The same core the typed REPL uses, with ears and a mouth."""
    from .audio import AudioError, SoundDeviceBackend
    from .stt import build_transcriber
    from .trigger import TriggerError, build_trigger
    from .tts import build_speaker
    from .voiceloop import VoiceLoop

    paint = _colour(sys.stdout.isatty())
    try:
        agent = _build_agent(config)
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

    print(paint("Ranger", BOLD + TEAL) + paint(f"  {config.model.name}, voice {config.tts.voice_id}", DIM))
    print(paint(f"  {len(plan.terms)} vocabulary hints, {config.stt.model}", DIM))
    print(paint("  the typed interface is still there: run 'ranger' with no flags", DIM))
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

    if config.tts.voice_id:
        print("  ok       tts.voice_id is set")
    else:
        print("  todo     tts.voice_id is not set. Run 'ranger voices', then put the id in")
        print(f"           {_local_config_path(config)}, which is git-ignored and survives a pull.")

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


def cmd_audio_devices(config: Config) -> int:
    """Tier 3a. What PortAudio can see, and which ones Ranger will use."""
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
        print("  Ranger writes here when you tell it something worth keeping, and you")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ranger", description="Ranger, a voice-first assistant.")
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
    sub.add_parser("chat", help="talk to Ranger in the terminal (default)")
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

    sub.add_parser("memory", help="Tier 4: show what Ranger remembers")
    keyterms = sub.add_parser("keyterms", help="show the vocabulary hints that would be sent")
    keyterms.add_argument("--all", action="store_true", help="show every hint, not the top 25")

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
    if args.command == "keyterms":
        return cmd_keyterms(config, args)
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
