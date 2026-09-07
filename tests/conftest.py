from __future__ import annotations

from pathlib import Path

import pytest

CONFIG_TEMPLATE = """
[model]
provider = "anthropic"
name = "test-model"
max_tokens = 512
effort = "low"
max_tool_rounds = {max_tool_rounds}
history_turns = {history_turns}

[vault]
root = "{root}"
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
stt_provider = "deepgram"
tts_provider = "elevenlabs"
voice_id = ""

[server]
host = "127.0.0.1"
port = 8765
"""


def write_config(tmp_path: Path, root: Path, **overrides) -> Path:
    values = {
        "root": str(root),
        "budget": 60000,
        "morning_hour": 7,
        "quiet_start": 18,
        "quiet_end": 6,
        "max_tool_rounds": 6,
        "history_turns": 24,
    }
    values.update(overrides)
    path = tmp_path / "ranger.toml"
    path.write_text(CONFIG_TEMPLATE.format(**values), encoding="utf-8")
    return path


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
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
def config(tmp_path: Path, vault_root: Path, monkeypatch):
    from ranger.config import load_config

    monkeypatch.delenv("RANGER_VAULT_ROOT", raising=False)
    path = write_config(tmp_path, vault_root)
    return load_config(path, load_env=False)
