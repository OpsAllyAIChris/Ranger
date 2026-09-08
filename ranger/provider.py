"""The model provider seam.

Jarvis talks to Claude through the official Anthropic SDK, but the core only
ever sees this interface. Swapping providers, or faking one in a test, is a
constructor argument and not a rewrite. The model name comes from config and
is never written down in this file.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from .config import ModelConfig


class ProviderError(Exception):
    """The model could not be reached, or refused the request.

    Carries a sentence the operator can act on. The terminal prints it and
    hands back a clean prompt; it never shows a stack trace for a network
    hiccup.
    """

    def __init__(self, message: str, *, retryable: bool = False, cause: Exception | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.cause = cause


@dataclass(frozen=True)
class TextChunk:
    """A piece of assistant text as it streams."""

    text: str


@dataclass(frozen=True)
class ToolRequest:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class Completion:
    """The end of one model response."""

    stop_reason: str | None
    text: str
    tool_requests: tuple[ToolRequest, ...] = ()
    content: list[dict[str, Any]] = field(default_factory=list)
    #: input_tokens, output_tokens, cache_creation_input_tokens and
    #: cache_read_input_tokens. The last one is the only proof caching is
    #: working: if it stays zero across turns, something is invalidating the
    #: prefix.
    usage: dict[str, int] = field(default_factory=dict)


ProviderEvent = TextChunk | Completion


@runtime_checkable
class Provider(Protocol):
    """Streams one model response. Nothing more."""

    async def stream(
        self,
        *,
        system: str | list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        ...


def _classify(exc: Exception, model_name: str) -> ProviderError:
    """Turn an SDK exception into something worth reading out loud."""
    try:
        import anthropic
    except ImportError:  # pragma: no cover - anthropic is a hard dependency
        return ProviderError(f"The model call failed: {exc}", retryable=False, cause=exc)

    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderError(
            "The model took too long to answer. Try again, or raise "
            "model.timeout_seconds in the config.",
            retryable=True,
            cause=exc,
        )
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderError(
            "Could not reach the model. Check the network and try again.",
            retryable=True,
            cause=exc,
        )
    if isinstance(exc, anthropic.RateLimitError):
        return ProviderError(
            "Rate limited by the model provider. Waiting and trying again.",
            retryable=True,
            cause=exc,
        )
    if isinstance(exc, anthropic.AuthenticationError):
        return ProviderError(
            "The API key was rejected. Check ANTHROPIC_API_KEY in .env.",
            retryable=False,
            cause=exc,
        )
    if isinstance(exc, anthropic.PermissionDeniedError):
        return ProviderError(
            f"That key is not allowed to use {model_name}.", retryable=False, cause=exc
        )
    if isinstance(exc, anthropic.NotFoundError):
        return ProviderError(
            f"The provider does not know a model called {model_name}. "
            "Check model.name in the config.",
            retryable=False,
            cause=exc,
        )
    if isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", 0) or 0
        if status >= 500 or status == 429:
            return ProviderError(
                "The model provider is having trouble. Trying again.",
                retryable=True,
                cause=exc,
            )
        return ProviderError(
            f"The model provider refused the request ({status}).",
            retryable=False,
            cause=exc,
        )
    return ProviderError(f"The model call failed: {exc}", retryable=False, cause=exc)


class AnthropicProvider:
    def __init__(self, model: ModelConfig, api_key: str, client: Any | None = None) -> None:
        self.model = model
        if client is None:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(api_key=api_key, timeout=model.timeout_seconds, max_retries=0)
        self.client = client

    async def stream(
        self,
        *,
        system: str | list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        kwargs: dict[str, Any] = {
            "model": self.model.name,
            "max_tokens": self.model.max_tokens,
            "system": system,
            "messages": messages,
        }
        # Current models reject temperature outright. Effort is the lever now,
        # and it lives inside output_config, not at the top level.
        if self.model.effort:
            kwargs["output_config"] = {"effort": self.model.effort}
        if tools:
            kwargs["tools"] = tools

        attempt = 0
        while True:
            spoke = False
            try:
                async for event in self._attempt(kwargs):
                    spoke = spoke or isinstance(event, TextChunk)
                    yield event
                return
            except ProviderError:
                raise
            except Exception as exc:
                error = _classify(exc, self.model.name)
                # Retrying after part of the reply has already streamed would
                # repeat it. Once Jarvis has started talking, a failure is
                # final for this turn.
                if not error.retryable or spoke or attempt >= self.model.max_retries:
                    raise error from exc
                await asyncio.sleep(self.model.retry_backoff_seconds * (2**attempt))
                attempt += 1

    async def _attempt(self, kwargs: dict[str, Any]) -> AsyncIterator[ProviderEvent]:
        async with self.client.messages.stream(**kwargs) as stream:
            async for event in stream:
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    yield TextChunk(event.delta.text)
            final = await stream.get_final_message()

        text_parts: list[str] = []
        tool_requests: list[ToolRequest] = []
        content: list[dict[str, Any]] = []
        for block in final.content:
            if block.type == "text":
                text_parts.append(block.text)
                content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                tool_requests.append(
                    ToolRequest(id=block.id, name=block.name, input=dict(block.input or {}))
                )
                content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": dict(block.input or {}),
                    }
                )
            else:
                # Thinking blocks and anything else the model produced go back
                # unchanged. Adaptive thinking is on by default on current
                # models, and dropping a thinking block breaks the next round
                # of a tool-using turn.
                content.append(_passthrough(block))

        yield Completion(
            stop_reason=final.stop_reason,
            text="".join(text_parts),
            tool_requests=tuple(tool_requests),
            content=content,
            usage={
                "input_tokens": getattr(final.usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(final.usage, "output_tokens", 0) or 0,
                "cache_creation_input_tokens":
                    getattr(final.usage, "cache_creation_input_tokens", 0) or 0,
                "cache_read_input_tokens":
                    getattr(final.usage, "cache_read_input_tokens", 0) or 0,
            },
        )


def _passthrough(block: Any) -> dict[str, Any]:
    """Serialise a content block we do not interpret, losing nothing."""
    dump = getattr(block, "model_dump", None)
    if dump is not None:
        return dump(mode="json", exclude_none=True)
    if isinstance(block, dict):
        return block
    return {"type": getattr(block, "type", "unknown")}


def build_provider(model: ModelConfig, api_key: str) -> Provider:
    if model.provider == "anthropic":
        return AnthropicProvider(model, api_key)
    raise ValueError(
        f"unknown model.provider {model.provider!r}; the only implementation is 'anthropic'"
    )
