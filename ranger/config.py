"""Configuration. One file, loaded once, validated at startup.

Nothing in the source hardcodes a model name, a vault path, an hour, or a
threshold. If a value could reasonably change, it lives in ranger.toml.
Secrets never appear here; they come from the environment via .env.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

DEFAULT_CONFIG_FILENAME = "ranger.toml"

#: Settings that are the operator's rather than the project's live here, beside
#: the tracked config and git-ignored. ranger.toml carries the defaults and gets
#: edited by whoever is working on Jarvis; this file carries the voice id, the
#: microphone, and anything else that should survive a pull.
LOCAL_SUFFIX = ".local.toml"


class ConfigError(Exception):
    """Raised at startup when the config cannot be trusted."""


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    name: str
    max_tokens: int
    #: How hard the model thinks. Replaces temperature, which current models
    #: reject outright. Empty string means do not send it at all, for older
    #: models that do not accept it.
    effort: str
    max_tool_rounds: int
    history_turns: int
    timeout_seconds: float = 60.0
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0
    #: Cache the stable half of the system prompt. The whole prompt is resent
    #: every turn, so this is what stops the knowledge budget costing money on
    #: each one.
    cache_prompt: bool = True
    #: Dollars per million tokens, for the running tally. Set to 0 to show
    #: token counts and no money. These are not looked up anywhere: the numbers
    #: change, and a stale guess in code would be worse than nothing.
    price_input: float = 0.0
    price_output: float = 0.0
    price_cache_write: float = 0.0
    price_cache_read: float = 0.0


@dataclass(frozen=True)
class VaultConfig:
    """Absolute, resolved paths. Amendment D is enforced in vault.py."""

    root: Path
    accounts: Path
    knowledge: Path
    ranger: Path
    memory: Path
    inbox: Path
    drafts: Path
    log: Path
    #: Take a daily git snapshot of the vault. Read-only Accounts/ used to be
    #: the undo; once Jarvis appends there, this is. Local only, never pushed.
    snapshot: bool = True
    #: Refuse to snapshot any single file larger than this. A ceiling catches
    #: the thing nobody thought to name, which is the failure that actually
    #: happened: the first snapshot took a live Chrome profile.
    snapshot_max_file_mb: float = 5.0
    #: Refuse a first snapshot larger than this in total, naming the largest
    #: files. A backup that quietly swallows a gigabyte is a surprise.
    snapshot_max_total_mb: float = 200.0

    @property
    def snapshot_max_file_bytes(self) -> int:
        return int(self.snapshot_max_file_mb * 1_048_576)

    @property
    def snapshot_max_total_bytes(self) -> int:
        return int(self.snapshot_max_total_mb * 1_048_576)

    @property
    def writable_roots(self) -> tuple[Path, ...]:
        """Amendment D: Ranger writes under Ranger/ and nowhere else.

        The whole tree, not only the four named folders. Listing just those
        four was a tightening beyond what Amendment D asks, and it refused the
        kill switch, which belongs at the root of Ranger's own folder where it
        is obvious rather than buried in the inbox.
        """
        return (self.ranger,)

    @property
    def named_roots(self) -> tuple[Path, ...]:
        return (self.memory, self.inbox, self.drafts, self.log)

    @property
    def append_only_roots(self) -> tuple[Path, ...]:
        return (self.log,)


@dataclass(frozen=True)
class KnowledgeConfig:
    #: Only a fallback for callers that do not pass a budget. The real ceiling
    #: is context.budget_chars minus what memory took.
    budget_chars: int
    priority: tuple[str, ...]


@dataclass(frozen=True)
class ScheduleConfig:
    morning_hour: int
    quiet_start_hour: int
    quiet_end_hour: int

    def in_quiet_hours(self, hour: int) -> bool:
        """Quiet windows wrap midnight, so 18 to 6 is a single window."""
        start, end = self.quiet_start_hour, self.quiet_end_hour
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end


@dataclass(frozen=True)
class AccountsConfig:
    quiet_after_days: int
    exclude_files: tuple[str, ...] = ()
    skip_statuses: tuple[str, ...] = ()
    #: The operator's Tier vocabulary, best first. Empty means tier does not
    #: affect ranking. Nothing in the code knows what a tier is called;
    #: `ranger accounts survey` reports what is actually in the vault.
    tier_order: tuple[str, ...] = ()
    #: Which opportunity stages mean the deal is over. Everything else is
    #: live, including a stage nobody has seen before and an opportunity with
    #: no stage at all.
    #:
    #: Deliberately the closed list rather than the open one. With an open
    #: list, a stage added to the CRM later would count as closed and the deal
    #: would vanish from the brief silently. With a closed list it shows up
    #: wrongly instead, which the operator can see and correct.
    closed_stages: tuple[str, ...] = ()

    def tier_rank(self, tier: str) -> int:
        """Lower is better. Anything unrecognised sorts last, never first."""
        value = tier.strip()
        for index, known in enumerate(self.tier_order):
            if value.casefold() == known.strip().casefold():
                return index
        return len(self.tier_order) + 1

    def is_open(self, stage: str) -> bool:
        """Live unless it is explicitly one of the finished stages."""
        return stage.strip().casefold() not in {
            value.strip().casefold() for value in self.closed_stages
        }


@dataclass(frozen=True)
class WakeConfig:
    """Hands free. Off unless the operator turns it on, every session.

    `enabled` is what the *setting file* says, and it is deliberately not the
    same thing as being armed: arming is a per session act in the interface and
    is never persisted. This only decides whether the control appears at all.
    """

    enabled: bool = False
    #: The phrase, two words, used to strip it off the front of a transcript
    #: and to say what is being listened for.
    phrase: str = "hey jarvis"
    #: A .onnx or .tflite next to the vault, or a name openWakeWord ships.
    model: str = "hey_jarvis"
    #: Higher is fewer false fires and more missed ones.
    threshold: float = 0.6
    #: Seconds to wait for speech after the phrase before discarding.
    grace_seconds: float = 3.0
    #: Silence that ends an utterance.
    silence_seconds: float = 0.9
    #: Ceiling, for a room noisy enough that silence never arrives.
    max_seconds: float = 30.0
    #: Kept before the fire, because detection lags the phrase.
    preroll_seconds: float = 1.5
    #: An open microphone nobody remembers is the likeliest failure.
    idle_disarm_minutes: float = 15.0
    #: How often to ask whether another application took the microphone.
    mic_check_seconds: float = 5.0
    #: Conversation mode. After a reply, keep listening for a follow-up without
    #: the phrase. Anchored to the end of playback, not the end of the turn.
    #:
    #: Time to *start* speaking. Once speech starts the ordinary utterance
    #: rules take over, so this is not a ceiling on the follow-up itself.
    conversation_seconds: float = 8.0
    #: Windows per firing of the wake word. The budget refills on the phrase and
    #: on nothing else: resetting it after a quiet period would mean a room with
    #: a fan refills it forever. Three is a first week guess and the logged
    #: close reasons are what will replace it.
    conversation_reopens: int = 3
    #: Refuse to open the window when the interface is not on screen. An open
    #: microphone whose only indication is on a window nobody can see is the
    #: thing the design exists to avoid. Turn this off once the window can
    #: bring itself to the front.
    conversation_requires_visible: bool = True
    #: Bring the window forward when the phrase fires.
    surface_on_wake: bool = True
    #: Force it in front with HWND_TOPMOST when Windows refuses foreground.
    #: Off by default: it works, and it puts Jarvis over a screen share.
    surface_topmost: bool = False
    #: Never force the window in front while another application has the
    #: microphone. The closest thing to "am I in a call" that exists without
    #: asking Teams, and what makes surface_topmost safe enough to trial.
    surface_topmost_never_in_call: bool = True

    @property
    def idle_disarm_seconds(self) -> float:
        return self.idle_disarm_minutes * 60


@dataclass(frozen=True)
class BriefConfig:
    """Tier 5. What the morning brief is allowed to say.

    A brief has a size, not a threshold. `accounts.quiet_after_days` decides
    what is eligible; these decide what the operator actually reads. With 58
    accounts a boolean threshold produced 41 lines sorted by how thoroughly
    each one had been abandoned, which is the list least likely to be acted on.
    """

    #: Hard cap. Everything below is a share of this.
    lines: int = 5
    #: Accounts that crossed the threshold since the last brief. The only
    #: genuinely new information in the report.
    slipping_max: int = 3
    #: Past the threshold with a live opportunity. A deal going quiet is a
    #: different emergency from a relationship going quiet.
    deals_max: int = 3
    #: Past this, an account is not lapsing, it has lapsed. Cold accounts never
    #: appear in the daily buckets; they come back one at a time as a decision.
    cold_after_days: int = 90
    #: Whether to close the brief with one cold account and ask if it should
    #: keep appearing. This is what drains the backlog instead of reprinting it.
    decision_prompt: bool = True
    #: When each account was first reported quiet, so the brief can report a
    #: crossing rather than a state.
    seen_file: str = "quiet-seen.md"
    #: Accounts the operator has said to stop surfacing. In Jarvis's folder
    #: rather than the note, because build_vault.py overwrites the notes.
    dormant_file: str = "dormant.md"


@dataclass(frozen=True)
class RecallConfig:
    """Account notes are large. These keep a digest bounded."""

    activities: int = 5
    detail_activities: int = 20
    section_chars: int = 400
    body_chars: int = 300
    max_chars: int = 4000


@dataclass(frozen=True)
class DraftsConfig:
    filename_format: str


@dataclass(frozen=True)
class DocumentsConfig:
    """Generated documents, and the preview that renders them back.

    Nothing here changes what a document contains. These are about the machine
    the window is running on: how much of a long document to draw, and how much
    of a flourish to spend on a screen that is often being shared.
    """

    #: A4 or letter. Wrong page size is the kind of thing nobody notices until
    #: a customer prints it.
    page_size: str = "A4"
    #: Draw the assembly animation when a document lands. Off is a supported
    #: answer: the preview is the point and the particles are not.
    assembly: bool = True
    #: How many particles converge. They are drawn in the existing scene, so
    #: this is the number to bring down first if the window costs frames while
    #: a call is being screen shared.
    assembly_particles: int = 220
    #: How long the flourish lasts. It never gates the preview, which opens at
    #: the same moment, so this is only how long the particles are in flight.
    assembly_seconds: float = 1.1
    #: How many paragraphs, tables and rows the preview draws before it says
    #: how much more there is. A forty page document is real, and so is a
    #: browser that has to stay responsive during a customer call.
    preview_blocks: int = 400
    preview_rows: int = 300


@dataclass(frozen=True)
class ImportsConfig:
    """Dropped files. What is allowed in, and how much of it is read.

    Nothing here decides what a file means. These are the two questions a
    machine has: how big a file may be, and how much of one is worth turning
    into context.
    """

    #: The ceiling on a dropped file, in megabytes. A NetSuite export is under
    #: a megabyte; this is here so a stray 400MB video is refused with a
    #: sentence rather than written into the vault and then into the snapshot.
    max_mb: float = 25.0
    #: How many rows of each sheet the sidecar quotes. Every sheet is always
    #: *named* with its row count, however many there are: a fourteen sheet
    #: workbook where four sheets are listed is a file the operator will be
    #: wrong about.
    extract_rows: int = 60
    #: How many sheets have their contents quoted, as opposed to being named.
    extract_sheets: int = 40
    #: Extract a dropped file the moment it lands. Off, and deliberately: a
    #: file dropped by accident should cost nothing, and the extract is written
    #: the first time something is actually asked about the file.
    extract_on_drop: bool = False

    @property
    def max_bytes(self) -> int:
        return int(self.max_mb * 1_048_576)


@dataclass(frozen=True)
class GpConfig:
    """The gross profit tracker. Entered by hand, computed in Python.

    Nothing here is a figure. These are the three things about the operator's
    year that the code cannot work out for itself, and each one, guessed
    wrongly, puts a real number quietly in the wrong place -- which is worse
    than showing nothing, because a wrong number that looks right gets acted
    on.
    """

    #: What the figures are in. Only ever a display symbol: no conversion
    #: happens anywhere, because a dashlet that converted currencies would be
    #: computing a number the operator never entered.
    currency: str = "$"
    #: The month a financial year starts in. 1 is the calendar year. A fiscal
    #: year starting in April is ordinary, and guessing January would put
    #: April's figure in the previous year's total with no sign of it.
    year_starts_month: int = 1
    #: After this many days with no new entry, the panel says the figure is
    #: stale. A stale figure that looks current is this dashlet's failure mode.
    stale_after_days: int = 45


@dataclass(frozen=True)
class VoiceConfig:
    push_to_talk: bool
    wake_word: bool
    voice_id: str
    model_id: str = "eleven_flash_v2_5"
    trigger: str = "hold"
    key: str = "space"
    input_device: str = ""
    output_device: str = ""
    sample_rate: int = 16000
    channels: int = 1
    max_seconds: int = 60


@dataclass(frozen=True)
class MemoryConfig:
    """Tier 4. Durable facts about the operator, in plain markdown."""

    #: Memory's guaranteed slice of the standing context. Taken before
    #: knowledge, because losing a memory fact makes Jarvis forget the operator
    #: while losing a knowledge file only makes it less well briefed.
    reserve_chars: int = 8000
    file: str = "facts.md"


@dataclass(frozen=True)
class ContextConfig:
    """The whole standing allowance: memory plus knowledge.

    Account recall does not compete for this. It is a tool result living in the
    messages, capped separately by [recall].
    """

    budget_chars: int = 60000


@dataclass(frozen=True)
class SttConfig:
    """Speech to text. Deepgram, pre-recorded endpoint."""

    provider: str = "deepgram"
    model: str = "nova-3"
    language: str = "en"
    smart_format: bool = True
    punctuate: bool = True
    timeout_seconds: float = 30.0
    #: Vocabulary hints. The parameter Deepgram wants depends on the model, so
    #: the model picks it: keyterm for nova-3, keywords for nova-2 and earlier.
    keyterms: tuple[str, ...] = ()
    #: Intensifier for keyword boosting only. Ignored by keyterm prompting.
    boost: float = 2.0
    #: Deepgram's real limit is not documented here because it could not be
    #: verified; if it is lower than this the request 400s and says so.
    max_hints: int = 100
    #: Build the hint list from the account filenames rather than by hand.
    keyterms_from_accounts: bool = True


@dataclass(frozen=True)
class TtsConfig:
    """Text to speech. ElevenLabs, streaming."""

    provider: str = "elevenlabs"
    voice_id: str = ""
    #: Flash or Turbo. Latency is the whole experience with push to talk.
    model_id: str = "eleven_flash_v2_5"
    #: pcm_24000 plays with no decoder. mp3_44100_128 works on every plan but
    #: needs soundfile.
    output_format: str = "pcm_24000"
    stability: float = 0.5
    similarity_boost: float = 0.75
    speed: float = 1.0
    timeout_seconds: float = 30.0


@dataclass(frozen=True)
class GateConfig:
    """Tier 6. The hard confirmation gate."""

    #: No gate may hang the caller. A heartbeat action nobody answers resolves
    #: to held and the loop keeps running.
    timeout_seconds: float = 120.0
    #: A spoken yes is not consent: transcription is good, not perfect, and
    #: "no, don't" is one mishearing away from "yes". Voice holds instead.
    voice_holds: bool = True


@dataclass(frozen=True)
class HeartbeatConfig:
    """Tier 5. The loop that acts without being spoken to."""

    #: The kill switch until Tier 6 builds a better one.
    enabled: bool = True
    #: How often the loop wakes. Checks decide for themselves whether they are
    #: due, so this only bounds how late a catch-up can be.
    interval_seconds: int = 300
    #: Nothing waits on a person. A check that runs longer than this is stopped
    #: and leaves a note.
    check_timeout_seconds: int = 120


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int


def _merge_tables(base: dict[str, Any], override: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Overlay the local file, key by key, and report what it changed.

    Only one level deep, because the config is sections of flat keys. Setting
    tts.voice_id locally must not discard the rest of [tts].
    """
    merged = {section: dict(values) if isinstance(values, dict) else values
              for section, values in base.items()}
    changed: list[str] = []

    for section, values in override.items():
        if not isinstance(values, dict):
            merged[section] = values
            changed.append(section)
            continue
        target = merged.setdefault(section, {})
        if not isinstance(target, dict):
            merged[section] = dict(values)
            changed.extend(f"{section}.{key}" for key in values)
            continue
        for key, value in values.items():
            target[key] = value
            changed.append(f"{section}.{key}")
    return merged, changed


@dataclass(frozen=True)
class Config:
    model: ModelConfig
    vault: VaultConfig
    knowledge: KnowledgeConfig
    memory: MemoryConfig
    context: ContextConfig
    schedule: ScheduleConfig
    accounts: AccountsConfig
    brief: BriefConfig
    wake: WakeConfig
    recall: RecallConfig
    drafts: DraftsConfig
    documents: DocumentsConfig
    imports: ImportsConfig
    gp: GpConfig
    voice: VoiceConfig
    stt: SttConfig
    tts: TtsConfig
    gate: GateConfig
    heartbeat: HeartbeatConfig
    server: ServerConfig
    source_path: Path | None = None
    local_path: Path | None = None
    #: "section.key" for every value the local file overrode, so an override is
    #: visible rather than magic.
    overrides: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


# Keys under [vault] whose values are paths. A Windows user pasting one of
# these naturally writes backslashes, which TOML reads as escape sequences.
_PATH_KEYS = frozenset(
    {"root", "accounts", "knowledge", "ranger", "memory", "inbox", "drafts", "log"}
)
_TABLE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_ASSIGN = re.compile(r'^(\s*)([A-Za-z_][A-Za-z0-9_-]*)(\s*=\s*)"([^"]*)"(\s*(?:#.*)?)$')


def _repair_windows_paths(text: str) -> tuple[str, list[str]]:
    """Let a Windows path survive a TOML basic string.

    `root = "C:\\Users\\Chris\\Vault"` is not a path to TOML, it is a string with
    escape sequences in it. Two ways that bites:

      C:\\Users\\Chris   -> TOMLDecodeError, because \\U starts a unicode escape
      C:\\temp\\notes    -> parses silently into "C:<tab>emp<newline>otes"

    The second is the reason this rewrites the text before parsing rather than
    just improving the error. A value that is already correctly escaped
    (contains \\\\) is left alone, because whoever wrote it meant it.
    """
    out: list[str] = []
    notes: list[str] = []
    table = ""

    for line in text.splitlines():
        header = _TABLE.match(line)
        if header:
            table = header.group(1).strip()
            out.append(line)
            continue

        assign = _ASSIGN.match(line)
        if assign and table == "vault" and assign.group(2) in _PATH_KEYS:
            indent, key, equals, value, tail = assign.groups()
            if "\\" in value and "\\\\" not in value:
                if "'" in value:
                    # A literal string cannot hold an apostrophe, so escape instead.
                    fixed = value.replace("\\", "\\\\")
                    out.append(f'{indent}{key}{equals}"{fixed}"{tail}')
                else:
                    out.append(f"{indent}{key}{equals}'{value}'{tail}")
                notes.append(
                    f"vault.{key} is a Windows path in a double-quoted string, where "
                    "a backslash means an escape sequence. Jarvis read it as a literal "
                    f"path. To silence this, use single quotes: {key} = '{value}'"
                )
                continue

        out.append(line)

    result = "\n".join(out)
    if text.endswith("\n"):
        result += "\n"
    return result, notes


#: Every key each table is allowed to carry. A setting the code does not read
#: is worse than a missing one: it looks configured and does nothing. This is
#: how a dead voice_id sat in [voice] while the code read tts.voice_id, so
#: setting it silently had no effect.
KNOWN_KEYS: dict[str, frozenset[str]] = {
    "model": frozenset({
        "provider", "name", "max_tokens", "effort", "max_tool_rounds",
        "history_turns", "timeout_seconds", "max_retries", "retry_backoff_seconds",
        "cache_prompt", "price_input", "price_output", "price_cache_write",
        "price_cache_read",
    }),
    "vault": frozenset({
        "root", "accounts", "knowledge", "ranger", "memory", "inbox", "drafts", "log",
        "snapshot", "snapshot_max_file_mb", "snapshot_max_total_mb",
    }),
    "knowledge": frozenset({"priority"}),
    "memory": frozenset({"reserve_chars", "file"}),
    "context": frozenset({"budget_chars"}),
    "schedule": frozenset({"morning_hour", "quiet_start_hour", "quiet_end_hour"}),
    "accounts": frozenset({
        "quiet_after_days", "exclude_files", "skip_statuses", "tier_order", "closed_stages",
    }),
    "wake": frozenset({
        "enabled", "phrase", "model", "threshold", "grace_seconds", "silence_seconds",
        "max_seconds", "preroll_seconds", "idle_disarm_minutes", "mic_check_seconds",
        "conversation_seconds", "conversation_reopens", "conversation_requires_visible",
        "surface_on_wake", "surface_topmost", "surface_topmost_never_in_call",
    }),
    "brief": frozenset({
        "lines", "slipping_max", "deals_max", "cold_after_days", "decision_prompt",
        "seen_file", "dormant_file",
    }),
    "recall": frozenset({
        "activities", "detail_activities", "section_chars", "body_chars", "max_chars",
    }),
    "drafts": frozenset({"filename_format"}),
    "documents": frozenset({
        "page_size", "assembly", "assembly_particles", "assembly_seconds",
        "preview_blocks", "preview_rows",
    }),
    "imports": frozenset({
        "max_mb", "extract_rows", "extract_sheets", "extract_on_drop",
    }),
    "gp": frozenset({"currency", "year_starts_month", "stale_after_days"}),
    "voice": frozenset({
        "push_to_talk", "wake_word", "trigger", "key", "input_device", "output_device",
        "sample_rate", "channels", "max_seconds",
    }),
    "stt": frozenset({
        "provider", "model", "language", "smart_format", "punctuate", "timeout_seconds",
        "keyterms", "boost", "max_hints", "keyterms_from_accounts",
    }),
    "tts": frozenset({
        "provider", "voice_id", "model_id", "output_format", "stability",
        "similarity_boost", "speed", "timeout_seconds",
    }),
    "gate": frozenset({"timeout_seconds", "voice_holds"}),
    "heartbeat": frozenset({"enabled", "interval_seconds", "check_timeout_seconds"}),
    "server": frozenset({"host", "port"}),
}


#: Settings that used to exist. A pointer beats "not a setting Jarvis reads".
RETIRED_KEYS: dict[str, str] = {
    "accounts.open_stages": (
        "Replaced by accounts.closed_stages, which lists the stages that mean the deal is "
        "over. Everything else counts as live. Listing the open ones meant a stage added to "
        "the CRM later would silently count as closed and the deal would drop out of the "
        "morning brief with nothing to show for it."
    ),
    "knowledge.budget_chars": (
        "Knowledge now takes whatever is left of context.budget_chars after memory's "
        "reserve. There is no separate knowledge ceiling, because having both meant the "
        "smaller one won silently. Delete this line and set context.budget_chars."
    ),
}


def _check_known_keys(table: dict[str, Any]) -> None:
    """Fail on a setting nothing reads, and say where it should have gone."""
    for section, allowed in KNOWN_KEYS.items():
        values = table.get(section)
        if not isinstance(values, dict):
            continue
        for key in values:
            if key in allowed:
                continue
            retired = RETIRED_KEYS.get(f"{section}.{key}")
            if retired:
                raise ConfigError(f"{section}.{key} was removed. {retired}")
            elsewhere = [s for s, keys in KNOWN_KEYS.items() if key in keys]
            hint = (
                f" Did you mean {elsewhere[0]}.{key}?"
                if elsewhere
                else " Remove it, or check the spelling."
            )
            raise ConfigError(f"{section}.{key} is not a setting Jarvis reads.{hint}")

    for section in table:
        if section not in KNOWN_KEYS:
            raise ConfigError(
                f"[{section}] is not a section Jarvis reads. Known sections: "
                + ", ".join(sorted(KNOWN_KEYS))
            )


def _require(table: dict[str, Any], section: str, key: str) -> Any:
    if section not in table:
        raise ConfigError(f"config is missing the [{section}] section")
    if key not in table[section]:
        raise ConfigError(f"config is missing {section}.{key}")
    return table[section][key]


def _hour(value: Any, label: str) -> int:
    if not isinstance(value, int) or not 0 <= value <= 23:
        raise ConfigError(f"{label} must be a whole number from 0 to 23, got {value!r}")
    return value


def _resolve_under(root: Path, relative: str, label: str) -> Path:
    """Keep every configured subfolder inside the vault root.

    A config typo should not become a path that writes outside the vault.
    """
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ConfigError(f"vault.{label} ({relative!r}) resolves outside the vault root")
    return candidate


# A drive-letter or UNC path, which is absolute on Windows and just a relative
# filename on POSIX. Recognised so the error message can say which it is.
_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def _vault_root(raw: str) -> Path:
    """Resolve the vault root, which must be absolute.

    A relative root would resolve against the current working directory, so
    Jarvis would quietly build a vault inside whatever folder it was launched
    from. That is never what anyone means, and it is how a stray 'C:\\tmp\\...'
    once got written into this repository on Linux.
    """
    override = os.environ.get("RANGER_VAULT_ROOT")
    source = "RANGER_VAULT_ROOT" if override else "vault.root"
    value = override or raw
    root = Path(value).expanduser()

    if not root.is_absolute():
        if _WINDOWS_ABSOLUTE.match(str(root)):
            raise ConfigError(
                f"{source} is {value!r}, a Windows path, but this is not Windows. "
                "It would be treated as a relative name and the vault would be built "
                "inside the current directory. Use a path for the platform you are on."
            )
        raise ConfigError(
            f"{source} must be an absolute path, got {value!r}. A relative path "
            "resolves against whatever directory Jarvis was started in, so the vault "
            "would move depending on where you launched it. Use a full path or one "
            "starting with ~."
        )

    return root.resolve()


def _build_vault(table: dict[str, Any]) -> VaultConfig:
    section = table.get("vault")
    if not isinstance(section, dict):
        raise ConfigError("config is missing the [vault] section")

    root = _vault_root(str(_require(table, "vault", "root")))
    names = ("accounts", "knowledge", "ranger", "memory", "inbox", "drafts", "log")
    paths = {name: _resolve_under(root, str(_require(table, "vault", name)), name) for name in names}

    ranger_root = paths["ranger"]
    for name in ("memory", "inbox", "drafts", "log"):
        path = paths[name]
        if path != ranger_root and ranger_root not in path.parents:
            raise ConfigError(
                f"vault.{name} must sit under vault.ranger; Jarvis writes nowhere else"
            )
    for name in ("accounts", "knowledge"):
        path = paths[name]
        if path == ranger_root or ranger_root in path.parents:
            raise ConfigError(
                f"vault.{name} is read only and must not sit under vault.ranger"
            )

    return VaultConfig(
        root=root,
        snapshot=bool(section.get("snapshot", True)),
        snapshot_max_file_mb=float(section.get("snapshot_max_file_mb", 5.0)),
        snapshot_max_total_mb=float(section.get("snapshot_max_total_mb", 200.0)),
        **paths,
    )


def _validate_schedule(schedule: ScheduleConfig) -> None:
    if schedule.quiet_start_hour == schedule.quiet_end_hour:
        raise ConfigError(
            "schedule.quiet_start_hour and schedule.quiet_end_hour are equal, "
            "which leaves no quiet window at all"
        )
    if schedule.in_quiet_hours(schedule.morning_hour):
        raise ConfigError(
            f"schedule.morning_hour ({schedule.morning_hour:02d}:00) falls inside the quiet "
            f"window ({schedule.quiet_start_hour:02d}:00 to {schedule.quiet_end_hour:02d}:00). "
            "The morning surface would never fire. Move one of them."
        )


def _read_toml(path: Path) -> tuple[dict[str, Any], list[str]]:
    raw = path.read_text(encoding="utf-8")
    repaired, notes = _repair_windows_paths(raw)
    try:
        return tomllib.loads(repaired), notes
    except tomllib.TOMLDecodeError as exc:
        hint = ""
        if "\\" in raw:
            hint = (
                "\n\nThere are backslashes in this file. Inside a double-quoted TOML "
                "string a backslash starts an escape sequence, so a Windows path has to "
                "be written with single quotes (root = 'C:\\Users\\you\\Vault') or with "
                "forward slashes, which work fine on Windows."
            )
        raise ConfigError(f"{path} is not valid TOML: {exc}{hint}") from exc


def load_config(path: str | Path | None = None, *, load_env: bool = True) -> Config:
    """Read, validate and freeze the configuration.

    Fails loudly at startup rather than quietly at the first bad turn.
    """
    if load_env:
        load_dotenv(override=False)

    if path is None:
        path = os.environ.get("RANGER_CONFIG", DEFAULT_CONFIG_FILENAME)
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(
            f"no config file at {config_path}. Copy ranger.toml from the repo root "
            "or set RANGER_CONFIG."
        )

    table, path_notes = _read_toml(config_path)

    local_path = config_path.with_name(config_path.stem + LOCAL_SUFFIX)
    overrides: list[str] = []
    if local_path.is_file():
        local_table, local_notes = _read_toml(local_path)
        path_notes.extend(local_notes)
        table, overrides = _merge_tables(table, local_table)

    _check_known_keys(table)

    model = ModelConfig(
        provider=str(_require(table, "model", "provider")),
        name=str(_require(table, "model", "name")),
        max_tokens=int(_require(table, "model", "max_tokens")),
        effort=str(table["model"].get("effort", "low")),
        max_tool_rounds=int(_require(table, "model", "max_tool_rounds")),
        history_turns=int(_require(table, "model", "history_turns")),
        timeout_seconds=float(table["model"].get("timeout_seconds", 60.0)),
        max_retries=int(table["model"].get("max_retries", 3)),
        retry_backoff_seconds=float(table["model"].get("retry_backoff_seconds", 1.0)),
        cache_prompt=bool(table["model"].get("cache_prompt", True)),
        price_input=float(table["model"].get("price_input", 0.0)),
        price_output=float(table["model"].get("price_output", 0.0)),
        price_cache_write=float(table["model"].get("price_cache_write", 0.0)),
        price_cache_read=float(table["model"].get("price_cache_read", 0.0)),
    )
    if model.max_tool_rounds < 1:
        raise ConfigError("model.max_tool_rounds must be at least 1")
    allowed_effort = {"", "low", "medium", "high", "xhigh", "max"}
    if model.effort not in allowed_effort:
        raise ConfigError(
            f"model.effort must be one of {sorted(allowed_effort - {''})}, or empty "
            f"to leave it unset. Got {model.effort!r}."
        )
    if model.max_retries < 0:
        raise ConfigError("model.max_retries cannot be negative")
    if model.timeout_seconds <= 0:
        raise ConfigError("model.timeout_seconds must be greater than zero")

    vault = _build_vault(table)

    knowledge_section = table.get("knowledge", {})
    knowledge = KnowledgeConfig(
        budget_chars=int(table.get("context", {}).get("budget_chars", 60000)),
        priority=tuple(str(name) for name in knowledge_section.get("priority", ())),
    )

    memory_section = table.get("memory", {})
    memory = MemoryConfig(
        reserve_chars=int(memory_section.get("reserve_chars", 8000)),
        file=str(memory_section.get("file", "facts.md")),
    )
    context = ContextConfig(
        budget_chars=int(table.get("context", {}).get("budget_chars", 60000))
    )
    if memory.reserve_chars < 0:
        raise ConfigError("memory.reserve_chars cannot be negative")
    if memory.reserve_chars > context.budget_chars:
        raise ConfigError(
            f"memory.reserve_chars ({memory.reserve_chars}) is larger than "
            f"context.budget_chars ({context.budget_chars}), which would leave knowledge "
            "nothing at all."
        )

    schedule = ScheduleConfig(
        morning_hour=_hour(_require(table, "schedule", "morning_hour"), "schedule.morning_hour"),
        quiet_start_hour=_hour(
            _require(table, "schedule", "quiet_start_hour"), "schedule.quiet_start_hour"
        ),
        quiet_end_hour=_hour(
            _require(table, "schedule", "quiet_end_hour"), "schedule.quiet_end_hour"
        ),
    )
    _validate_schedule(schedule)

    accounts_section = table.get("accounts", {})
    accounts = AccountsConfig(
        quiet_after_days=int(accounts_section.get("quiet_after_days", 21)),
        exclude_files=tuple(str(n) for n in accounts_section.get("exclude_files", ())),
        skip_statuses=tuple(str(s) for s in accounts_section.get("skip_statuses", ("UNCONFIRMED",))),
        tier_order=tuple(str(t) for t in accounts_section.get("tier_order", ())),
        closed_stages=tuple(str(t) for t in accounts_section.get("closed_stages", ())),
    )
    if accounts.quiet_after_days < 1:
        raise ConfigError("accounts.quiet_after_days must be at least 1")

    wake_section = table.get("wake", {})
    wake = WakeConfig(
        enabled=bool(wake_section.get("enabled", False)),
        phrase=str(wake_section.get("phrase", "hey jarvis")),
        model=str(wake_section.get("model", "hey_jarvis")),
        threshold=float(wake_section.get("threshold", 0.6)),
        grace_seconds=float(wake_section.get("grace_seconds", 3.0)),
        silence_seconds=float(wake_section.get("silence_seconds", 0.9)),
        max_seconds=float(wake_section.get("max_seconds", 30.0)),
        preroll_seconds=float(wake_section.get("preroll_seconds", 1.5)),
        idle_disarm_minutes=float(wake_section.get("idle_disarm_minutes", 15.0)),
        mic_check_seconds=float(wake_section.get("mic_check_seconds", 5.0)),
        conversation_seconds=float(wake_section.get("conversation_seconds", 8.0)),
        conversation_reopens=int(wake_section.get("conversation_reopens", 3)),
        conversation_requires_visible=bool(
            wake_section.get("conversation_requires_visible", True)
        ),
        surface_on_wake=bool(wake_section.get("surface_on_wake", True)),
        surface_topmost=bool(wake_section.get("surface_topmost", False)),
        surface_topmost_never_in_call=bool(
            wake_section.get("surface_topmost_never_in_call", True)
        ),
    )
    if len(wake.phrase.split()) < 2:
        raise ConfigError(
            f"wake.phrase is {wake.phrase!r}, which is one word. A single common word is "
            "a wake phrase that fires constantly, so two are required."
        )
    if not 0.0 < wake.threshold <= 1.0:
        raise ConfigError("wake.threshold must be above 0 and at most 1")
    if wake.conversation_seconds <= 0:
        raise ConfigError(
            "wake.conversation_seconds must be above 0. To turn conversation mode off, "
            "set wake.conversation_reopens = 0."
        )
    if wake.conversation_reopens < 0:
        raise ConfigError("wake.conversation_reopens cannot be negative. 0 turns it off.")
    if wake.idle_disarm_minutes <= 0:
        raise ConfigError(
            "wake.idle_disarm_minutes must be positive. Hands free that never disarms "
            "itself is the failure this setting exists to prevent."
        )

    brief_section = table.get("brief", {})
    brief = BriefConfig(
        lines=int(brief_section.get("lines", 5)),
        slipping_max=int(brief_section.get("slipping_max", 3)),
        deals_max=int(brief_section.get("deals_max", 3)),
        cold_after_days=int(brief_section.get("cold_after_days", 90)),
        decision_prompt=bool(brief_section.get("decision_prompt", True)),
        seen_file=str(brief_section.get("seen_file", "quiet-seen.md")),
        dormant_file=str(brief_section.get("dormant_file", "dormant.md")),
    )
    if brief.lines < 1:
        raise ConfigError("brief.lines must be at least 1")
    if brief.cold_after_days <= accounts.quiet_after_days:
        raise ConfigError(
            f"brief.cold_after_days ({brief.cold_after_days}) must be more than "
            f"accounts.quiet_after_days ({accounts.quiet_after_days}), or every "
            "lapsed account is cold the moment it lapses and the brief is empty"
        )

    recall_section = table.get("recall", {})
    recall = RecallConfig(
        activities=int(recall_section.get("activities", 5)),
        detail_activities=int(recall_section.get("detail_activities", 20)),
        section_chars=int(recall_section.get("section_chars", 400)),
        body_chars=int(recall_section.get("body_chars", 300)),
        max_chars=int(recall_section.get("max_chars", 4000)),
    )
    drafts = DraftsConfig(
        filename_format=str(
            table.get("drafts", {}).get("filename_format", "{date}-{slug}.md")
        )
    )

    documents_section = table.get("documents", {})
    documents = DocumentsConfig(
        page_size=str(documents_section.get("page_size", "A4")),
        assembly=bool(documents_section.get("assembly", True)),
        assembly_particles=int(documents_section.get("assembly_particles", 220)),
        assembly_seconds=float(documents_section.get("assembly_seconds", 1.1)),
        preview_blocks=int(documents_section.get("preview_blocks", 400)),
        preview_rows=int(documents_section.get("preview_rows", 300)),
    )
    if documents.page_size.upper() not in ("A4", "LETTER"):
        raise ConfigError(
            f"documents.page_size is {documents.page_size!r}. It must be A4 or letter."
        )
    if not 0 <= documents.assembly_particles <= 2000:
        raise ConfigError(
            "documents.assembly_particles must be between 0 and 2000. This window "
            "sits open during customer calls; an unbounded particle count is a "
            "frame rate nobody chose."
        )
    if documents.preview_blocks < 1 or documents.preview_rows < 1:
        raise ConfigError("documents.preview_blocks and preview_rows must be at least 1")

    imports_section = table.get("imports", {})
    imports = ImportsConfig(
        max_mb=float(imports_section.get("max_mb", 25.0)),
        extract_rows=int(imports_section.get("extract_rows", 60)),
        extract_sheets=int(imports_section.get("extract_sheets", 40)),
        extract_on_drop=bool(imports_section.get("extract_on_drop", False)),
    )
    if not 0 < imports.max_mb <= 500:
        raise ConfigError("imports.max_mb must be above 0 and at most 500")
    if imports.extract_rows < 1 or imports.extract_sheets < 1:
        raise ConfigError("imports.extract_rows and extract_sheets must be at least 1")

    gp_section = table.get("gp", {})
    gp = GpConfig(
        currency=str(gp_section.get("currency", "$")),
        year_starts_month=int(gp_section.get("year_starts_month", 1)),
        stale_after_days=int(gp_section.get("stale_after_days", 45)),
    )
    if not 1 <= gp.year_starts_month <= 12:
        raise ConfigError(
            f"gp.year_starts_month is {gp.year_starts_month}, which is not a month. "
            "1 is January and a calendar year; 4 is a financial year starting in April."
        )
    if gp.stale_after_days < 1:
        raise ConfigError(
            "gp.stale_after_days must be at least 1. Turning the stale marker off is "
            "not an option: a figure that looks current when it is months old is the "
            "one failure this dashlet has."
        )

    voice_section = table.get("voice", {})
    voice = VoiceConfig(
        push_to_talk=bool(voice_section.get("push_to_talk", True)),
        wake_word=bool(voice_section.get("wake_word", False)),
        voice_id=str(voice_section.get("voice_id", "")),
        model_id=str(voice_section.get("model_id", "eleven_flash_v2_5")),
        trigger=str(voice_section.get("trigger", "hold")),
        key=str(voice_section.get("key", "space")),
        input_device=str(voice_section.get("input_device", "")),
        output_device=str(voice_section.get("output_device", "")),
        sample_rate=int(voice_section.get("sample_rate", 16000)),
        channels=int(voice_section.get("channels", 1)),
        max_seconds=int(voice_section.get("max_seconds", 60)),
    )
    if voice.trigger not in {"hold", "toggle"}:
        raise ConfigError(f'voice.trigger must be "hold" or "toggle", got {voice.trigger!r}')
    if voice.sample_rate < 8000:
        raise ConfigError(f"voice.sample_rate looks wrong: {voice.sample_rate}")
    if voice.channels not in (1, 2):
        raise ConfigError(f"voice.channels must be 1 or 2, got {voice.channels}")
    if voice.max_seconds < 1:
        raise ConfigError("voice.max_seconds must be at least 1")
    if voice.wake_word:
        raise ConfigError("voice.wake_word is not built. Push to talk only.")

    stt_section = table.get("stt", {})
    stt = SttConfig(
        provider=str(stt_section.get("provider", "deepgram")),
        model=str(stt_section.get("model", "nova-3")),
        language=str(stt_section.get("language", "en")),
        smart_format=bool(stt_section.get("smart_format", True)),
        punctuate=bool(stt_section.get("punctuate", True)),
        timeout_seconds=float(stt_section.get("timeout_seconds", 30.0)),
        keyterms=tuple(str(t).strip() for t in stt_section.get("keyterms", ()) if str(t).strip()),
        boost=float(stt_section.get("boost", 2.0)),
        max_hints=int(stt_section.get("max_hints", 100)),
        keyterms_from_accounts=bool(stt_section.get("keyterms_from_accounts", True)),
    )
    if stt.max_hints < 0:
        raise ConfigError("stt.max_hints cannot be negative")
    if stt.timeout_seconds <= 0:
        raise ConfigError("stt.timeout_seconds must be greater than zero")

    tts_section = table.get("tts", {})
    tts = TtsConfig(
        provider=str(tts_section.get("provider", "elevenlabs")),
        voice_id=str(tts_section.get("voice_id", "")),
        model_id=str(tts_section.get("model_id", "eleven_flash_v2_5")),
        output_format=str(tts_section.get("output_format", "pcm_24000")),
        stability=float(tts_section.get("stability", 0.5)),
        similarity_boost=float(tts_section.get("similarity_boost", 0.75)),
        speed=float(tts_section.get("speed", 1.0)),
        timeout_seconds=float(tts_section.get("timeout_seconds", 30.0)),
    )
    if not 0.0 <= tts.stability <= 1.0:
        raise ConfigError("tts.stability must be between 0 and 1")

    gate_section = table.get("gate", {})
    gate = GateConfig(
        timeout_seconds=float(gate_section.get("timeout_seconds", 120.0)),
        voice_holds=bool(gate_section.get("voice_holds", True)),
    )
    if gate.timeout_seconds <= 0:
        raise ConfigError("gate.timeout_seconds must be greater than zero")
    if not gate.voice_holds:
        raise ConfigError(
            "gate.voice_holds = false is not supported. A spoken yes is not consent: "
            "transcription is good but not perfect, and a misheard no is a yes."
        )

    heartbeat_section = table.get("heartbeat", {})
    heartbeat = HeartbeatConfig(
        enabled=bool(heartbeat_section.get("enabled", True)),
        interval_seconds=int(heartbeat_section.get("interval_seconds", 300)),
        check_timeout_seconds=int(heartbeat_section.get("check_timeout_seconds", 120)),
    )
    if heartbeat.interval_seconds < 10:
        raise ConfigError("heartbeat.interval_seconds must be at least 10")
    if heartbeat.check_timeout_seconds < 1:
        raise ConfigError("heartbeat.check_timeout_seconds must be at least 1")

    server_section = table.get("server", {})
    server = ServerConfig(
        host=str(server_section.get("host", "127.0.0.1")),
        port=int(server_section.get("port", 8765)),
    )

    warnings: list[str] = list(path_notes)
    if not vault.root.is_dir():
        warnings.append(f"vault root does not exist yet: {vault.root}")
    else:
        for label, path in (("accounts", vault.accounts), ("knowledge", vault.knowledge)):
            if not path.is_dir():
                warnings.append(f"vault.{label} does not exist yet: {path}")
        if not vault.ranger.is_dir():
            warnings.append(
                f"Ranger's own folder does not exist yet: {vault.ranger}. Run 'ranger init'."
            )

    return Config(
        model=model,
        vault=vault,
        knowledge=knowledge,
        memory=memory,
        context=context,
        schedule=schedule,
        accounts=accounts,
        brief=brief,
        wake=wake,
        recall=recall,
        drafts=drafts,
        documents=documents,
        imports=imports,
        gp=gp,
        voice=voice,
        stt=stt,
        tts=tts,
        gate=gate,
        heartbeat=heartbeat,
        server=server,
        source_path=config_path,
        local_path=local_path if local_path.is_file() else None,
        overrides=tuple(overrides),
        warnings=tuple(warnings),
    )


def require_api_key(env_var: str = "ANTHROPIC_API_KEY") -> str:
    key = os.environ.get(env_var, "").strip()
    if not key:
        raise ConfigError(
            f"{env_var} is not set. Copy .env.example to .env and fill it in."
        )
    return key
