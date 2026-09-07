"""The agent core.

Amendment A. This is a library. It has one entry point, Ranger.turn(), which
takes a turn of input and yields events as they happen: state changes, text as
it streams, tool calls it made along the way.

By the end of the build there are four callers of this: the terminal, the
push-to-talk loop, the heartbeat, and the browser. There is no agent logic in
any of them. If you find yourself writing some, stop and put it here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, AsyncIterator

from .config import Config
from .events import (
    Event,
    Notice,
    State,
    StateChanged,
    TextDelta,
    ToolCalled,
    ToolFinished,
    TurnComplete,
)
from .knowledge import KnowledgeContext, KnowledgeLoader
from .memory import MemoryContext, load_memory
from .prompts import build_system_prompt
from .provider import Completion, Provider, ProviderError, TextChunk
from .tools import ToolRegistry
from .vault import Vault


class Ranger:
    """One conversation with one operator. No per-user state anywhere."""

    def __init__(
        self,
        config: Config,
        provider: Provider,
        registry: ToolRegistry | None = None,
        vault: Vault | None = None,
        knowledge_loader: KnowledgeLoader | None = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.registry = registry or ToolRegistry()
        self.vault = vault or Vault(config.vault)
        self.knowledge_loader = knowledge_loader or KnowledgeLoader(
            self.vault, config.vault, config.knowledge
        )
        self.messages: list[dict[str, Any]] = []
        self._state = State.IDLE
        self._knowledge: KnowledgeContext | None = None
        self._memory: MemoryContext | None = None

    # -- state ---------------------------------------------------------

    @property
    def state(self) -> State:
        return self._state

    def _set_state(self, state: State) -> StateChanged | None:
        if state is self._state:
            return None
        self._state = state
        return StateChanged(state)

    # -- conversation --------------------------------------------------

    def reset(self) -> None:
        self.messages.clear()
        self._knowledge = None
        self._memory = None
        self._state = State.IDLE

    def _trim_history(self) -> None:
        """Keep the working transcript bounded. Tier 4 catches the spillover."""
        limit = self.config.model.history_turns * 2
        if len(self.messages) <= limit:
            return
        excess = len(self.messages) - limit
        # Never start the transcript on a tool_result block; walk forward to a
        # plain user turn so the message list stays well formed.
        while excess < len(self.messages):
            candidate = self.messages[excess]
            if candidate["role"] == "user" and not _has_tool_result(candidate):
                break
            excess += 1
        self.messages = self.messages[excess:]

    def memory(self, refresh: bool = False) -> MemoryContext:
        """Read fresh, never cached across a restart, so an edit in Obsidian
        takes effect on the next turn."""
        if self._memory is None or refresh:
            self._memory = load_memory(
                self.vault, self.config.vault.memory, self.config.memory.reserve_chars
            )
        return self._memory

    def knowledge(self, refresh: bool = False) -> KnowledgeContext:
        """Whatever standing budget memory did not use.

        Memory is loaded first and keeps its reserve. When the two collide,
        knowledge is the one that loses: it is larger, it is recoverable, and
        it says which files it dropped.
        """
        if self._knowledge is None or refresh:
            remaining = max(0, self.config.context.budget_chars - self.memory(refresh).total_chars)
            self._knowledge = self.knowledge_loader.load(budget=remaining)
        return self._knowledge

    def system_prompt(self, now: datetime | None = None) -> str:
        return build_system_prompt(
            self.config, self.knowledge(), self.registry, now, memory=self.memory()
        )

    # -- the entry point -----------------------------------------------

    async def turn(self, user_input: str) -> AsyncIterator[Event]:
        """Take one turn of input, yield the reply as it streams.

        Every caller in this project goes through here.
        """
        text = user_input.strip()
        if not text:
            yield Notice("info", "empty input, nothing to do")
            return

        memory = self.memory()
        knowledge = self.knowledge()
        for warning in (*memory.warnings, *knowledge.warnings):
            yield Notice("warn", warning)

        # If the model is unreachable this turn never happened, so keep a
        # copy to roll back to. A half-written turn left in the transcript
        # would poison every turn after it.
        checkpoint = list(self.messages)
        self.messages.append({"role": "user", "content": text})
        self._trim_history()

        system = self.system_prompt()
        tools = self.registry.api_specs()
        tools_used: list[str] = []
        reply_parts: list[str] = []
        usage: dict[str, int] = {}
        stop_reason: str | None = None

        for round_index in range(self.config.model.max_tool_rounds):
            changed = self._set_state(State.THINKING)
            if changed:
                yield changed

            completion: Completion | None = None
            spoke = False

            try:
                async for event in self.provider.stream(
                    system=system, messages=self.messages, tools=tools or None
                ):
                    if isinstance(event, TextChunk):
                        if not spoke:
                            spoke = True
                            changed = self._set_state(State.SPEAKING)
                            if changed:
                                yield changed
                        reply_parts.append(event.text)
                        yield TextDelta(event.text)
                    elif isinstance(event, Completion):
                        completion = event
            except ProviderError as error:
                self.messages = checkpoint
                changed = self._set_state(State.IDLE)
                if changed:
                    yield changed
                yield Notice("alert", str(error))
                yield TurnComplete(
                    reply="".join(reply_parts),
                    tools_used=tuple(tools_used),
                    stop_reason="provider_error",
                    usage=usage,
                )
                return

            if completion is None:
                yield Notice("alert", "the model stream ended without completing")
                break

            stop_reason = completion.stop_reason
            for key, value in completion.usage.items():
                usage[key] = usage.get(key, 0) + value

            if not completion.tool_requests:
                # A response with no blocks at all would be an invalid message
                # to send back, so keep the transcript well formed.
                content = completion.content or (
                    [{"type": "text", "text": completion.text}] if completion.text else None
                )
                if content is None:
                    yield Notice("warn", "the model returned an empty response")
                else:
                    self.messages.append({"role": "assistant", "content": content})
                break

            self.messages.append({"role": "assistant", "content": completion.content})

            changed = self._set_state(State.THINKING)
            if changed:
                yield changed

            results: list[dict[str, Any]] = []
            for request in completion.tool_requests:
                yield ToolCalled(request.name, request.input)
                tool = self.registry.get(request.name)

                if tool is not None and tool.confirm:
                    # Tier 6 owns this. Until the gate exists, refuse rather
                    # than let a confirm-flagged tool run unguarded.
                    message = (
                        f"{request.name} needs the operator's confirmation and the "
                        "confirmation gate is not built yet. Refused."
                    )
                    yield ToolFinished(request.name, False, "blocked, no gate yet")
                    yield Notice("alert", message)
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": request.id,
                            "is_error": True,
                            "content": message,
                        }
                    )
                    continue

                result = await self.registry.run(request.name, request.input)
                tools_used.append(request.name)
                yield ToolFinished(request.name, result.ok, result.display())
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": request.id,
                        "is_error": not result.ok,
                        "content": result.content,
                    }
                )

            self.messages.append({"role": "user", "content": results})
            self._trim_history()
        else:
            yield Notice(
                "warn",
                f"stopped after {self.config.model.max_tool_rounds} tool rounds without "
                "a final answer",
            )

        changed = self._set_state(State.IDLE)
        if changed:
            yield changed

        yield TurnComplete(
            reply="".join(reply_parts),
            tools_used=tuple(tools_used),
            stop_reason=stop_reason,
            usage=usage,
        )


def _has_tool_result(message: dict[str, Any]) -> bool:
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    )
