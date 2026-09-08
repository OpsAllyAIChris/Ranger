from __future__ import annotations

from dataclasses import replace

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


def test_init_stands_up_a_brand_new_vault(config, vault_root, capsys):
    import shutil

    shutil.rmtree(vault_root)
    assert not vault_root.exists()

    assert cmd_init(config, assume_yes=True) == 0

    for folder in ("Accounts", "Knowledge", "Ranger/memory", "Ranger/drafts", "Ranger/log"):
        assert (vault_root / folder).is_dir(), folder

    out = capsys.readouterr().out
    assert "creates the whole layout" in out
    assert "seed it by hand" in out
    # An empty Knowledge folder is a thing worth saying out loud.
    assert "Jarvis knows nothing about the business yet" in out


def test_init_on_an_existing_vault_only_touches_rangers_folders(config, vault_root, capsys):
    import shutil

    shutil.rmtree(vault_root / "Ranger")
    (vault_root / "Accounts" / "Illes Foods.md").write_text("notes", encoding="utf-8")

    assert cmd_init(config, assume_yes=True) == 0
    out = capsys.readouterr().out
    assert "creates the whole layout" not in out
    assert (vault_root / "Accounts" / "Illes Foods.md").read_text(encoding="utf-8") == "notes"


def test_init_declines_without_a_yes(config, vault_root, monkeypatch, capsys):
    import shutil

    shutil.rmtree(vault_root / "Ranger")
    monkeypatch.setattr("builtins.input", lambda *a: "n")

    assert cmd_init(config, assume_yes=False) == 1
    assert not (vault_root / "Ranger").exists()
    assert "Nothing created" in capsys.readouterr().out


def test_doctor_names_the_unseeded_folders(config, capsys):
    from ranger.cli import _describe_seeding
    from ranger.vault import Vault

    report = _describe_seeding(config, Vault(config.vault))
    assert "Accounts is empty" in report
    assert "Knowledge is empty" in report
    # The priority names order the load; they are not files you must create.
    assert "not required" in report


def test_doctor_names_the_knowledge_files_that_reach_the_model(config, vault_root):
    """Which of the business context is actually in the prompt, by name."""
    from ranger.cli import _describe_seeding
    from ranger.vault import Vault

    (vault_root / "Accounts" / "Illes Foods.md").write_text("notes", encoding="utf-8")
    (vault_root / "Knowledge" / "company.md").write_text("OpsAlly", encoding="utf-8")
    (vault_root / "Knowledge" / "anything at all.md").write_text("also loaded", encoding="utf-8")

    report = _describe_seeding(config, Vault(config.vault))
    assert "1 account note(s)" in report
    assert "loaded   Knowledge/company.md" in report
    # A file named nothing like the priority list is loaded just the same.
    assert "loaded   Knowledge/anything at all.md" in report


def test_doctor_names_the_knowledge_files_that_were_dropped(config, vault_root):
    from dataclasses import replace

    from ranger.cli import _describe_seeding
    from ranger.vault import Vault

    (vault_root / "Knowledge" / "company.md").write_text("c" * 400, encoding="utf-8")
    (vault_root / "Knowledge" / "playbook.md").write_text("p" * 400, encoding="utf-8")
    tight = replace(config, context=replace(config.context, budget_chars=450))

    report = _describe_seeding(tight, Vault(tight.vault))
    assert "loaded   Knowledge/company.md" in report
    assert "DROPPED  not sent  Knowledge/playbook.md" in report


def test_doctor_is_quiet_once_everything_is_seeded(config, vault_root):
    from ranger.cli import _describe_seeding
    from ranger.vault import Vault

    (vault_root / "Accounts" / "Illes Foods.md").write_text("notes", encoding="utf-8")
    for name in config.knowledge.priority:
        (vault_root / "Knowledge" / name).write_text("context", encoding="utf-8")

    report = _describe_seeding(config, Vault(config.vault))
    assert "todo" not in report
    assert "vault-conventions" not in report


async def test_the_turn_reports_whether_the_cache_was_used(config, capsys):
    """cache_read_input_tokens is the only proof caching is on."""
    from ranger.core import Ranger
    from ranger.testing import ScriptedProvider

    class Cached(ScriptedProvider):
        async def stream(self, **kwargs):
            async for event in super().stream(**kwargs):
                from ranger.provider import Completion

                if isinstance(event, Completion):
                    yield Completion(
                        stop_reason=event.stop_reason, text=event.text,
                        content=event.content,
                        usage={"input_tokens": 300, "output_tokens": 90,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 14800},
                    )
                else:
                    yield event

    agent = Ranger(config=config, provider=Cached([{"text": "Rod owes you volumes."}]))
    await _run_turn(agent, "where are we", plain, show_state=False)

    err = capsys.readouterr().err
    assert "98% cached" in err
    assert "session:" in err


def test_doctor_says_hands_free_has_nothing_to_listen_with(config, monkeypatch, capsys):
    """The operator's ask: say it when hands free is switched on, not on the
    first arm. openWakeWord's wheel contains no models, so a machine that
    installed the extra and turned wake.enabled on has nothing to listen with.
    """
    config = replace(config, wake=replace(config.wake, enabled=True))
    monkeypatch.setattr("ranger.wake.available", lambda: (True, "openwakeword is installed"))
    monkeypatch.setattr(
        "ranger.wake.missing_models",
        lambda setting, folder=None: ["melspectrogram.onnx", "hey_jarvis (openWakeWord's own)"],
    )

    code = cmd_doctor(config)
    out = capsys.readouterr().out

    assert "hands free cannot work: no model is installed" in out
    assert "ranger wake install" in out
    assert code >= 1


def test_doctor_is_quiet_about_hands_free_once_the_models_are_there(config, monkeypatch, capsys):
    config = replace(config, wake=replace(config.wake, enabled=True))
    monkeypatch.setattr("ranger.wake.available", lambda: (True, "openwakeword is installed"))
    monkeypatch.setattr("ranger.wake.missing_models", lambda setting, folder=None: [])

    cmd_doctor(config)
    out = capsys.readouterr().out

    assert "no model is installed" not in out
    assert "hands free is on" in out


def test_doctor_separates_a_missing_extra_from_a_missing_model(config, monkeypatch, capsys):
    """Not installing the optional extra is a todo. Installing it, switching
    hands free on and having no model is a problem, because that one looks like
    it works right up until the moment it is needed.
    """
    config = replace(config, wake=replace(config.wake, enabled=True))
    monkeypatch.setattr(
        "ranger.wake.available", lambda: (False, "openwakeword is not installed")
    )

    cmd_doctor(config)
    out = capsys.readouterr().out

    assert "todo     wake.enabled is true but openwakeword is not installed" in out
    assert "problem  hands free" not in out
