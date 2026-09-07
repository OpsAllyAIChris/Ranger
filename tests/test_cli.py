from __future__ import annotations

from ranger.cli import _describe_config, _describe_vault, _run_turn, cmd_doctor, cmd_init, main
from ranger.core import Ranger
from ranger.testing import ScriptedProvider
from ranger.tools import Tool, ToolRegistry, ToolResult


def plain(text, code):
    return text


async def test_run_turn_renders_text_and_tools(config, capsys):
    async def handler(payload):
        return ToolResult(ok=True, content="details", summary="1 account")

    registry = ToolRegistry(
        [
            Tool(
                name="what_went_quiet",
                description="d",
                input_schema={"type": "object", "properties": {}},
                handler=handler,
            )
        ]
    )
    agent = Ranger(
        config=config,
        provider=ScriptedProvider(
            [
                {"tools": [{"name": "what_went_quiet", "input": {}}]},
                {"text": "Illes Foods has gone quiet."},
            ]
        ),
        registry=registry,
    )

    await _run_turn(agent, "what went quiet", plain, show_state=True)
    out = capsys.readouterr()
    assert "Illes Foods has gone quiet." in out.out
    assert "-> what_went_quiet" in out.out
    assert "<- what_went_quiet ok: 1 account" in out.out
    assert "[thinking]" in out.err


def test_describe_helpers_mention_the_real_paths(config):
    text = _describe_config(config) + _describe_vault(config)
    assert str(config.vault.drafts) in text
    assert "append only" in text
    assert config.model.name in text


def test_init_creates_only_rangers_folders(config, vault_root):
    import shutil

    shutil.rmtree(vault_root / "Ranger")
    assert cmd_init(config, assume_yes=True) == 0
    assert (vault_root / "Ranger" / "drafts").is_dir()
    assert sorted(p.name for p in (vault_root / "Ranger").iterdir()) == [
        "drafts",
        "inbox",
        "log",
        "memory",
    ]


def test_doctor_reports_missing_api_key(config, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert cmd_doctor(config) == 1
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().out


def test_main_rejects_a_bad_config_path(capsys):
    assert main(["-c", "/nonexistent/ranger.toml", "doctor"]) == 2
    assert "config error" in capsys.readouterr().err
