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
    assert "used its 2 tool call(s)" in record.why
    assert len(record.steps) == 2, "exactly the allowance, not one more"
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


class Reenters:
    """A provider that starts a second headless run from inside the first.

    **The earliest possible moment**, and a deterministic one: this happens
    during the outer run's very first provider call, with no sleeps and no
    reliance on when the event loop chooses to run anything.
    """

    def __init__(self, config, make_agent, mode: str = "await") -> None:
        self.config = config
        self.make_agent = make_agent
        self.mode = mode
        self.inner: list = []
        self.spawned: list = []
        self.gate = asyncio.Event()

    async def stream(self, *args, **kwargs):
        if self.mode == "await":
            # Directly awaited: shares the outer run's context.
            self.inner.append(
                await headless.run(self.config, "and another", agent=self.make_agent())
            )
        elif self.mode == "concurrent":
            # A task, awaited while the outer run is still live.
            task = asyncio.create_task(
                headless.run(self.config, "and another", agent=self.make_agent())
            )
            self.inner.append(await task)
        else:
            # A task created inside the run that **cannot** run until the outer
            # run has finished, because its first act is to wait on a gate the
            # test opens afterwards. That is the Windows ordering, made
            # deterministic: no sleeps, no hoping the scheduler cooperates.
            #
            # With a module flag the guard is clear by then and it goes ahead.
            # With a context variable the task carries the guard it was created
            # under, whenever it eventually runs.
            async def later():
                await self.gate.wait()
                return await headless.run(
                    self.config, "and another", agent=self.make_agent()
                )

            self.spawned.append(asyncio.create_task(later()))
        yield {"type": "text", "text": "done"}


def reentering(config, vault, mode: str):
    agent = agent_for(config, vault, [])
    agent.provider = Reenters(config, lambda: agent_for(config, vault, [{"text": "no"}]),
                              mode)
    return agent


async def test_a_run_cannot_start_another_from_inside_the_first_call(config, vault):
    """**The earliest possible moment.** Not scheduled, not hoped for: the
    nested call happens inside the outer run's first provider call."""
    agent = reentering(config, vault, "await")
    await headless.run(config, "do a thing", agent=agent)

    inner = agent.provider.inner
    assert inner and inner[0].outcome == headless.REFUSED
    assert "already in progress" in inner[0].why


async def test_a_run_cannot_start_another_concurrently(config, vault):
    """A task, running while the outer run is still live."""
    agent = reentering(config, vault, "concurrent")
    await headless.run(config, "do a thing", agent=agent)

    inner = agent.provider.inner
    assert inner and inner[0].outcome == headless.REFUSED


async def test_work_spawned_inside_a_run_is_refused_even_after_it_finishes(
    config, vault
):
    """**The one that failed on Windows and passed here.**

    A task created inside a run, executed after the run has finished. A module
    flag is clear by then, so it went ahead -- on Linux the scheduler happened
    to interleave it inside the run and it was refused, which is why the suite
    said the guard held. The task now carries the guard it was created under.
    """
    agent = reentering(config, vault, "later")
    outer = await headless.run(config, "do a thing", agent=agent)

    assert outer.outcome == headless.COMPLETED, "the outer run has finished"
    assert not headless.inside(), "and the guard is no longer held out here"

    spawned = agent.provider.spawned
    assert spawned, "a task really was created inside the run"
    assert not spawned[0].done(), "and it has not run yet: it is waiting on the gate"

    agent.provider.gate.set()
    inner = await spawned[0]

    assert inner.outcome == headless.REFUSED, (
        "work spawned by a run is headless work whenever it runs; a guard that "
        "only holds while the run is on the stack is a guard the scheduler owns"
    )


async def test_the_guard_is_visible_to_a_synchronous_callback(config, vault):
    """Callbacks run in the run's own context, so they can see it too. That is
    what stops a sync caller from starting one behind the guard's back."""
    seen: list[bool] = []
    await go(config, vault, [{"text": "done"}],
             on_event=lambda event: seen.append(headless.inside()))

    assert seen and all(seen), "inside the run"
    assert not headless.inside(), "and not outside it"


async def test_two_unrelated_runs_are_not_each_others_problem(config, vault):
    """The guard is about nesting, not about the clock. Two runs started from
    unrelated contexts are not nested, and refusing one of them would be a
    queue pretending to be a safety bound -- which is the distinction M2's job
    queue is going to need."""
    first, second = await asyncio.gather(
        headless.run(config, "one", agent=agent_for(config, vault, [{"text": "a"}])),
        headless.run(config, "two", agent=agent_for(config, vault, [{"text": "b"}])),
    )

    assert first.outcome == headless.COMPLETED
    assert second.outcome == headless.COMPLETED


async def test_the_guard_is_not_a_module_flag(config, vault):
    """Named, because the shape is the bug. A flag answers "is a run in
    progress right now", which is a question about the clock; the guard has to
    answer "was this work started by a run", which is about the caller."""
    import ast

    source = (Path(__file__).resolve().parent.parent / "ranger" / "headless.py")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    globals_assigned = [
        node.names[0] for node in ast.walk(tree) if isinstance(node, ast.Global)
    ]

    assert not globals_assigned, f"headless.py rebinds module state: {globals_assigned}"
    assert "ContextVar" in source.read_text(encoding="utf-8")


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
    assert not headless.inside(), "the guard is released by the failure path too"

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


# -- the audit of the other bounds -----------------------------------------
#
# The nested guard failed on Windows because it was observed rather than
# enforced. These are the rest of them, checked for the same shape.


async def test_the_turn_bound_is_applied_where_the_core_enforces_it(config, vault,
                                                                    monkeypatch):
    """**A bound that could not fire at any setting.**

    It was a check on TurnComplete events, and `turn()` emits exactly one of
    those, at the end. The count never reached two, so `max_turns` was inert at
    every value while sitting in config looking like a limit. It is now applied
    to the agent, where the core counts tool rounds and stops on them.
    """
    from dataclasses import replace

    monkeypatch.setattr("ranger.provider.build_provider", lambda cfg: ScriptedProvider([]))
    tight = replace(config, headless=replace(config.headless, max_turns=2))
    agent = headless.build_agent(tight, vault=vault)

    assert agent.config.model.max_tool_rounds == 2, (
        "the bound has to reach the code that enforces it"
    )


async def test_running_out_of_tool_rounds_is_reported_as_bounded(config, vault):
    """And a run that hit it says so. Reporting "completed" for a turn the core
    cut off would be saying the work finished when it did not."""
    from dataclasses import replace

    script = [{"tools": [{"name": "account_recall", "input": {"account": "Illes"}}]}
              for _ in range(8)]
    tight = replace(config, model=replace(config.model, max_tool_rounds=2))
    agent = agent_for(tight, vault, script)
    record = await headless.run(tight, "check everything", agent=agent, max_tools=50)

    assert record.outcome == headless.BOUNDED
    assert "tool rounds" in record.why
    assert record.steps, "and the work it did is still in the record"


async def test_the_tool_bound_allows_exactly_what_it_says(config, vault):
    """It was `>` after appending, so an allowance of two let three through.
    Not an ordering bug, but a bound that did not do what it said."""
    script = [{"tools": [{"name": "account_recall", "input": {"account": "Illes"}}]}
              for _ in range(6)] + [{"text": "done"}]

    for allowed in (1, 2, 3):
        record = await go(config, vault, list(script), max_tools=allowed)
        assert len(record.steps) == allowed, f"{allowed} allowed, {len(record.steps)} made"


async def test_a_tool_that_never_returns_still_ends_the_run(config, vault, monkeypatch):
    """The wall clock is checked between events, so a tool that hangs is not
    caught by it. The hard backstop is what covers that, and it is structural:
    it cancels rather than waiting to be noticed."""
    monkeypatch.setattr(headless, "HARD_GRACE_SECONDS", 0.05)

    from ranger.tools import Tool, ToolResult

    async def hangs(payload):
        await asyncio.sleep(30)
        return ToolResult(True, "never", "never")  # pragma: no cover

    agent = agent_for(config, vault, [
        {"tools": [{"name": "hangs", "input": {}}]},
        {"text": "done"},
    ])
    agent.registry.register(Tool(name="hangs", description="x" * 90,
                                 input_schema={"type": "object"}, handler=hangs))

    record = await headless.run(config, "do it", agent=agent, max_seconds=0.05)

    assert record.outcome == headless.BOUNDED
    assert "stopped answering" in record.why


def test_no_bound_is_left_to_the_scheduler():
    """The rule, as a test. Every bound is either enforced by code that counts
    (the core's tool rounds), by a structural cancel (the hard backstop), or by
    a value the calling context carries (the nested guard). None of them is a
    module flag read at a moment that happens to be the right one."""
    source = (Path(__file__).resolve().parent.parent / "ranger" / "headless.py")
    text = source.read_text(encoding="utf-8")

    assert "ContextVar" in text
    assert "asyncio.wait_for" in text, "the backstop cancels rather than asking"
    assert "global " not in text, "no module state is rebound at all"
