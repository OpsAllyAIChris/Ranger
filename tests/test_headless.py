"""Item M: a turn with nobody at the other end.

The fifth caller. Speech, typing, the heartbeat and the browser all enter
`Ranger.turn()`; this is the one with no human in front of it, and it enters
the same place. There is no second agent path here and these tests are largely
about proving that.

Three properties carry the weight, and each is tested by running the thing
rather than by reading it:

- **It cannot approve its own gate.** A consequential action stops, writes to
  the inbox, and waits for a keyboard. Work running while the operator is in a
  meeting has *less* consent authority than a voice turn, not more.
- **It is bounded**, and a bound is an outcome rather than an exception: the
  work that was done comes back with it. "I read four accounts and here is what
  I found" is worth something; a bare timeout is worth nothing.
- **It cannot start another one.** One level, guarded in code.

And the property that is not new but has to survive the new entrance: content
from the vault is still fenced, which is in `test_planted_instructions.py` with
the rest of that evidence.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

from ranger import headless
from ranger.core import Ranger
from ranger.testing import ScriptedProvider
from ranger.toolset import build_registry
from ranger.vault import Vault


def agent_for(config, vault, script, *, gate=None, audit=None):
    """A core wired exactly as `build_agent` wires one, with a fixed provider."""
    from ranger.gate import HoldingGate
    from ranger.heartbeat import Inbox
    from ranger.knowledge import KnowledgeLoader

    return Ranger(
        config=config,
        provider=ScriptedProvider(list(script)),
        registry=build_registry(config, vault, audit=audit),
        vault=vault,
        knowledge_loader=KnowledgeLoader(vault, config.vault, config.knowledge),
        gate=gate or HoldingGate(Inbox(vault, config.vault.inbox)),
        audit=audit,
        origin=headless.ORIGIN,
    )


async def go(config, vault, script, prompt="check the Illes account", **kwargs):
    return await headless.run(
        config, prompt, agent=agent_for(config, vault, script, audit=kwargs.pop("audit", None)),
        **kwargs,
    )


# -- it is the same core ----------------------------------------------------


def test_the_headless_caller_builds_the_ordinary_core(config, vault, monkeypatch):
    """One agent core. Not a lite version, not a parallel implementation: the
    same class, the same registry, the same prompt."""
    monkeypatch.setattr("ranger.provider.build_provider", lambda cfg: ScriptedProvider([]))
    agent = headless.build_agent(config, vault=vault)

    assert isinstance(agent, Ranger)
    assert sorted(agent.registry.names()) == sorted(build_registry(config, vault).names())
    assert agent.origin == headless.ORIGIN
    assert "Python computes; you do not" in " ".join(agent.system_prompt().split())


def test_there_is_no_way_to_hand_it_a_gate_that_can_approve(config, vault, monkeypatch):
    """Not a convention. `build_agent` takes no gate, so a caller cannot pass
    one that says yes, and the one it builds cannot."""
    import inspect

    monkeypatch.setattr("ranger.provider.build_provider", lambda cfg: ScriptedProvider([]))
    assert "gate" not in inspect.signature(headless.build_agent).parameters

    from ranger.gate import HoldingGate

    assert isinstance(headless.build_agent(config, vault=vault).gate, HoldingGate)


# -- it cannot approve its own gate ----------------------------------------


async def test_a_gated_action_holds_and_waits_for_a_keyboard(config, vault):
    """**Tested by running it, not by inspecting the gate.**

    `forget` is the gated tool. Headless, it does not run: it writes a notice
    into the inbox and comes back held, and the fact it wrote stays untouched.
    """
    from ranger.heartbeat import Inbox
    from ranger.memory import append_fact, load_memory

    append_fact(vault, config.vault.memory, "Chris prefers morning meetings",
                filename=config.memory.file, today=date(2026, 9, 9))

    record = await go(
        config, vault,
        [
            {"tools": [{"name": "forget", "input": {"fact": "morning meetings"}}]},
            {"text": "That needs your yes, so it is in your inbox."},
        ],
        prompt="forget the morning meetings thing",
    )

    assert record.held, "a headless run may not approve anything"
    assert record.outcome == headless.HELD

    notices = Inbox(vault, config.vault.inbox).pending()
    assert len(notices) == 1
    assert "Waiting on you" in notices[0].title
    assert "Nothing has been done" in notices[0].body

    facts = " ".join(
        fact.text
        for fact in load_memory(vault, config.vault.memory,
                                config.memory.reserve_chars).facts
    )
    assert "morning meetings" in facts, "the fact is still there, because nothing ran"


async def test_the_notice_says_the_headless_run_started_it(config, vault):
    """Whoever reads the inbox later needs to know a person did not ask for
    this at a keyboard."""
    from ranger.heartbeat import Inbox
    from ranger.memory import append_fact

    append_fact(vault, config.vault.memory, "a fact",
                filename=config.memory.file, today=date(2026, 9, 9))
    await go(config, vault, [
        {"tools": [{"name": "forget", "input": {"fact": "a fact"}}]},
        {"text": "held"},
    ])

    body = Inbox(vault, config.vault.inbox).pending()[0].body
    assert "Started from: headless" in body


async def test_an_ungated_tool_runs_exactly_as_it_would_anywhere_else(config, vault):
    """Only consent is different. Reading, drafting and filing are unchanged,
    or this would be a second agent with its own rules."""
    record = await go(config, vault, [
        {"tools": [{"name": "draft_and_hold",
                    "input": {"title": "Illes follow up", "body": "Numbers Thursday."}}]},
        {"text": "Draft is held."},
    ])

    assert record.outcome == headless.COMPLETED
    assert record.tools_used() == ["draft_and_hold"]
    assert list(config.vault.drafts.glob("*.md")), "it really wrote the draft"


# -- it is bounded ----------------------------------------------------------


async def test_too_many_tool_calls_stops_it_and_keeps_the_work(config, vault):
    """**A bound is an outcome, not an exception.** What it had comes back."""
    script = [{"tools": [{"name": "account_recall", "input": {"account": "Illes"}}]}
              for _ in range(6)] + [{"text": "done"}]
    record = await go(config, vault, script, max_tools=2)

    assert record.outcome == headless.BOUNDED
    assert "more than 2 tool calls" in record.why
    assert len(record.steps) >= 2, "the calls it did make are in the record"
    assert record.partial


async def test_running_out_of_time_stops_it_and_says_what_it_managed(config, vault):
    """A bare timeout would send the operator back to do it all by hand."""
    script = [{"tools": [{"name": "account_recall", "input": {"account": "Illes"}}]}
              for _ in range(4)] + [{"text": "done"}]
    record = await go(config, vault, script, max_seconds=-1)

    assert record.outcome == headless.BOUNDED
    assert "ran past" in record.why
    assert "tool call(s) done" in record.why
    assert "stopped at a limit" in record.describe()


async def test_a_provider_that_never_answers_is_cut_off(config, vault, monkeypatch):
    """The backstop. Between-event checks never fire if nothing is ever
    emitted, so a hung call still ends."""
    monkeypatch.setattr(headless, "HARD_GRACE_SECONDS", 0.05)

    class Hangs:
        async def stream(self, *args, **kwargs):
            await asyncio.sleep(30)
            yield {}  # pragma: no cover - never reached

    agent = agent_for(config, vault, [])
    agent.provider = Hangs()
    record = await headless.run(config, "anything", agent=agent, max_seconds=0.05)

    assert record.outcome == headless.BOUNDED
    assert "stopped answering" in record.why


async def test_the_report_of_a_bounded_run_carries_the_work(config, vault):
    script = [{"tools": [{"name": "account_recall", "input": {"account": "Illes"}}]}
              for _ in range(4)] + [{"text": "done"}]
    record = await go(config, vault, script, max_tools=1)
    report = record.report()

    assert "stopped at a limit" in report
    assert "account_recall" in report
    assert "what it had managed" in report or record.text


async def test_the_bounds_are_config_and_there_is_no_unbounded_setting(config_file):
    from ranger.config import ConfigError, load_config

    path = config_file()
    path.write_text(
        path.read_text(encoding="utf-8") + "\n[headless]\nmax_tool_calls = 0\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as caught:
        load_config(path, load_env=False)

    assert "must be above 0" in str(caught.value)
    assert "needs a limit" in str(caught.value)


# -- one level --------------------------------------------------------------


async def test_a_headless_run_cannot_start_another(config, vault):
    """One level, until the bounds are proven on real work. A headless turn
    that can start headless turns is a bound that multiplies."""
    inner: list = []

    async def nested(event):
        if not inner:
            inner.append(await headless.run(config, "and another",
                                            agent=agent_for(config, vault, [{"text": "no"}])))

    await headless.run(
        config, "do a thing",
        agent=agent_for(config, vault, [{"text": "done"}]),
        on_event=lambda event: asyncio.ensure_future(nested(event)),
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert inner and inner[0].outcome == headless.REFUSED
    assert "already in progress" in inner[0].why


async def test_the_guard_is_released_even_when_a_run_fails(config, vault):
    class Breaks:
        async def stream(self, *args, **kwargs):
            raise RuntimeError("the provider fell over")
            yield {}  # pragma: no cover

    agent = agent_for(config, vault, [])
    agent.provider = Breaks()
    first = await headless.run(config, "anything", agent=agent)

    assert first.outcome == headless.FAILED
    assert "the provider fell over" in first.why

    second = await go(config, vault, [{"text": "fine"}])
    assert second.outcome == headless.COMPLETED, "the next run is not refused"


async def test_an_empty_prompt_does_nothing(config, vault):
    record = await headless.run(config, "   ", agent=agent_for(config, vault, []))

    assert record.outcome == headless.REFUSED
    assert "nothing to do" in record.why


# -- what comes back --------------------------------------------------------


async def test_the_run_record_is_what_jobs_and_continuity_will_read(config, vault):
    """The shape matters more than the code: M2 persists this and reports
    progress from it, and K reads it to answer "where were we"."""
    import json

    record = await go(config, vault, [
        {"tools": [{"name": "account_recall", "input": {"account": "Illes"}}]},
        {"text": "Illes is active."},
    ], invoked_by="a test")

    payload = record.as_dict()
    assert json.loads(json.dumps(payload)) == payload, "it has to survive a round trip"
    assert payload["invoked_by"] == "a test"
    assert payload["outcome"] == "completed"
    assert payload["text"] == "Illes is active."
    assert payload["steps"][0]["name"] == "account_recall"
    assert payload["seconds"] >= 0 and payload["started"] and payload["finished"]
    assert payload["partial"] is False and payload["held"] is False


async def test_events_reach_a_caller_that_wants_progress(config, vault):
    """A job's progress file is written from this. One API, not two."""
    seen: list[str] = []
    await go(config, vault, [
        {"tools": [{"name": "account_recall", "input": {"account": "Illes"}}]},
        {"text": "done"},
    ], on_event=lambda event: seen.append(event.kind))

    assert "tool_called" in seen and "tool_finished" in seen
    assert "turn_complete" in seen


async def test_a_broken_progress_callback_does_not_stop_the_work(config, vault):
    def explode(event):
        raise RuntimeError("the progress file is on a full disk")

    record = await go(config, vault, [{"text": "done"}], on_event=explode)

    assert record.outcome == headless.COMPLETED
    assert record.text == "done"


# -- it is in the log -------------------------------------------------------


async def test_every_headless_turn_is_logged_as_headless(config, vault):
    from ranger.audit import AuditLog

    audit = AuditLog(vault, config.vault.log)
    await go(config, vault, [{"text": "done"}], invoked_by="the morning check",
             audit=audit, prompt="what went quiet")

    written = "\n".join(
        path.read_text(encoding="utf-8") for path in config.vault.log.glob("*.md")
    )
    assert "headless started" in written
    assert "the morning check" in written, "and what invoked it"
    assert "headless completed" in written
    assert "| headless |" in written, "marked as headless, not as a conversation"


async def test_a_bounded_run_is_logged_as_bounded(config, vault):
    from ranger.audit import AuditLog

    audit = AuditLog(vault, config.vault.log)
    script = [{"tools": [{"name": "account_recall", "input": {"account": "Illes"}}]}
              for _ in range(4)] + [{"text": "done"}]
    await go(config, vault, script, max_tools=1, audit=audit)

    written = "\n".join(
        path.read_text(encoding="utf-8") for path in config.vault.log.glob("*.md")
    )
    assert "headless bounded" in written


# -- the terminal -----------------------------------------------------------


def test_the_cli_runs_one_and_prints_what_happened(config, vault, capsys, monkeypatch):
    """The way to exercise this with no browser."""
    from ranger import cli

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(
        "ranger.headless.build_agent",
        lambda cfg, **kwargs: agent_for(config, vault, [
            {"tools": [{"name": "account_recall", "input": {"account": "Illes"}}]},
            {"text": "Illes is active."},
        ]),
    )

    class Args:
        prompt = ["check", "Illes"]
        invoked_by = ""
        max_seconds = 0.0
        max_turns = 0
        max_tools = 0

    assert cli.cmd_run(config, Args()) == 0
    out = capsys.readouterr().out

    assert "running headless: check Illes" in out
    assert "bounds:" in out
    assert "can approve a gate" in out, "the terminal says what it cannot do"
    assert "account_recall" in out, "the tool calls are visible as they happen"
    assert "Illes is active." in out
