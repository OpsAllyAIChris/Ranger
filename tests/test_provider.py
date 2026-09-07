"""The Anthropic adapter, exercised against a fake client.

Verifies that streamed text and tool_use blocks come out of the seam in the
shape the core expects, without touching the network.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ranger.provider import AnthropicProvider, Completion, TextChunk, build_provider


class FakeStream:
    def __init__(self, events, final):
        self._events = events
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        for event in self._events:
            yield event

    async def get_final_message(self):
        return self._final


class FakeMessages:
    def __init__(self, stream):
        self._stream = stream
        self.kwargs = None

    def stream(self, **kwargs):
        self.kwargs = kwargs
        return self._stream


def delta(text):
    return SimpleNamespace(
        type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=text)
    )


async def test_streams_text_then_completes(config):
    final = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="Rod is waiting.")],
        usage=SimpleNamespace(input_tokens=11, output_tokens=4),
    )
    messages = FakeMessages(FakeStream([delta("Rod is "), delta("waiting.")], final))
    provider = AnthropicProvider(config.model, "key", client=SimpleNamespace(messages=messages))

    events = [e async for e in provider.stream(system="s", messages=[], tools=None)]
    assert [e.text for e in events if isinstance(e, TextChunk)] == ["Rod is ", "waiting."]

    completion = events[-1]
    assert isinstance(completion, Completion)
    assert completion.text == "Rod is waiting."
    assert completion.usage == {"input_tokens": 11, "output_tokens": 4}
    assert messages.kwargs["model"] == config.model.name
    assert "tools" not in messages.kwargs


async def test_tool_use_blocks_become_tool_requests(config):
    final = SimpleNamespace(
        stop_reason="tool_use",
        content=[
            SimpleNamespace(type="text", text="Checking."),
            SimpleNamespace(
                type="tool_use", id="toolu_1", name="account_recall", input={"name": "Illes"}
            ),
        ],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    messages = FakeMessages(FakeStream([delta("Checking.")], final))
    provider = AnthropicProvider(config.model, "key", client=SimpleNamespace(messages=messages))

    completion = [
        e
        async for e in provider.stream(
            system="s", messages=[], tools=[{"name": "account_recall"}]
        )
    ][-1]

    assert completion.stop_reason == "tool_use"
    assert len(completion.tool_requests) == 1
    request = completion.tool_requests[0]
    assert (request.id, request.name, request.input) == ("toolu_1", "account_recall", {"name": "Illes"})
    assert completion.content[1]["type"] == "tool_use"
    assert messages.kwargs["tools"] == [{"name": "account_recall"}]


def test_unknown_provider_is_rejected(config):
    from dataclasses import replace

    with pytest.raises(ValueError, match="unknown model.provider"):
        build_provider(replace(config.model, provider="openai"), "key")
