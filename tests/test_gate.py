"""Tier 6: the rails.

The confirmation gate, the proof that planted instructions are flagged rather
than obeyed, the audit trail, and the kill switch.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest

from ranger.audit import AuditLog
from ranger.core import Ranger
from ranger.events import ConfirmationRequested, Notice, StateChanged, State, ToolFinished
from ranger.gate import (
    APPROVED,
    DECLINED,
    HELD,
    ConfirmationRequest,
    DenyingGate,
    HoldingGate,
    ScriptedGate,
    TerminalGate,
    ask_with_timeout,
)
from ranger.heartbeat import Heartbeat, Inbox, KillSwitch, build_checks
from ranger.testing import ScriptedProvider
from ranger.toolset import build_registry
from ranger.vault import Vault

FIXTURES = Path(__file__).parent / "fixtures" / "vault"


@pytest.fixture
def seeded(config, vault_root):
    for name in ("Accounts", "Knowledge"):
        shutil.rmtree(vault_root / name, ignore_errors=True)
        shutil.copytree(FIXTURES / name, vault_root / name)
    return config


def agent_with(config, gate, script, **kwargs):
    vault = Vault(config.vault)
    return Ranger(
        config=config,
        provider=ScriptedProvider(script),
        registry=build_registry(config, vault),
        vault=vault,
        gate=gate,
        **kwargs,
    )


FORGET = [
    {"tools": [{"name": "forget", "input": {"fact": "morning meetings"}}]},
    {"text": "Done."},
]


async def collect(agent, text):
    return [e async for e in agent.turn(text)]


def remember(config, text="Chris prefers morning meetings."):
    from ranger.memory import append_fact

    append_fact(Vault(config.vault), config.vault.memory, text, today=date(2026, 9, 7))


# -- the gate --------------------------------------------------------------


async def test_a_confirmed_tool_actually_runs_when_approved(seeded):
    """Turning the gate on has to make forget work, not just stop refusing."""
    remember(seeded)
    gate = ScriptedGate([APPROVED])
    agent = agent_with(seeded, gate, FORGET)

    events = await collect(agent, "forget that I prefer morning meetings")

    assert [r.tool for r in gate.asked] == ["forget"]
    finished = [e for e in events if isinstance(e, ToolFinished)][0]
    assert finished.ok and "removed 1" in finished.summary

    text = (seeded.vault.memory / "facts.md").read_text(encoding="utf-8")
    assert "morning meetings" not in text


async def test_declining_leaves_everything_untouched(seeded):
    remember(seeded)
    before = (seeded.vault.memory / "facts.md").read_text(encoding="utf-8")
    agent = agent_with(seeded, ScriptedGate([DECLINED]), FORGET)

    events = await collect(agent, "forget that")

    assert (seeded.vault.memory / "facts.md").read_text(encoding="utf-8") == before
    finished = [e for e in events if isinstance(e, ToolFinished)][0]
    assert not finished.ok and finished.summary == "declined"


async def test_the_operator_is_told_what_is_about_to_happen(seeded):
    """Stating it plainly is the point; a yes to "run forget" is not consent."""
    remember(seeded)
    gate = ScriptedGate([DECLINED])
    await collect(agent_with(seeded, gate, FORGET), "forget that")

    action = gate.asked[0].action
    assert "Permanently remove from memory" in action
    assert "morning meetings" in action
    assert "cannot be undone" in action


async def test_an_unconfirmed_tool_never_reaches_the_gate(seeded):
    """Read-only actions flow freely."""
    gate = ScriptedGate([APPROVED])
    agent = agent_with(
        seeded, gate,
        [{"tools": [{"name": "account_recall", "input": {"account": "Illes"}}]}, {"text": "ok"}],
    )
    await collect(agent, "where are we on Illes")
    assert gate.asked == []


async def test_drafting_is_not_gated(seeded):
    """Creating a file inside Ranger's own folder is expressly permitted."""
    gate = ScriptedGate([])
    agent = agent_with(
        seeded, gate,
        [{"tools": [{"name": "draft_and_hold", "input": {"title": "Hi", "body": "Body."}}]},
         {"text": "Drafted."}],
    )
    await collect(agent, "draft something")
    assert gate.asked == []
    assert len(list(seeded.vault.drafts.glob("*.md"))) == 1


async def test_approval_is_never_remembered(seeded):
    """Approving one action does not pre-authorise the next."""
    remember(seeded, "Chris prefers morning meetings.")
    remember(seeded, "Chris covers Texas.")
    gate = ScriptedGate([APPROVED, APPROVED])
    agent = agent_with(
        seeded, gate,
        [
            {"tools": [{"name": "forget", "input": {"fact": "morning meetings"}}]},
            {"tools": [{"name": "forget", "input": {"fact": "Texas"}}]},
            {"text": "Both gone."},
        ],
    )
    await collect(agent, "forget both of those")
    assert len(gate.asked) == 2, "each action has to ask on its own"


async def test_the_default_gate_refuses(seeded):
    """Nothing consequential runs just because no gate was wired in."""
    remember(seeded)
    agent = agent_with(seeded, DenyingGate(), FORGET)
    await collect(agent, "forget that")
    assert "morning meetings" in (seeded.vault.memory / "facts.md").read_text(encoding="utf-8")


async def test_the_turn_announces_awaiting_confirmation(seeded):
    remember(seeded)
    events = await collect(agent_with(seeded, ScriptedGate([DECLINED]), FORGET), "forget that")
    states = [e.state for e in events if isinstance(e, StateChanged)]
    assert State.AWAITING_CONFIRMATION in states
    assert any(isinstance(e, ConfirmationRequested) for e in events)


async def test_the_terminal_gate_reads_a_yes(config):
    answers = iter(["y\n"])
    gate = TerminalGate(prompt=lambda _: next(answers))
    decision = await gate.ask(ConfirmationRequest("forget", "Remove a fact.", {}))
    assert decision.approved


@pytest.mark.parametrize("answer", ["n\n", "\n", "no", "maybe", "Y E S"])
async def test_the_terminal_gate_treats_anything_but_yes_as_no(config, answer):
    gate = TerminalGate(prompt=lambda _: answer)
    assert not (await gate.ask(ConfirmationRequest("forget", "Remove a fact.", {}))).approved


# -- voice and the heartbeat share one mechanism ---------------------------


async def test_voice_holds_the_action_and_puts_it_in_the_inbox(config, vault):
    """A spoken yes is not consent: transcription is good, not perfect."""
    inbox = Inbox(vault, config.vault.inbox)
    gate = HoldingGate(inbox, now=lambda: datetime(2026, 9, 7, 9, 0))

    decision = await gate.ask(
        ConfirmationRequest("forget", "Permanently remove a fact.", {}, origin="voice")
    )
    assert decision.held and not decision.approved

    pending = inbox.pending()
    assert len(pending) == 1
    assert pending[0].title.startswith("Waiting on you:")
    assert "Nothing has been done" in pending[0].body
    assert "voice" in pending[0].body


async def test_a_heartbeat_action_nobody_answers_does_nothing_and_leaves_a_note(config, vault):
    """Never block forever waiting on someone who is asleep."""
    class Hangs:
        name = "hangs"

        async def ask(self, request):
            await asyncio.sleep(30)
            return None

    decision = await ask_with_timeout(
        Hangs(), ConfirmationRequest("forget", "Remove a fact.", {}, origin="heartbeat"), 0.05
    )
    assert decision.held and not decision.approved
    assert "nobody answered" in decision.reason


async def test_a_held_action_tells_the_model_not_to_work_around_it(seeded, vault):
    remember(seeded)
    gate = HoldingGate(Inbox(vault, seeded.vault.inbox))
    agent = agent_with(seeded, gate, FORGET, origin="voice")

    await collect(agent, "forget that")
    tool_result = next(
        block for m in agent.messages if isinstance(m.get("content"), list)
        for block in m["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    )
    assert "do not try another way round it" in tool_result["content"]


def test_a_spoken_yes_cannot_be_configured_on(tmp_path, config_file):
    from ranger.config import ConfigError, load_config

    path = config_file()
    (path.parent / (path.stem + ".local.toml")).write_text(
        "[gate]\nvoice_holds = false\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="misheard no is a yes"):
        load_config(path, load_env=False)


# -- the audit trail -------------------------------------------------------


def test_the_log_is_append_only_and_ranger_cannot_rewrite_it(config, vault):
    from ranger.vault import VaultWriteDenied

    log = AuditLog(vault, config.vault.log, now=lambda: datetime(2026, 9, 7, 9, 0))
    path = log.write("turn", "where are we on Illes")
    log.write("tool", "account_recall ok: Illes Foods")

    text = path.read_text(encoding="utf-8")
    assert text.count("| 09:00:00 |") == 2
    with pytest.raises(VaultWriteDenied):
        vault.overwrite(path, "rewritten", allow_overwrite=True)


def test_a_multi_line_detail_stays_on_one_row(config, vault):
    log = AuditLog(vault, config.vault.log, now=lambda: datetime(2026, 9, 7, 9, 0))
    path = log.write("tool", "line one\nline two | with a pipe")
    body = path.read_text(encoding="utf-8").splitlines()[-1]
    assert body.startswith("| 09:00:00 |") and body.endswith("|")
    assert "\\|" in body


async def test_a_turn_and_its_tools_are_logged(seeded, vault):
    log = AuditLog(vault, seeded.vault.log)
    agent = agent_with(
        seeded, ScriptedGate([]),
        [{"tools": [{"name": "account_recall", "input": {"account": "Illes"}}]}, {"text": "ok"}],
        audit=log,
    )
    await collect(agent, "where are we on Illes Foods")

    text = log.read()
    assert "where are we on Illes Foods" in text
    assert "account_recall ok" in text


async def test_what_was_confirmed_and_what_was_declined_is_logged(seeded, vault):
    remember(seeded)
    log = AuditLog(vault, seeded.vault.log)
    await collect(agent_with(seeded, ScriptedGate([DECLINED]), FORGET, audit=log), "forget that")
    assert "declined" in log.read()

    remember(seeded, "Chris covers Texas.")
    await collect(
        agent_with(seeded, ScriptedGate([APPROVED]),
                   [{"tools": [{"name": "forget", "input": {"fact": "Texas"}}]}, {"text": "ok"}],
                   audit=log),
        "forget Texas",
    )
    assert "approved" in log.read()


async def test_a_broken_log_never_stops_a_turn(config):
    """A lost log line is bad. A lost turn because the disk was full is worse."""
    class Broken:
        def write(self, *a, **k):
            raise OSError("disk full")

    agent = Ranger(config=config, provider=ScriptedProvider([{"text": "ok"}]), audit=Broken())
    events = [e async for e in agent.turn("hello")]
    assert any(getattr(e, "text", "") == "ok" for e in events)


async def test_the_heartbeat_writes_to_the_log(config, vault):
    log = AuditLog(vault, config.vault.log)

    class Check:
        name = "fake"
        runs_in_quiet_hours = True

        def status(self, now, inbox):
            from ranger.heartbeat import Dueness

            return Dueness(True, "due")

        async def run(self):
            from ranger.heartbeat import Notice

            return Notice("fake", "A thing", "Body.", datetime(2026, 9, 7, 9, 0))

    beat = Heartbeat(config, Inbox(vault, config.vault.inbox), [Check()],
                     now=lambda: datetime(2026, 9, 7, 9, 0), audit=log)
    await beat.tick()

    text = log.read(date(2026, 9, 7))
    assert "heartbeat" in text and "surfaced" in text


# -- the kill switch -------------------------------------------------------


def test_the_kill_switch_starts_disengaged(config, vault):
    switch = KillSwitch(vault, config.vault.ranger / "paused.md")
    assert not switch.engaged()


def test_pausing_and_resuming(config, vault):
    switch = KillSwitch(vault, config.vault.ranger / "paused.md")
    path = switch.set(paused=True)
    assert switch.engaged()
    assert "Paused" in path.read_text(encoding="utf-8")

    switch.set(paused=False)
    assert not switch.engaged()


async def test_a_paused_heartbeat_runs_nothing(config, vault):
    class Check:
        name = "fake"
        runs_in_quiet_hours = True

        def status(self, now, inbox):
            from ranger.heartbeat import Dueness

            return Dueness(True, "due")

        async def run(self):
            raise AssertionError("a paused heartbeat must not run a check")

    switch = KillSwitch(vault, config.vault.ranger / "paused.md")
    switch.set(paused=True)
    beat = Heartbeat(config, Inbox(vault, config.vault.inbox), [Check()],
                     now=lambda: datetime(2026, 9, 7, 9, 0), kill_switch=switch)

    report = await beat.tick()
    assert report.ran == ()
    assert "kill switch is engaged" in report.outcomes[0].detail


async def test_conversation_still_works_while_paused(config, vault, seeded):
    """The switch stops proactive behaviour, not talking to Ranger."""
    KillSwitch(vault, seeded.vault.ranger / "paused.md").set(paused=True)
    agent = agent_with(seeded, ScriptedGate([]),
                       [{"tools": [{"name": "account_recall", "input": {"account": "Illes"}}]},
                        {"text": "Rod owes you volumes."}])
    events = await collect(agent, "where are we on Illes")
    assert any(getattr(e, "text", "") for e in events)


def test_the_switch_is_readable_and_editable_by_hand(config, vault):
    """It is a markdown file in the vault, not a hidden flag."""
    switch = KillSwitch(vault, config.vault.ranger / "paused.md")
    path = switch.set(paused=True)
    assert path.suffix == ".md"
    assert "Resume with `ranger resume`" in path.read_text(encoding="utf-8")

    path.write_text(path.read_text(encoding="utf-8").replace("paused: true", "paused: false"),
                    encoding="utf-8")
    assert not switch.engaged()


async def test_the_log_records_what_ranger_said_not_only_what_it_did(seeded, vault):
    """A turn that chose not to act must not look like a turn that broke.

    The operator's voice turns logged "turn" and then nothing at all, and there
    was no way to tell whether the model had declined to call a tool or
    something had failed.
    """
    log = AuditLog(vault, seeded.vault.log)
    agent = agent_with(
        seeded, ScriptedGate([]), [{"text": "You have already told me that."}], audit=log
    )
    await collect(agent, "remember I prefer morning meetings")

    text = log.read()
    assert "You have already told me that." in text
    assert "[no tools]" in text


async def test_the_log_names_the_tools_a_turn_actually_used(seeded, vault):
    log = AuditLog(vault, seeded.vault.log)
    agent = agent_with(
        seeded, ScriptedGate([]),
        [{"tools": [{"name": "account_recall", "input": {"account": "Illes"}}]}, {"text": "Rod owes you volumes."}],
        audit=log,
    )
    await collect(agent, "where are we on Illes")
    assert "[used account_recall]" in log.read()


async def test_a_turn_that_said_nothing_says_so(seeded, vault):
    log = AuditLog(vault, seeded.vault.log)
    await collect(agent_with(seeded, ScriptedGate([]), [{"text": ""}], audit=log), "hello")
    assert "(said nothing)" in log.read()


def test_the_prompt_tells_the_model_the_asking_is_not_its_job(seeded):
    """Politeness must not be able to route around the record.

    Two voice turns produced no tool row, no confirmation row and no inbox
    notice: the model heard "forget that I prefer morning meetings", decided
    the polite thing was to ask out loud first, and so the gate was never
    reached. Nothing unsafe happened, which is the trap. The confirmation is
    the only thing that writes to the log and the inbox, so a question asked
    in prose instead is a confirmation that leaves no trace.
    """
    from ranger.prompts import build_system_prompt
    from ranger.vault import Vault

    registry = build_registry(seeded, Vault(seeded.vault))
    prompt = build_system_prompt(seeded, registry=registry)

    assert "Tools that ask first" in prompt
    assert "You do not run that confirmation yourself" in prompt
    assert "held" in prompt


def test_the_prompt_names_the_gated_tools_from_the_confirm_flag(seeded):
    """Derived from the registry, never from a list of names in this file.

    Same rule the gate itself follows: a tool added later with confirm=True is
    covered without anyone remembering to edit prompts.py.
    """
    from ranger.prompts import build_system_prompt
    from ranger.tools import Tool, ToolResult
    from ranger.vault import Vault

    registry = build_registry(seeded, Vault(seeded.vault))
    expected = [tool.name for tool in registry if tool.confirm]
    assert expected, "the registry has no gated tool, so this test proves nothing"

    prompt = build_system_prompt(seeded, registry=registry)
    for name in expected:
        assert f"`{name}`" in prompt
    for tool in registry:
        if not tool.confirm:
            assert f"`{tool.name}`" not in prompt.split("Tools that ask first")[1]

    async def handler(payload):
        return ToolResult(ok=True, content="")

    registry.register(
        Tool(
            name="send_email",
            description="a tool that does not exist yet",
            input_schema={"type": "object", "properties": {}},
            handler=handler,
            confirm=True,
        )
    )
    assert "`send_email`" in build_system_prompt(seeded, registry=registry)


def test_a_registry_with_nothing_gated_says_nothing_about_the_gate(seeded):
    from ranger.prompts import build_system_prompt
    from ranger.tools import Tool, ToolRegistry, ToolResult

    async def handler(payload):
        return ToolResult(ok=True, content="")

    registry = ToolRegistry(
        [
            Tool(
                name="look_something_up",
                description="reads and nothing else",
                input_schema={"type": "object", "properties": {}},
                handler=handler,
            )
        ]
    )
    assert "Tools that ask first" not in build_system_prompt(seeded, registry=registry)
