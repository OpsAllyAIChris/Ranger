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

    @property
    def writable_roots(self) -> tuple[Path, ...]:
        return (self.memory, self.inbox, self.drafts, self.log)

    @property
    def append_only_roots(self) -> tuple[Path, ...]:
        return (self.log,)


@dataclass(frozen=True)
class KnowledgeConfig:
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
class VoiceConfig:
    push_to_talk: bool
    wake_word: bool
    tts_provider: str
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
    max_hints: int = 100


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int


@dataclass(frozen=True)
class Config:
    model: ModelConfig
    vault: VaultConfig
    knowledge: KnowledgeConfig
    schedule: ScheduleConfig
    accounts: AccountsConfig
    recall: RecallConfig
    drafts: DraftsConfig
    voice: VoiceConfig
    stt: SttConfig
    server: ServerConfig
    source_path: Path | None = None
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
                    "a backslash means an escape sequence. Ranger read it as a literal "
                    f"path. To silence this, use single quotes: {key} = '{value}'"
                )
                continue

        out.append(line)

    result = "\n".join(out)
    if text.endswith("\n"):
        result += "\n"
    return result, notes


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
    Ranger would quietly build a vault inside whatever folder it was launched
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
            "resolves against whatever directory Ranger was started in, so the vault "
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
                f"vault.{name} must sit under vault.ranger; Ranger writes nowhere else"
            )
    for name in ("accounts", "knowledge"):
        path = paths[name]
        if path == ranger_root or ranger_root in path.parents:
            raise ConfigError(
                f"vault.{name} is read only and must not sit under vault.ranger"
            )

    return VaultConfig(root=root, **paths)


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

    raw = config_path.read_text(encoding="utf-8")
    repaired, path_notes = _repair_windows_paths(raw)
    try:
        table = tomllib.loads(repaired)
    except tomllib.TOMLDecodeError as exc:
        hint = ""
        if "\\" in raw:
            hint = (
                "\n\nThere are backslashes in this file. Inside a double-quoted TOML "
                "string a backslash starts an escape sequence, so a Windows path has to "
                "be written with single quotes (root = 'C:\\Users\\you\\Vault') or with "
                "forward slashes, which work fine on Windows."
            )
        raise ConfigError(f"{config_path} is not valid TOML: {exc}{hint}") from exc

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
        budget_chars=int(knowledge_section.get("budget_chars", 60000)),
        priority=tuple(str(name) for name in knowledge_section.get("priority", ())),
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
    )
    if accounts.quiet_after_days < 1:
        raise ConfigError("accounts.quiet_after_days must be at least 1")

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

    voice_section = table.get("voice", {})
    voice = VoiceConfig(
        push_to_talk=bool(voice_section.get("push_to_talk", True)),
        wake_word=bool(voice_section.get("wake_word", False)),
        tts_provider=str(voice_section.get("tts_provider", "elevenlabs")),
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
    )
    if stt.timeout_seconds <= 0:
        raise ConfigError("stt.timeout_seconds must be greater than zero")

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
        schedule=schedule,
        accounts=accounts,
        recall=recall,
        drafts=drafts,
        voice=voice,
        stt=stt,
        server=server,
        source_path=config_path,
        warnings=tuple(warnings),
    )


def require_api_key(env_var: str = "ANTHROPIC_API_KEY") -> str:
    key = os.environ.get(env_var, "").strip()
    if not key:
        raise ConfigError(
            f"{env_var} is not set. Copy .env.example to .env and fill it in."
        )
    return key
