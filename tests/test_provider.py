"""The Anthropic adapter, exercised against a fake client.

Verifies that streamed text and tool_use blocks come out of the seam in the
shape the core expects, without touching the network.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ranger.provider import (
    AnthropicProvider,
    Completion,
    ProviderError,
    TextChunk,
    build_provider,
)


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


class FlakyMessages:
    """Fails a set number of times before succeeding."""

    def __init__(self, error, failures, stream=None):
        self.error = error
        self.failures = failures
        self.attempts = 0
        self._stream = stream

    def stream(self, **kwargs):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise self.error
        return self._stream


def ok_stream(text="done"):
    final = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    return FakeStream([delta(text)], final)


def fake_response(status):
    return SimpleNamespace(status_code=status, headers={}, request=SimpleNamespace())


def provider_for(config, messages, **overrides):
    from dataclasses import replace

    model = replace(config.model, retry_backoff_seconds=0.0, **overrides)
    return AnthropicProvider(model, "key", client=SimpleNamespace(messages=messages))


async def test_transient_failure_is_retried(config):
    import anthropic

    error = anthropic.APIConnectionError(request=SimpleNamespace())
    messages = FlakyMessages(error, failures=2, stream=ok_stream())
    provider = provider_for(config, messages, max_retries=3)

    events = [e async for e in provider.stream(system="s", messages=[], tools=None)]
    assert messages.attempts == 3
    assert isinstance(events[-1], Completion)


async def test_retries_give_up_with_a_readable_message(config):
    import anthropic

    error = anthropic.APIConnectionError(request=SimpleNamespace())
    messages = FlakyMessages(error, failures=99, stream=ok_stream())
    provider = provider_for(config, messages, max_retries=2)

    with pytest.raises(ProviderError) as caught:
        [e async for e in provider.stream(system="s", messages=[], tools=None)]

    assert messages.attempts == 3
    assert "Could not reach the model" in str(caught.value)
    assert caught.value.retryable is True


async def test_a_rejected_key_is_not_retried(config):
    import anthropic

    error = anthropic.AuthenticationError("bad key", response=fake_response(401), body=None)
    messages = FlakyMessages(error, failures=99, stream=ok_stream())
    provider = provider_for(config, messages, max_retries=5)

    with pytest.raises(ProviderError) as caught:
        [e async for e in provider.stream(system="s", messages=[], tools=None)]

    assert messages.attempts == 1
    assert "ANTHROPIC_API_KEY" in str(caught.value)


async def test_an_unknown_model_name_says_so(config):
    import anthropic

    error = anthropic.NotFoundError("no model", response=fake_response(404), body=None)
    provider = provider_for(config, FlakyMessages(error, failures=99, stream=ok_stream()))

    with pytest.raises(ProviderError) as caught:
        [e async for e in provider.stream(system="s", messages=[], tools=None)]

    assert config.model.name in str(caught.value)
    assert "model.name in the config" in str(caught.value)


async def test_effort_is_sent_inside_output_config_not_as_temperature(config):
    messages = FakeMessages(ok_stream())
    provider = provider_for(config, messages)

    [e async for e in provider.stream(system="s", messages=[], tools=None)]

    assert messages.kwargs["output_config"] == {"effort": config.model.effort}
    # Current models reject temperature outright.
    assert "temperature" not in messages.kwargs
    assert "top_p" not in messages.kwargs


async def test_effort_is_omitted_when_left_unset(config):
    messages = FakeMessages(ok_stream())
    provider = provider_for(config, messages, effort="")

    [e async for e in provider.stream(system="s", messages=[], tools=None)]
    assert "output_config" not in messages.kwargs


async def test_thinking_blocks_are_passed_back_unchanged(config):
    class Block:
        """Stands in for an SDK content block."""

        def __init__(self, payload):
            self.type = payload["type"]
            self._payload = payload

        def model_dump(self, **kwargs):
            return dict(self._payload)

    thinking = {"type": "thinking", "thinking": "weighing it up", "signature": "sig-abc"}
    final = SimpleNamespace(
        stop_reason="tool_use",
        content=[
            Block(thinking),
            SimpleNamespace(type="tool_use", id="toolu_1", name="lookup", input={}),
        ],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    provider = provider_for(config, FakeMessages(FakeStream([], final)))

    completion = [
        e async for e in provider.stream(system="s", messages=[], tools=[{"name": "lookup"}])
    ][-1]

    # Dropping the thinking block would break the next round of the turn.
    assert completion.content[0] == thinking
    assert completion.content[1]["type"] == "tool_use"
