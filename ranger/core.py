"""The agent core.

Amendment A. This is a library. It has one entry point, Ranger.turn(), which
takes a turn of input and yields events as they happen: state changes, text as
it streams, tool calls it made along the way.

By the end of the build there are four callers of this: the terminal, the
push-to-talk loop, the heartbeat, and the browser. There is no agent logic in
any of them. If you find yourself writing some, stop and put it here.
"""

from __future__ import annotations

import asyncio

from datetime import datetime
from typing import Any, AsyncIterator

from .config import Config
from .gate import ConfirmationRequest, DenyingGate, Gate, ask_with_timeout
from .events import (
    ConfirmationRequested,
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
from .prompts import build_system_blocks, build_system_prompt
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
        gate: Gate | None = None,
        audit: Any = None,
        origin: str = "conversation",
    ) -> None:
        self.config = config
        self.provider = provider
        self.registry = registry or ToolRegistry()
        self.vault = vault or Vault(config.vault)
        self.knowledge_loader = knowledge_loader or KnowledgeLoader(
            self.vault, config.vault, config.knowledge
        )
        #: Refusing is the default. A caller that can actually ask the operator
        #: passes a gate in; nothing consequential runs without one.
        self.gate = gate or DenyingGate()
        self.audit = audit
        self.origin = origin
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

    def _log(self, kind: str, detail: str) -> None:
        if self.audit is not None:
            try:
                self.audit.write(kind, detail, origin=self.origin)
            except Exception:
                pass  # the log must never be able to stop a turn

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

    def system_blocks(self, now: datetime | None = None) -> list[dict]:
        """What actually goes on the wire: two blocks, the big one cached."""
        return build_system_blocks(
            self.config,
            self.knowledge(),
            self.registry,
            now,
            memory=self.memory(),
            cache=self.config.model.cache_prompt,
        )

    # -- the entry point -----------------------------------------------

    async def turn(self, user_input: str) -> AsyncIterator[Event]:
        """Take one turn of input, yield the reply as it streams.

        Every caller in this project goes through here.

        Cancelling the task consuming this is barge-in, and it has to leave a
        transcript the next turn can use. The checkpoint is taken here rather
        than inside, because a turn stopped mid tool round has an assistant
        message holding a tool_use that will never get its tool_result, and the
        API rejects the whole conversation from then on. So an interrupted turn
        rewinds to before it started and is replayed as what was actually said:
        the operator's words, and whatever Jarvis managed to say back.
        """
        checkpoint = list(self.messages)
        said: list[str] = []
        stream = self._run_turn(user_input)
        try:
            async for event in stream:
                if isinstance(event, TextDelta):
                    said.append(event.text)
                yield event
        except asyncio.CancelledError:
            try:
                await stream.aclose()
            except (asyncio.CancelledError, RuntimeError):
                pass
            self._interrupted(checkpoint, user_input, "".join(said))
            raise
        except GeneratorExit:
            # A caller that breaks out of the loop rather than cancelling the
            # task. Same damage to the transcript, same repair. Nothing may be
            # awaited here, and nothing needs to be.
            self._interrupted(checkpoint, user_input, "".join(said))
            raise

    def _interrupted(self, checkpoint: list, user_input: str, said: str) -> None:
        """Put the transcript back into a state the next turn can build on.

        Not a rollback to nothing: the model said part of a reply and the
        operator talked over it, so "no, not that one" has to make sense. What
        is dropped is any half-finished tool round, which is the only part that
        cannot survive.
        """
        self.messages = list(checkpoint)
        text = user_input.strip()
        if text:
            self.messages.append({"role": "user", "content": text})
        if said.strip():
            self.messages.append({"role": "assistant", "content": said.strip()})
        self._trim_history()
        self._state = State.IDLE
        self._log("interrupted", (said.strip() or "(said nothing yet)")[:200])

    async def _run_turn(self, user_input: str) -> AsyncIterator[Event]:
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
        self._log("turn", text[:200])
        self.messages.append({"role": "user", "content": text})
        self._trim_history()

        system = self.system_blocks()
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
                self._log("error", str(error))
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
                    action = tool.describe_action(request.input)
                    ask = ConfirmationRequest(
                        tool=request.name,
                        action=action,
                        payload=request.input,
                        origin=self.origin,
                        token=request.id,
                    )
                    yield StateChanged(State.AWAITING_CONFIRMATION)
                    yield ConfirmationRequested(action=action, detail=request.name, token=request.id)

                    decision = await ask_with_timeout(
                        self.gate, ask, self.config.gate.timeout_seconds
                    )
                    self._log("confirmation", f"{decision.outcome}: {action} ({decision.reason})")

                    if not decision.approved:
                        message = (
                            f"Not done. {decision.reason}. Tell the operator plainly that "
                            "it is waiting on them and do not try another way round it."
                        )
                        yield ToolFinished(request.name, False, decision.outcome)
                        yield Notice(
                            "warn" if decision.held else "alert",
                            f"{decision.outcome}: {action}",
                        )
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
                self._log("tool", f"{request.name} {'ok' if result.ok else 'failed'}: {result.display()}")
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

        reply = "".join(reply_parts)
        # What Jarvis said, not only what it did. Without this a turn where the
        # model chose not to act looks exactly like a turn that broke: the log
        # showed "turn" and then nothing, and there was no way to tell which.
        self._log(
            "reply",
            (reply.strip() or "(said nothing)")[:200]
            + (f"  [used {', '.join(tools_used)}]" if tools_used else "  [no tools]"),
        )
        yield TurnComplete(
            reply=reply,
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
