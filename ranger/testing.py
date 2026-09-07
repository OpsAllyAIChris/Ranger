"""A provider that never calls the network.

Lets the core be verified end to end without an API key and without spending
anything. Tier 1's verification leans on this; every later tier should too.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from .provider import Completion, ProviderEvent, TextChunk, ToolRequest


class ScriptedProvider:
    """Replays a list of scripted responses, one per model round."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def stream(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        self.calls.append({"system": system, "messages": [*messages], "tools": tools})

        if not self.script:
            yield TextChunk("")
            yield Completion(stop_reason="end_turn", text="", content=[])
            return

        step = self.script.pop(0)
        text = step.get("text", "")
        requests = [
            ToolRequest(id=f"toolu_{index}", name=call["name"], input=call.get("input", {}))
            for index, call in enumerate(step.get("tools", []))
        ]

        for chunk in _chunks(text):
            yield TextChunk(chunk)

        content: list[dict[str, Any]] = []
        if text:
            content.append({"type": "text", "text": text})
        for request in requests:
            content.append(
                {
                    "type": "tool_use",
                    "id": request.id,
                    "name": request.name,
                    "input": request.input,
                }
            )

        yield Completion(
            stop_reason="tool_use" if requests else "end_turn",
            text=text,
            tool_requests=tuple(requests),
            content=content,
            usage={"input_tokens": 10, "output_tokens": len(text) // 4},
        )


def _chunks(text: str, size: int = 12) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] if text else []
