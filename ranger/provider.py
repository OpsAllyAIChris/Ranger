"""The model provider seam.

Ranger talks to Claude through the official Anthropic SDK, but the core only
ever sees this interface. Swapping providers, or faking one in a test, is a
constructor argument and not a rewrite. The model name comes from config and
is never written down in this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from .config import ModelConfig


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
    usage: dict[str, int] = field(default_factory=dict)


ProviderEvent = TextChunk | Completion


@runtime_checkable
class Provider(Protocol):
    """Streams one model response. Nothing more."""

    async def stream(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        ...


class AnthropicProvider:
    def __init__(self, model: ModelConfig, api_key: str, client: Any | None = None) -> None:
        self.model = model
        if client is None:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(api_key=api_key)
        self.client = client

    async def stream(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        kwargs: dict[str, Any] = {
            "model": self.model.name,
            "max_tokens": self.model.max_tokens,
            "temperature": self.model.temperature,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

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

        yield Completion(
            stop_reason=final.stop_reason,
            text="".join(text_parts),
            tool_requests=tuple(tool_requests),
            content=content,
            usage={
                "input_tokens": getattr(final.usage, "input_tokens", 0),
                "output_tokens": getattr(final.usage, "output_tokens", 0),
            },
        )


def build_provider(model: ModelConfig, api_key: str) -> Provider:
    if model.provider == "anthropic":
        return AnthropicProvider(model, api_key)
    raise ValueError(
        f"unknown model.provider {model.provider!r}; the only implementation is 'anthropic'"
    )
