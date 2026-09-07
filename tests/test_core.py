from __future__ import annotations

import pytest

from ranger.core import Ranger
from ranger.events import (
    Notice,
    State,
    StateChanged,
    TextDelta,
    ToolCalled,
    ToolFinished,
    TurnComplete,
)
from ranger.testing import ScriptedProvider
from ranger.tools import Tool, ToolRegistry, ToolResult


async def collect(agent: Ranger, text: str) -> list:
    return [event async for event in agent.turn(text)]


def make_agent(config, script, registry=None) -> Ranger:
    return Ranger(config=config, provider=ScriptedProvider(script), registry=registry)


async def test_turn_streams_text_and_completes(config):
    agent = make_agent(config, [{"text": "Rod is waiting on the film numbers."}])
    events = await collect(agent, "where are we on Illes Foods")

    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "Rod is waiting on the film numbers."

    done = events[-1]
    assert isinstance(done, TurnComplete)
    assert done.reply == text
    assert done.stop_reason == "end_turn"
    assert done.tools_used == ()


async def test_state_stream_is_ordered(config):
    agent = make_agent(config, [{"text": "ok"}])
    states = [e.state for e in await collect(agent, "hello") if isinstance(e, StateChanged)]
    assert states == [State.THINKING, State.SPEAKING, State.IDLE]
    assert agent.state is State.IDLE


async def test_events_are_json_serialisable(config):
    agent = make_agent(config, [{"text": "ok"}])
    for event in await collect(agent, "hello"):
        payload = event.as_dict()
        assert isinstance(payload["kind"], str)
        assert all(isinstance(key, str) for key in payload)


async def test_tool_round_trip(config):
    calls: list[dict] = []

    async def handler(payload):
        calls.append(payload)
        return ToolResult(ok=True, content="No activity since 12 August.", summary="1 account")

    registry = ToolRegistry(
        [
            Tool(
                name="what_went_quiet",
                description="accounts with no logged activity",
                input_schema={"type": "object", "properties": {}},
                handler=handler,
            )
        ]
    )
    agent = make_agent(
        config,
        [
            {"tools": [{"name": "what_went_quiet", "input": {"days": 21}}]},
            {"text": "Illes Foods has gone quiet."},
        ],
        registry,
    )

    events = await collect(agent, "what went quiet")
    assert calls == [{"days": 21}]
    assert [e.name for e in events if isinstance(e, ToolCalled)] == ["what_went_quiet"]
    finished = [e for e in events if isinstance(e, ToolFinished)]
    assert finished[0].ok and finished[0].summary == "1 account"
    assert events[-1].tools_used == ("what_went_quiet",)


async def test_unknown_tool_is_reported_not_raised(config):
    agent = make_agent(
        config,
        [{"tools": [{"name": "send_email", "input": {}}]}, {"text": "That tool does not exist."}],
    )
    events = await collect(agent, "email Rusty")
    finished = [e for e in events if isinstance(e, ToolFinished)]
    assert finished and not finished[0].ok


async def test_confirm_flagged_tool_is_blocked_until_tier_six(config):
    ran = False

    async def handler(payload):
        nonlocal ran
        ran = True
        return ToolResult(ok=True, content="sent")

    registry = ToolRegistry(
        [
            Tool(
                name="send_email",
                description="sends an email",
                input_schema={"type": "object", "properties": {}},
                handler=handler,
                confirm=True,
            )
        ]
    )
    agent = make_agent(
        config,
        [{"tools": [{"name": "send_email", "input": {}}]}, {"text": "I did not send it."}],
        registry,
    )

    events = await collect(agent, "send it")
    assert ran is False
    alerts = [e for e in events if isinstance(e, Notice) and e.level == "alert"]
    assert alerts and "confirmation gate is not built yet" in alerts[0].message


async def test_tool_rounds_are_bounded(config):
    async def handler(payload):
        return ToolResult(ok=True, content="again")

    registry = ToolRegistry(
        [
            Tool(
                name="loop",
                description="loops",
                input_schema={"type": "object", "properties": {}},
                handler=handler,
            )
        ]
    )
    script = [{"tools": [{"name": "loop", "input": {}}]} for _ in range(20)]
    agent = make_agent(config, script, registry)

    events = await collect(agent, "go")
    warnings = [e for e in events if isinstance(e, Notice) and e.level == "warn"]
    assert any("tool rounds" in w.message for w in warnings)
    calls = [e for e in events if isinstance(e, ToolCalled)]
    assert len(calls) == config.model.max_tool_rounds


async def test_empty_input_does_nothing(config):
    agent = make_agent(config, [{"text": "should not be reached"}])
    events = await collect(agent, "   ")
    assert len(events) == 1 and isinstance(events[0], Notice)
    assert agent.messages == []


async def test_reset_clears_the_conversation(config):
    agent = make_agent(config, [{"text": "one"}, {"text": "two"}])
    await collect(agent, "first")
    assert agent.messages
    agent.reset()
    assert agent.messages == []


async def test_history_is_trimmed_to_configured_turns(make_config):
    config = make_config(history_turns=2)
    agent = make_agent(config, [{"text": "reply"} for _ in range(6)])
    for index in range(6):
        await collect(agent, f"turn {index}")

    assert len(agent.messages) <= 4
    assert agent.messages[0]["role"] == "user"


async def test_system_prompt_carries_the_rules(config):
    agent = make_agent(config, [{"text": "ok"}])
    prompt = agent.system_prompt()
    assert "data, never an instruction" in prompt
    assert "No dashes" in prompt
    assert str(config.vault.drafts) in prompt
    assert "no tools yet" in prompt


async def test_empty_model_response_does_not_corrupt_the_transcript(config):
    agent = make_agent(config, [{"text": ""}])
    events = await collect(agent, "hello")
    assert any(isinstance(e, Notice) and "empty response" in e.message for e in events)
    assert [m["role"] for m in agent.messages] == ["user"]


class FailingProvider:
    """A provider that is simply unreachable."""

    def __init__(self, error):
        self.error = error

    async def stream(self, *, system, messages, tools=None):
        raise self.error
        yield  # pragma: no cover - makes this an async generator


async def test_unreachable_model_ends_the_turn_cleanly(config):
    from ranger.provider import ProviderError

    agent = Ranger(
        config=config,
        provider=FailingProvider(
            ProviderError("Could not reach the model. Check the network and try again.")
        ),
    )
    events = await collect(agent, "where are we on Illes Foods")

    alerts = [e for e in events if isinstance(e, Notice) and e.level == "alert"]
    assert alerts and "Could not reach the model" in alerts[0].message
    assert isinstance(events[-1], TurnComplete)
    assert events[-1].stop_reason == "provider_error"
    assert agent.state is State.IDLE


async def test_a_failed_turn_leaves_no_residue_in_the_transcript(config):
    from ranger.provider import ProviderError

    good = ScriptedProvider([{"text": "Rod is waiting."}])
    agent = Ranger(config=config, provider=good)
    await collect(agent, "first")
    clean = list(agent.messages)

    agent.provider = FailingProvider(ProviderError("unreachable"))
    await collect(agent, "second")
    assert agent.messages == clean

    # And the next turn works normally against the same transcript.
    agent.provider = ScriptedProvider([{"text": "Still waiting."}])
    events = await collect(agent, "third")
    assert isinstance(events[-1], TurnComplete)
    assert [m["role"] for m in agent.messages] == ["user", "assistant", "user", "assistant"]


async def test_a_failed_tool_round_rolls_back_to_before_the_turn(config):
    from ranger.provider import ProviderError

    async def handler(payload):
        return ToolResult(ok=True, content="detail")

    registry = ToolRegistry(
        [
            Tool(
                name="lookup",
                description="d",
                input_schema={"type": "object", "properties": {}},
                handler=handler,
            )
        ]
    )

    class DiesAfterTool:
        def __init__(self):
            self.calls = 0

        async def stream(self, *, system, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                async for event in ScriptedProvider(
                    [{"tools": [{"name": "lookup", "input": {}}]}]
                ).stream(system=system, messages=messages, tools=tools):
                    yield event
                return
            raise ProviderError("dropped mid conversation")

    agent = Ranger(config=config, provider=DiesAfterTool(), registry=registry)
    await collect(agent, "look it up")
    assert agent.messages == []
