"""The tool registry.

Empty in Tier 1 on purpose. Tier 2 registers exactly three tools: account
recall, draft and hold, and what went quiet. Everything after that (CRM
writes, quotes, proposals, transcript ingestion) is another entry in this
registry and nothing else. That is the whole point of the registry existing
before there is anything in it.

Any tool declared with confirm=True routes through the Tier 6 gate. The gate
is not implemented yet; the core refuses to run such a tool until it is, so
the flag cannot be quietly ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: str
    summary: str = ""

    def display(self) -> str:
        return self.summary or self.content[:120]


Handler = Callable[[dict[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    #: Writes something Ranger persists. Always inside Ranger's own folders.
    writes: bool = False
    #: Reaches a human or an outside system. Hard gate, every time, no
    #: blanket approvals and no remembering a previous yes.
    confirm: bool = False

    def as_api_spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self):
        return iter(self._tools.values())

    def api_specs(self) -> list[dict[str, Any]]:
        return [tool.as_api_spec() for tool in self._tools.values()]

    async def run(self, name: str, payload: dict[str, Any]) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult(
                ok=False,
                content=f"No tool named {name!r}. Available: {', '.join(self.names()) or 'none'}.",
                summary="unknown tool",
            )
        try:
            return await tool.handler(payload)
        except Exception as exc:  # a tool failing is a turn outcome, not a crash
            return ToolResult(ok=False, content=f"{type(exc).__name__}: {exc}", summary="failed")
