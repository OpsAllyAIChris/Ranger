"""Shared fixtures.

Everything here reaches test modules through pytest's fixture mechanism, which
means no test module ever imports from this file. An import like
`from tests.conftest import ...` only resolves when the repo root happens to be
on sys.path, which is true under `python -m pytest` and false under a bare
`pytest`. The fixtures below work either way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ranger.config import Config, load_config

# vault.root is a TOML *literal* string (single quotes) so a Windows tmp_path
# with backslashes survives verbatim. A basic string would read them as escapes.
CONFIG_TEMPLATE = """
[model]
provider = "anthropic"
name = "test-model"
max_tokens = 512
effort = "low"
max_tool_rounds = {max_tool_rounds}
history_turns = {history_turns}

[vault]
root = '{root}'
accounts = "Accounts"
knowledge = "Knowledge"
ranger = "Ranger"
memory = "Ranger/memory"
inbox = "Ranger/inbox"
drafts = "Ranger/drafts"
log = "Ranger/log"

[knowledge]
budget_chars = {budget}
priority = ["company.md", "icp.md"]

[schedule]
morning_hour = {morning_hour}
quiet_start_hour = {quiet_start}
quiet_end_hour = {quiet_end}

[accounts]
quiet_after_days = 21

[drafts]
filename_format = "{{date}}-{{slug}}.md"

[voice]
push_to_talk = true
wake_word = false
trigger = "hold"
key = "space"
input_device = ""
output_device = ""
sample_rate = 16000
channels = 1
max_seconds = 60

[stt]
provider = "deepgram"
model = "nova-3"
language = "en"
keyterms = ["Wexxar", "Illes", "corrugated"]

[tts]
provider = "elevenlabs"
voice_id = "test-voice-id"
model_id = "eleven_flash_v2_5"
output_format = "pcm_24000"

[server]
host = "127.0.0.1"
port = 8765
"""

DEFAULTS: dict[str, object] = {
    "budget": 60000,
    "morning_hour": 7,
    "quiet_start": 18,
    "quiet_end": 6,
    "max_tool_rounds": 6,
    "history_turns": 24,
}


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    """A vault with the full layout and nothing in it."""
    root = tmp_path / "Vault"
    for folder in (
        "Accounts",
        "Knowledge",
        "Ranger/memory",
        "Ranger/inbox",
        "Ranger/drafts",
        "Ranger/log",
    ):
        (root / folder).mkdir(parents=True)
    return root


@pytest.fixture
def config_file(tmp_path: Path, vault_root: Path):
    """Write a ranger.toml and hand back its path.

    Use this when the test needs the file itself: to corrupt it, or to assert
    that loading it raises.
    """

    def _write(**overrides: object) -> Path:
        values = {**DEFAULTS, "root": str(vault_root), **overrides}
        path = tmp_path / "ranger.toml"
        path.write_text(CONFIG_TEMPLATE.format(**values), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def make_config(config_file, monkeypatch):
    """Build a loaded Config, overriding any template value."""

    def _make(**overrides: object) -> Config:
        monkeypatch.delenv("RANGER_VAULT_ROOT", raising=False)
        return load_config(config_file(**overrides), load_env=False)

    return _make


@pytest.fixture
def config(make_config) -> Config:
    return make_config()
