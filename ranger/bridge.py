"""The browser as the fourth caller of the core.

Amendment A, exactly: the browser sends a turn to the same `Ranger.turn()` the
terminal calls, and receives the same events the terminal prints. Nothing here
decides anything. It reads JSON off a socket, calls the core, and writes the
core's own events back as JSON. Every event was built JSON-serialisable in
Tier 1 for this moment, so this is a transport and not a refactor.

One connection is one conversation. Two browser tabs are two transcripts, the
same way two terminals would be, and they share the vault and the log.

Reading and running are concurrent, and that is not an optimisation. The gate
asks the browser and waits for the answer, so if the read loop stopped while a
turn was in flight the answer could never arrive and every confirmation would
time out. A turn runs as a task; the socket keeps being read the whole time.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Config
from .core import Ranger
from .events import State
from .gate import Gate, SocketGate

TURN = "turn"
AUDIO = "audio"
DECISION = "decision"
PANEL = "panel"
DISMISS = "dismiss"
STOP = "stop"
ARM = "arm"
DISARM = "disarm"
#: The browser's speaker queue drained, having played up to `index`. Half of
#: the conversation window's anchor; the other half is the turn completing.
SPOKEN = "spoken"
#: A click on the × beside a draft. Runs the same clear_draft tool the model
#: calls, so the browser holds no idea of what clearing means.
CLEAR_DRAFT = "clear_draft"
#: The page became visible or hidden. The window will not open behind a
#: minimised window, because the only sign the microphone is live is on it.
VISIBLE = "visible"
#: Open the preview for a document already in the drafts folder. Sent when the
#: operator clicks one in the panel, which is a reopen and therefore never
#: replays the assembly animation.
PREVIEW = "preview"
#: Show a generated document in the operator's file manager. The other half of
#: getting the file out; the download link is the half that always works.
REVEAL = "reveal"
#: A dropped file landed (over the POST route) and the browser is telling the
#: server to look at it. Nothing is parsed on this: it checks whether the
#: headers are a shape the operator has already mapped, which is Python reading
#: one row, and imports silently if they are.
DROPPED = "dropped"
#: "Propose a mapping for this file." Sent by the import button on a panel row,
#: never automatically: an accidental drop does nothing.
IMPORT_PROPOSE = "import_propose"
#: The operator confirmed a mapping at the keyboard. This is the only way a new
#: shape is ever learned.
IMPORT_APPLY = "import_apply"
#: Export the analysis on screen as a document. A format change of the file
#: that is already being looked at, never a second computation.
ANALYSIS_EXPORT = "analysis_export"
#: A gross profit figure typed into the panel. The operator's own keystrokes,
#: not an agent action: the model is not in this path and must not be. See
#: dashlets.py for why the whole command centre works this way.
GP_ENTRY = "gp_entry"

#: How many tool calls the panel remembers. Enough to see what just happened,
#: not a second audit log: the real one is in the vault and is append only.
RECENT_TOOLS = 8


@dataclass
class Session:
    """One websocket connection, one conversation."""

    agent: Ranger
    send: Callable[[dict[str, Any]], None]
    busy: bool = False
    tools: list[dict[str, Any]] = field(default_factory=list)
    #: The sentences sent to the browser this turn, by index. Kept so an
    #: interruption can say what nobody heard rather than only that it happened.
    speech: list[str] = field(default_factory=list)
    #: The highest index the browser has reported playing.
    spoken_to: int = -1
    #: Watches for the operator talking over a reply.
    bargein: Any = None
    #: Set when the connection has ears and a mouth. None means the browser can
    #: still type: voice is an addition to this socket, never a replacement.
    transcriber: Any = None
    speaker: Any = None
    keyterms: bool = True
    #: Hands free. None until armed, and never armed by anything but an
    #: explicit message from the operator: condition one is that it is opted
    #: into per session and never persisted on, so there is no path where a
    #: restart comes back listening.
    listener: Any = None
    #: Conversation mode. Created when hands free arms and thrown away when it
    #: disarms: there is no window without a hotword to refill its budget.
    window: Any = None
    _turn: asyncio.Task | None = None
    _window_timer: asyncio.TimerHandle | None = None

    def __post_init__(self) -> None:
        """Put the card gate's outbound messages through `watch`.

        Done here rather than at assembly so the guarantee does not depend on
        how the session was built. A card opening must close the conversation
        window whether this session came from `build_session`, from a test, or
        from whatever calls it next.
        """
        gate = getattr(self.agent, "gate", None)
        if isinstance(gate, SocketGate):
            gate.emit = self.watch

    # -- outbound ------------------------------------------------------

    def emit(self, kind: str, **fields: Any) -> None:
        self.send({"kind": kind, **fields})

    def watch(self, payload: dict[str, Any]) -> None:
        """Everything the gate emits passes through here on its way out.

        A card opening closes the conversation window and spends the budget.
        Not because a spoken yes could ever reach the gate -- it cannot, the
        gate takes a token and a click and nothing else -- but because a window
        held open under a card is an open microphone next to a decision, and a
        follow-up arriving with no phrase in front of it reads exactly like an
        answer. The safest version of that temptation is one that never occurs.
        """
        if payload.get("kind") == "confirm_open":
            from .conversation import Why

            self._close_window(Why.GATED, "a confirmation card opened")
        self.send(payload)

    def hello(self) -> None:
        """What the front end needs before it can render anything."""
        registry = self.agent.registry
        self.emit(
            "hello",
            model=self.agent.config.model.name,
            origin=self.agent.origin,
            gate=self.agent.gate.name,
            tools=registry.names() if registry else [],
            gated=[tool.name for tool in (registry or []) if tool.confirm],
            vault=str(self.agent.config.vault.root),
            # The front end shows a mic only if there is something behind it.
            voice=self.transcriber is not None,
            speech=self.speaker is not None,
            hands_free=self._hands_free_state(),
            # So an oversized drop is refused with a sentence in the window
            # rather than by pushing 400MB through a socket to be told no.
            drop_max_bytes=self.agent.config.imports.max_bytes,
        )
        self.emit("state", state=State.IDLE.value)
        self.push_panel()

    def push_panel(self) -> None:
        """The vault, as the panel draws it. Read fresh, never cached."""
        from .panel import snapshot

        try:
            view = snapshot(self.agent.config, self.agent.vault)
        except Exception as exc:  # a panel that cannot read must not kill a turn
            self.emit("error", message=f"could not read the vault: {exc}")
            return
        view["tools"] = list(self.tools)
        self.emit("panel", **view)

    # -- inbound -------------------------------------------------------

    async def handle(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.emit("error", message=f"that was not JSON: {exc}")
            return
        if not isinstance(message, dict):
            self.emit("error", message="expected an object")
            return

        kind = message.get("type")
        if kind == TURN:
            return self._start_turn(message)
        if kind == AUDIO:
            return self._start_audio(message)
        if kind == DECISION:
            return self._decide(message)
        if kind == PANEL:
            return self.push_panel()
        if kind == DISMISS:
            return self._dismiss(message)
        if kind == ARM:
            return self._arm()
        if kind == DISARM:
            return self._disarm()
        if kind == CLEAR_DRAFT:
            return await self._clear_draft(message)
        if kind == SPOKEN:
            return self._played(message)
        if kind == VISIBLE:
            return self._visibility(message)
        if kind == GP_ENTRY:
            return self._gp_entry(message)
        if kind == PREVIEW:
            return self._preview(message)
        if kind == REVEAL:
            return self._reveal(message)
        if kind == DROPPED:
            return self._dropped(message)
        if kind == IMPORT_PROPOSE:
            return self._import_propose(message)
        if kind == IMPORT_APPLY:
            return self._import_apply(message)
        if kind == ANALYSIS_EXPORT:
            return self._analysis_export(message)
        if kind == STOP:
            if not self.stop():
                self.emit("error", message="nothing was running")
            return
        self.emit("error", message=f"unknown message type {kind!r}")

    def _start_turn(self, message: dict[str, Any]) -> None:
        text = str(message.get("text", "")).strip()
        if not text:
            self.emit("error", message="an empty turn has nothing to answer")
            return

        # Typing is a different way of answering, so the microphone stops
        # waiting. A hard close: the budget is spent rather than carried.
        from .conversation import Why

        self._close_window(Why.TYPED, "answered at the keyboard")

        if self.busy:
            if not bool(message.get("interrupt", False)):
                self.emit("error", message="still working on the last one")
                return
            # Barge-in. The operator is talking over Jarvis, so what Jarvis was
            # saying stops mattering. The core keeps what it managed to say, so
            # "no, not that one" still has something to refer to.
            self.stop("interrupted")

        self.busy = True
        self._turn = asyncio.create_task(self._run(text))

    def _start_audio(self, message: dict[str, Any]) -> None:
        """A recording arrived. Transcribe it, then take it as a turn."""
        from .listen import AudioRejected, decode_audio

        if self.transcriber is None:
            self.emit("error", message="this connection has no transcription wired")
            return
        try:
            utterance = decode_audio(message)
        except AudioRejected as exc:
            self.emit("error", message=str(exc))
            return

        if self.busy:
            if not bool(message.get("interrupt", False)):
                self.emit("error", message="still working on the last one")
                return
            self.stop("interrupted")

        self.busy = True
        self._turn = asyncio.create_task(self._run_audio(utterance))

    def _surface_window(self, why: str) -> None:
        """Bring the interface forward on a firing, and log which of the three
        outcomes actually happened.

        Caller-side, like everything about hands free. Nothing here enters
        `Ranger.turn()`.
        """
        wake = self.agent.config.wake
        if not wake.surface_on_wake:
            return
        from .desktop import focus_window

        topmost = wake.surface_topmost
        if topmost and wake.surface_topmost_never_in_call:
            # HWND_TOPMOST puts Jarvis above every ordinary window, and a
            # screen-share of a whole monitor captures the desktop as composed
            # -- so a forced window lands in what the customer is looking at.
            # The microphone check already knows whether something else has the
            # device, which is the closest thing to "am I in a call" that exists
            # without asking Teams. Fall back to the flash rather than risk it.
            from .micuse import may_arm

            try:
                verdict = may_arm(ignore=("chrome.exe",))
                if not verdict.allowed:
                    topmost = False
                    self._log_wake(
                        "surface not topmost",
                        f"something else has the microphone ({verdict.reason}), "
                        "so the window was not forced over what may be a share",
                    )
            except Exception:
                # A check that cannot answer is a check that failed. Do not
                # force a window over an unknown screen state.
                topmost = False

        try:
            result = focus_window(topmost=topmost)
        except Exception as exc:  # surfacing must never be why a turn fails
            self._log_wake("surface failed", f"{type(exc).__name__}: {exc}")
            return
        # A flash is not focus. Logged as what it was, so a week of these says
        # what Windows actually does on this machine.
        self._log_wake(f"surface {result.outcome}", f"{why}; {result.detail}")

    def _dismissed(self, text: str) -> bool:
        """"That's all Jarvis": put the window away, stay armed.

        A whole-utterance rule on a completed transcript, not a second hotword.
        It costs no tokens, never reaches the model, and never touches `State`:
        the microphone stays on, because a phrase that changed the safety state
        is exactly what the operator did not want.
        """
        from .wake import is_dismissal

        if not is_dismissal(text, self.agent.config.wake.phrase):
            return False

        from .conversation import Why

        # It closes an open window and spends nothing. The wake word still
        # refills the budget, as before.
        if self.window is not None and self.window.open:
            self.window.close(Why.DISMISSED, text.strip()[:60])
            self._flush_window("")
            self._emit_window()
        self._cancel_window_timer()

        # The ctypes path, not a browser blur. window.blur() is ignored in
        # Chrome's app mode -- confirmed on the operator's machine over several
        # attempts -- and ShowWindow(SW_MINIMIZE) is not foreground-gated, so it
        # does not hit the refusal that makes surfacing degrade to a flash.
        from .desktop import minimise_window

        try:
            result = minimise_window()
        except Exception as exc:  # putting a window away must not fail a turn
            result = None
            self._log_wake("dismissed", f"{text.strip()[:80]!r}; minimise raised {exc}")

        if result is not None:
            # What happened, not what was attempted. Same rule as surfacing.
            self._log_wake(
                f"dismissed {result.outcome}",
                f"{text.strip()[:80]!r}; {result.detail}; still armed",
            )

        # Nothing here touches the hotword or the socket. Minimising is a thing
        # that happens to a window; the microphone stays as armed as it was.
        self.emit("dismissed_aloud", text=text.strip(),
                  outcome=result.outcome if result is not None else "failed")
        self.emit("notice", level="info", message="minimised. Still listening for the phrase.")
        return True

    async def _run_audio(self, utterance: Any, *, strip: str = "") -> None:
        from .events import State
        from .listen import transcribe

        try:
            self.emit("state", state=State.LISTENING.value)
            heard = await transcribe(self.transcriber, utterance, hints=self.keyterms)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.emit("error", message=f"could not transcribe that: {exc}")
            self._finish()
            return

        text = (heard.text or "").strip()
        if strip:
            # The wake phrase is taken off as text, never cut out of the audio:
            # the audio boundary is a guess and guessing it wrong eats the
            # first word of the request.
            from .wake import strip_phrase

            text = strip_phrase(text, strip)
        # Always shown, even when empty. "Heard nothing in that" with the
        # numbers is the message that stopped three silent turns in Tier 3
        # from looking like a broken microphone.
        self.emit(
            "heard",
            text=text,
            confidence=round(getattr(heard, "confidence", 0.0) or 0.0, 3),
            seconds=round(getattr(utterance, "seconds", 0) or 0, 2),
            shaky=[word.text for word in getattr(heard, "shaky_words", ())],
        )
        if not text:
            self.emit("error", message="heard nothing in that")
            self._finish()
            return

        # Checked before the turn, so a dismissal costs nothing and never
        # enters the conversation history.
        if self._dismissed(text):
            self._finish()
            return

        await self._run(text)

    def stop(self, why: str = "stopped") -> bool:
        """Cut the turn in flight. False if there was nothing to cut."""
        if self._turn is None or self._turn.done():
            return False
        self._turn.cancel()
        self._turn = None
        self.busy = False
        self.emit("stopped", reason=why)
        self.emit("state", state=State.IDLE.value)
        return True

    # -- hands free ----------------------------------------------------

    def _hands_free_state(self) -> dict[str, Any]:
        """What the interface needs to draw the control, or to hide it."""
        from .wake import available

        wake = self.agent.config.wake
        ready, why = available()
        return {
            "offered": bool(wake.enabled) and self.transcriber is not None,
            "ready": ready,
            "reason": why,
            "phrase": wake.phrase,
            "armed": bool(self.listener and self.listener.hotword.armed),
            "idle_minutes": wake.idle_disarm_minutes,
        }

    def _arm(self) -> None:
        """Turn hands free on for this session only."""
        from .handsfree import Listener, microphone_frames
        from .micuse import may_arm
        from .wake import WakeUnavailable, build_hotword

        wake = self.agent.config.wake
        if not wake.enabled:
            self.emit("error", message="wake.enabled is false, so hands free is not offered")
            return
        if self.transcriber is None:
            self.emit("error", message="there is no transcription wired, so hands free would be deaf")
            return
        if self.listener is not None and self.listener.hotword.armed:
            self.emit("error", message="hands free is already on")
            return

        try:
            hotword = build_hotword(self.agent.config, check_microphone=may_arm)
        except WakeUnavailable as exc:
            self.emit("error", message=str(exc))
            return

        ok, why = hotword.arm()
        self._log_wake("armed" if ok else "refused", why)
        if not ok:
            self.emit("error", message=f"hands free did not start: {why}")
            self.emit("hands_free", **self._hands_free_state())
            return

        loop = asyncio.get_running_loop()

        def utterance(pcm: bytes, follow_up: bool = False) -> None:
            loop.call_soon_threadsafe(self._heard_hands_free, pcm, follow_up)

        def fired(fire) -> None:
            loop.call_soon_threadsafe(self._log_fire, fire)

        def state(value) -> None:
            loop.call_soon_threadsafe(
                lambda: self.emit("hands_free", **self._hands_free_state(), listening=value.value)
            )

        def level(value: float) -> None:
            loop.call_soon_threadsafe(lambda: self.emit("level", value=round(value, 3)))

        from .conversation import build_window

        self.window = build_window(
            self.agent.config,
            check_microphone=lambda: may_arm(ignore=("chrome.exe",)).allowed,
        )

        from .bargein import Detector

        self.bargein = Detector(
            margin=wake.bargein_margin,
            sustain_seconds=wake.bargein_sustain_seconds,
            tail_seconds=wake.bargein_tail_seconds,
            room_margin=wake.bargein_room_margin,
            room_seconds=wake.bargein_room_seconds,
        ) if wake.bargein else None

        def interrupted() -> None:
            loop.call_soon_threadsafe(self._interrupted)

        self.listener = Listener(
            hotword=hotword,
            frames=microphone_frames,
            on_utterance=utterance,
            on_fire=fired,
            on_state=state,
            on_level=level,
            check_seconds=wake.mic_check_seconds,
            bargein=self.bargein,
            on_bargein=interrupted,
        )
        self.listener.start()
        self.emit("hands_free", **self._hands_free_state())
        self.emit("notice", level="info", message=why)

    def _disarm(self, why: str = "stopped at the keyboard") -> None:
        if self.listener is None:
            self.emit("error", message="hands free is not on")
            return
        from .conversation import Why as WindowWhy

        self._close_window(WindowWhy.DISARMED, why)
        self.window = None
        self._cancel_window_timer()
        self.listener.hotword.disarm(why)
        self.listener.stop()
        self.listener = None
        self._log_wake("disarmed", why)
        self.emit("hands_free", **self._hands_free_state())
        self._emit_window()

    def _heard_hands_free(self, pcm: bytes, follow_up: bool = False) -> None:
        """An utterance captured by the hotword, taken as a turn.

        `follow_up` means it came from a conversation window rather than from
        the phrase: there is nothing on the front to strip, and it must not
        refill the reopen budget.
        """
        from .listen import Utterance
        from .wake import wav_of

        if not follow_up:
            self._woke()
        self._cancel_window_timer()

        if self.busy:
            self.stop("interrupted")
        self.busy = True
        self._turn = asyncio.create_task(
            self._run_audio(Utterance(audio=wav_of(pcm), mime="audio/wav",
                                      seconds=round(len(pcm) / 2 / 16000, 2)),
                            strip="" if follow_up else self.agent.config.wake.phrase)
        )

    def _woke(self) -> None:
        """The phrase fired. The only thing that refills the reopen budget.

        Not elapsed time. A budget that refilled after a quiet period would not
        be a budget: a room with a fan would refill it forever.
        """
        if self.window is not None:
            self.window.woke()
        self._surface_window("the wake phrase fired")

    def _log_fire(self, fire) -> None:
        """Every firing, including the discarded ones.

        The operator asked for this so false fires can be counted over a week
        rather than guessed at, and so a fire during a call it should have
        disarmed for is visible rather than invisible.
        """
        self._log_wake(f"fired {fire.outcome.value}", fire.describe(self.agent.config.wake.phrase))
        self.emit(
            "wake_fire",
            outcome=fire.outcome.value,
            confidence=round(fire.confidence, 3),
            seconds=round(fire.seconds, 2),
            blockers=list(fire.blockers),
            detail=fire.describe(self.agent.config.wake.phrase),
        )
        if fire.outcome.value == "blocked":
            self.listener = None
            self.emit("hands_free", **self._hands_free_state())

    def _log_wake(self, kind: str, detail: str) -> None:
        audit = getattr(self.agent, "audit", None)
        if audit is None:
            return
        try:
            audit.write(f"hands-free {kind}", detail, origin=self.agent.origin)
        except Exception:
            pass

    # -- conversation mode ---------------------------------------------
    #
    # The window that stays open after Jarvis stops talking. All of it lives
    # here, in the caller: nothing about it reaches Ranger.turn(), which does
    # not know whether the words it was handed came from a keyboard, a phrase
    # or a follow-up, and should not.

    def _played(self, message: dict[str, Any]) -> None:
        """The browser's speaker queue drained.

        This arrives several times in an ordinary reply, because sentences are
        spoken as they are produced and the queue empties whenever the model is
        slower than the voice. It is half an anchor, never a trigger.
        """
        if self.window is None:
            return
        try:
            index = int(message.get("index", -1))
        except (TypeError, ValueError):
            return
        self.window.played(index)
        self.spoken_to = max(self.spoken_to, index)
        if self.bargein is not None:
            # Drained, and it is the browser saying so rather than this end
            # guessing at how long the audio was. Between sentences this
            # happens constantly; the detector's tail is what tells that from
            # the end of the reply.
            self.bargein.speaking(False)
        self._maybe_open()

    def _interrupted(self) -> None:
        """The operator talked over the reply. **Stop the speech, not the turn.**

        The turn is left running deliberately. By the time a sentence is being
        spoken the tools for that turn have already run or are running, and
        cancelling half way could leave a note filed with no reply to say so --
        a half-spoken answer that still changed something is harder to recover
        from than one that finishes into a room where nobody is listening. What
        is dropped is the audio, which is the thing actually in the way.

        Conversation mode's rule holds: barge-in does not close the window. The
        operator interrupting is the most engaged they get.
        """
        from .bargein import Interruption

        if self.bargein is None:
            return
        # Drain the browser's queue now, so nothing trailing is played.
        self.emit("stop_speaking", reason="you started talking")
        self.bargein.speaking(False)

        sent = len(self.speech)
        unspoken = [text for text in self.speech[self.spoken_to + 1:] if text]
        record = Interruption(
            spoken=self.spoken_to,
            sent=sent,
            unspoken_sentences=len(unspoken),
            unspoken_chars=sum(len(text) for text in unspoken),
        )
        self.emit("notice", level="info", message="stopped talking; go ahead")
        audit = getattr(self.agent, "audit", None)
        if audit is not None:
            try:
                audit.write("speech interrupted", record.describe(), origin="voice")
            except Exception:
                pass

    def _visibility(self, message: dict[str, Any]) -> None:
        if self.window is None:
            return
        self.window.sees(bool(message.get("visible", True)))
        self._flush_window("the interface went off screen")

    def _maybe_open(self) -> None:
        """Open the window if the turn is done and the speaking has stopped."""
        if self.window is None or self.listener is None:
            return
        opened, why = self.window.opens(self._clock())
        if why == "not ready":
            return
        self._flush_window(why)
        if opened:
            started, why_not = self.listener.hotword.listen(self.window.seconds)
            if not started:
                # Say which. "The hotword would not listen" covered both device
                # contention and the state machine being mid-utterance, and
                # those have different fixes.
                from .conversation import Why

                self.window.close(Why.MIC_CHECK, why_not)
                self._flush_window(why_not)
                self._emit_window()
                self._log_wake("window refused", why_not)
                return
            self._arm_window_timer()
        self._emit_window()

    def _close_window(self, why, detail: str = "") -> None:
        if self.window is None:
            return
        if self.window.close(why, detail):
            self._cancel_window_timer()
            self._flush_window(detail)
            self._emit_window()

    def _emit_window(self) -> None:
        """What the interface draws the draining ring from."""
        window = self.window
        if window is None:
            self.emit("window", open=False, seconds=0.0, used=0, of=0)
            return
        self.emit(
            "window",
            open=window.open,
            seconds=window.seconds,
            used=window.used,
            of=window.reopens,
        )

    def _arm_window_timer(self) -> None:
        """One timer for the whole window, cancelled by whatever closes it."""
        self._cancel_window_timer()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._window_timer = loop.call_later(
            self.agent.config.wake.conversation_seconds, self._window_expired
        )

    def _cancel_window_timer(self) -> None:
        if self._window_timer is not None:
            self._window_timer.cancel()
            self._window_timer = None

    def _window_expired(self) -> None:
        self._window_timer = None
        if self.window is None or not self.window.open:
            return
        why = self.window.tick(self._clock())
        if why is not None:
            self._flush_window("")
            self._emit_window()
            if why.value == "mic_check":
                self._disarm("another application took the microphone")

    def _clock(self) -> float:
        import time

        return time.monotonic()

    def _flush_window(self, detail: str) -> None:
        for kind, note in self.window.drain() if self.window else []:
            self._log_wake(f"window {kind}", note or detail)

    def _decide(self, message: dict[str, Any]) -> None:
        """A click on a confirmation card.

        Only a `SocketGate` can be answered this way. If the connection is
        wired to any other gate, a decision message is meaningless and is
        refused rather than quietly ignored, because a front end sending one
        into a gate that cannot hear it would look exactly like an approval
        that did nothing.
        """
        gate = self.agent.gate
        if not isinstance(gate, SocketGate):
            self.emit("error", message=f"this connection's gate is {gate.name}, not a card")
            return
        token = str(message.get("token", ""))
        allowed = bool(message.get("allow", False))
        if not gate.decide(token, allowed):
            self.emit("error", message="nothing was waiting on that answer")

    async def _clear_draft(self, message: dict[str, Any]) -> None:
        """The × beside a draft. The same tool, not a second implementation.

        The browser sends a name and the server runs `clear_draft`, so there is
        one meaning of "cleared", one place it is logged, and no path where the
        panel can move a file the tool would have refused to move.
        """
        registry = self.agent.registry
        if registry is None or "clear_draft" not in registry.names():
            self.emit("error", message="this connection has no clear_draft wired")
            return
        result = await registry.run("clear_draft", {"name": str(message.get("name", ""))})
        self.emit(
            "notice",
            level="info" if result.ok else "warn",
            message=result.summary or result.content[:200],
        )
        self.push_panel()

    def _gp_entry(self, message: dict[str, Any]) -> None:
        """A GP figure typed into the panel.

        No gate, and no model. A gate exists because a spoken yes is not
        consent and because Jarvis should not act consequentially on its own;
        this is the operator typing a number they were sent, at their own
        keyboard, into their own vault. Asking them to confirm their own
        keystroke would train them to click through cards.

        What it does check is the input, because it arrived over a socket: the
        amount and the period are parsed strictly and a bad one is refused
        rather than guessed at. The path is built by Python from the parsed
        period, so nothing from the browser becomes part of a filename.
        """
        from .gp import BadEntry, money, record

        try:
            entry = record(
                self.agent.config,
                self.agent.vault,
                message.get("amount", ""),
                period=message.get("period", ""),
                note=str(message.get("note", "")),
            )
        except BadEntry as exc:
            self.emit("error", message=str(exc))
            return
        except Exception as exc:
            self.emit("error", message=f"could not record that: {type(exc).__name__}: {exc}")
            return

        self.emit(
            "notice",
            level="info",
            message=(
                f"{entry.period} recorded as "
                f"{money(entry.amount, self.agent.config.gp.currency)}"
            ),
        )
        self.push_panel()

    # -- documents -----------------------------------------------------

    def _find_document(self, name: str):
        """One previewable file in the drafts folder, by name or by path.

        Markdown counts. A draft is the thing most often read back -- it is an
        email somebody is about to send -- and "open Obsidian, find the file"
        was the path to reading one. The filter is what `preview` can render
        rather than "is not markdown", so the two cannot disagree about what
        the panel offers.
        """
        from .ownfiles import find, listing
        from .preview import previewable

        wanted = str(name or "").strip()
        if not wanted:
            return None
        for cleared in (False, True):
            files = [f for f in listing(self.agent.vault, self.agent.config,
                                        "drafts", cleared=cleared)
                     if previewable(f.path)]
            found, _ = find(files, wanted)
            if found is not None:
                return found
        return None

    def _send_document(self, path, *, assembly: bool) -> bool:
        """The preview, built from the file on disk. Nothing else is previewed.

        Returns whether it went. **The event is what starts the assembly
        animation in the browser, so it is only ever emitted for a file that is
        on disk right now.** Particles forming over a generation that failed,
        or that is still running, would be the empty ring over a live
        microphone again: an interface saying something happened because it was
        told to, rather than because it did.
        """
        from .preview import preview

        try:
            rendered = preview(
                path,
                root=self.agent.config.vault.root,
                max_blocks=self.agent.config.documents.preview_blocks,
                max_rows=self.agent.config.documents.preview_rows,
            )
        except Exception as exc:
            self.emit("error", message=f"could not preview {path.name}: {exc}")
            return False

        from urllib.parse import quote

        from .server import FILE_PATH

        url = FILE_PATH + quote(rendered.relative)
        payload = rendered.as_dict()
        # `kind` on the wire is the message type -- every frame the browser
        # receives is keyed on it -- so the document's own kind travels as
        # `format`. Sending both under one name made the preview arrive
        # claiming to be a "document" file, which is nothing.
        payload["format"] = payload.pop("kind")
        self.emit(
            "document",
            assembly=bool(assembly) and self.agent.config.documents.assembly,
            particles=self.agent.config.documents.assembly_particles,
            seconds=self.agent.config.documents.assembly_seconds,
            url=url,
            download=url + "?download=1",
            **payload,
        )
        return True

    def _analysis_landed(self, event: Any) -> None:
        """An `analyse` call that succeeded. Show the file it wrote.

        The sheet renders the report **off disk**, not the result object that
        produced it, so what is on screen exists as a file and an export of it
        is a format change rather than a second computation that might
        disagree.
        """
        from . import analysis

        if getattr(event, "name", "") != "analyse" or not getattr(event, "ok", False):
            return
        folder = analysis.folder_for(self.agent.config)
        try:
            newest = max(folder.glob("*.md"), key=lambda p: p.stat().st_mtime)
        except (ValueError, OSError):
            return
        self._send_analysis(newest)

    def _send_analysis(self, path) -> bool:
        from . import analysis

        try:
            report = analysis.read_report(
                path, self.agent.config.vault.root,
                max_rows=self.agent.config.analysis.max_rows,
            )
        except Exception as exc:
            self.emit("error", message=f"could not read that analysis: {exc}")
            return False
        self.emit("analysis", **report.as_dict())
        return True

    def _analysis_export(self, message: dict[str, Any]) -> None:
        """The table on screen, as a document. **The same file, reformatted.**

        Built from the report on disk rather than from the computation, so the
        spreadsheet the operator gets cannot disagree with the table they were
        looking at when they asked for it.
        """
        from . import analysis, documents

        relative = str(message.get("relative", "")).strip()
        kind = str(message.get("format", "xlsx")).strip().lower()
        try:
            target = self.agent.vault.resolve_read(self.agent.config.vault.root / relative)
        except Exception:
            self.emit("error", message="no analysis by that name")
            return
        if not target.is_file() or target.parent.parent.name != analysis.FOLDER:
            self.emit("error", message="that is not an analysis Jarvis wrote")
            return

        report = analysis.read_report(target, self.agent.config.vault.root, max_rows=100_000)
        try:
            made = documents.generate(
                self.agent.vault, self.agent.config.vault.drafts,
                analysis.to_document(report), kind,
                page_size=self.agent.config.documents.page_size,
            )
        except documents.MissingLibrary as exc:
            self.emit("error", message=str(exc))
            return
        except Exception as exc:
            self.emit("error", message=f"could not export it: {exc}")
            return
        self.emit("notice", level="info", message=f"exported to {made.relative}")
        self.push_panel()
        self._send_document(made.path, assembly=False)

    def _document_landed(self, event: Any) -> None:
        """A write_document tool call that succeeded. Open the preview.

        The tool reports the vault-relative path it wrote, and this resolves
        that path and checks the file is there before saying anything to the
        browser. A tool that failed reports `ok=False` and nothing happens
        here, which is the whole of "a failure renders as a failure".
        """
        if getattr(event, "name", "") != "write_document" or not getattr(event, "ok", False):
            return
        relative = str(getattr(event, "summary", "") or "").strip()
        if not relative:
            return
        try:
            target = self.agent.vault.resolve_read(self.agent.config.vault.root / relative)
        except Exception:
            return
        if not target.is_file():
            return
        self._send_document(target, assembly=True)
        self.push_panel()

    def _preview(self, message: dict[str, Any]) -> None:
        """Reopen a preview from the panel. Never replays the animation."""
        found = self._find_document(str(message.get("name", "")))
        if found is None:
            self.emit("error", message="nothing in drafts by that name")
            return
        self._send_document(found.path, assembly=False)

    def _reveal(self, message: dict[str, Any]) -> None:
        """Show the file in the operator's file manager. Never opens it."""
        from .desktop import REVEAL_FAILED, REVEAL_UNSUPPORTED, reveal_file

        found = self._find_document(str(message.get("name", "")))
        if found is None:
            self.emit("error", message="nothing in drafts by that name")
            return
        outcome = reveal_file(found.path)
        if outcome == REVEAL_UNSUPPORTED:
            self.emit("notice", level="warn",
                      message="no file manager to open here. Use the download button.")
        elif outcome == REVEAL_FAILED:
            self.emit("notice", level="warn", message=f"could not show {found.name}")
        else:
            self.emit("notice", level="info", message=f"showing {found.name}")

    # -- dropped files -------------------------------------------------

    def _import_context(self, name: str):
        """(the dropped file, its rows, the header index, the headers).

        Python throughout. This reads a spreadsheet; it never sends one
        anywhere, and no part of what it reads reaches a model.
        """
        from . import imports, shapes

        found, _ = imports.find(self.agent.vault, self.agent.config, name)
        if found is None or not found.tabular:
            return None, [], -1, []
        tables = imports.tables_of(found.path)
        if not tables:
            return found, [], -1, []
        table = tables[0]
        index = shapes.header_row(table.rows)
        headers = table.header_at(index)
        return found, table, index, headers

    def _dropped(self, message: dict[str, Any]) -> None:
        """A file landed. Say what happened and, if it is a known shape, import it.

        **A known shape applies silently**, because being stopped every month
        to confirm the same mapping is how a card becomes a reflex. What it
        never does is apply a shape it has not been shown: an unknown header
        row is reported as unknown, in plain words, and waits.
        """
        from . import imports, shapes

        name = str(message.get("name", "")).strip()
        self.push_panel()
        found, table, index, headers = self._import_context(name)
        if found is None or index < 0:
            return

        known = shapes.find(self.agent.vault, self.agent.config, headers)
        if known is None:
            self.emit(
                "notice",
                level="info",
                message=(
                    f"{found.name} is a spreadsheet Jarvis has not been shown before. "
                    "Use import on the panel row and it will propose a mapping."
                ),
            )
            return
        self._run_import(found, table, index, known)

    def _run_import(self, found, table, index, shape) -> None:
        """Read the columns, write what changed, tell the operator."""
        from . import gp

        try:
            period = [h.casefold() for h in table.header_at(index)].index(
                shape.period_column.casefold()
            )
            amount = [h.casefold() for h in table.header_at(index)].index(
                shape.amount_column.casefold()
            )
        except ValueError:
            self.emit("error", message=(
                f"{found.name} matched a known shape but its columns have moved. "
                "Use import on the panel row to map it again."
            ))
            return

        ledger = gp.ledger_for(self.agent.config, self.agent.vault)
        plan = gp.plan_import(
            ledger, table.rows, header_index=index,
            period_column=period, amount_column=amount, formulas=table.formulas,
        )
        if plan.refused:
            self.emit("error", message=plan.refused)
            return

        written = gp.apply_import(
            self.agent.config, self.agent.vault, plan, source=found.name
        )
        summary = plan.summary()
        self.emit("notice", level="info", message=f"{found.name}: {summary}")
        self._log_import(found, summary, len(written))
        self.push_panel()

    def _log_import(self, found, summary: str, written: int) -> None:
        """One line in the inbox. A silent import still leaves a trace.

        The operator asked to know if an export moved five months without being
        stopped every time, and this is that: a notice they will see, written
        whether or not anybody was at the keyboard when it happened.
        """
        from datetime import datetime

        from .heartbeat import Inbox, Notice

        try:
            Inbox(self.agent.vault, self.agent.config.vault.inbox).write(
                Notice(
                    kind="import",
                    title=f"Imported {found.name}: {summary}",
                    body=(
                        f"Read from {found.relative} using a column mapping you "
                        f"confirmed earlier. {written} entry(s) written to Ranger/gp. "
                        "Nothing was overwritten; a changed figure supersedes the "
                        "one it corrects."
                    ),
                    created=datetime.now(),
                )
            )
        except Exception as exc:  # an inbox failure must not lose the import
            self.emit("error", message=f"the import was written; the notice was not: {exc}")

    def _import_propose(self, message: dict[str, Any]) -> None:
        """What Jarvis would import, and from which columns. Writes nothing.

        The proposal comes from the *header* text and from nothing else. A cell
        cannot reach this: it is a regular expression over the header row, and
        then a person at a keyboard.
        """
        from . import gp, shapes

        name = str(message.get("name", "")).strip()
        found, table, index, headers = self._import_context(name)
        if found is None:
            self.emit("error", message="no dropped file by that name")
            return
        if index < 0:
            self.emit("error", message=(
                f"{found.name} has no header row Jarvis can see, so there is nothing "
                "to map. It is still readable as context."
            ))
            return

        known = shapes.find(self.agent.vault, self.agent.config, headers)
        period_name = known.period_column if known else ""
        amount_name = known.amount_column if known else ""
        ambiguity = ""
        if not (period_name and amount_name):
            proposal = shapes.propose(headers)
            period_name, amount_name = proposal.period, proposal.amount
            # Where it will not guess, said out loud. Two columns that could
            # both be the figure is a question; picking one would be a number
            # the operator never chose.
            ambiguity = proposal.why()

        preview_plan = None
        if period_name and amount_name:
            lowered = [h.casefold() for h in headers]
            try:
                ledger = gp.ledger_for(self.agent.config, self.agent.vault)
                preview_plan = gp.plan_import(
                    ledger, table.rows, header_index=index,
                    period_column=lowered.index(period_name.casefold()),
                    amount_column=lowered.index(amount_name.casefold()),
                    formulas=table.formulas,
                )
            except ValueError:
                preview_plan = None

        self.emit(
            "import_proposal",
            name=found.name,
            relative=found.relative,
            known=known is not None,
            headers=list(headers),
            period=period_name,
            amount=amount_name,
            fingerprint=shapes.fingerprint(headers),
            ambiguity=ambiguity,
            changes=[
                {"period": c.period, "amount": str(c.amount),
                 "was": None if c.was is None else str(c.was), "verdict": c.verdict}
                for c in (preview_plan.changes if preview_plan else [])
            ],
            skipped=list(preview_plan.skipped) if preview_plan else [],
            refused=preview_plan.refused if preview_plan else "",
            summary=preview_plan.summary() if preview_plan else "",
        )

    def _import_apply(self, message: dict[str, Any]) -> None:
        """The operator confirmed. Remember the shape, then write what changed."""
        from datetime import date

        from . import shapes

        name = str(message.get("name", "")).strip()
        period_name = str(message.get("period", "")).strip()
        amount_name = str(message.get("amount", "")).strip()
        found, table, index, headers = self._import_context(name)
        if found is None or index < 0:
            self.emit("error", message="no dropped file by that name")
            return

        lowered = [h.casefold() for h in headers]
        if period_name.casefold() not in lowered or amount_name.casefold() not in lowered:
            self.emit("error", message="those columns are not in that file")
            return

        shape = shapes.Shape(
            fingerprint=shapes.fingerprint(headers),
            name=found.name,
            headers=tuple(headers),
            period_column=period_name,
            amount_column=amount_name,
            sheet="",
            confirmed=date.today().isoformat(),
        )
        try:
            shapes.remember(self.agent.vault, self.agent.config, shape)
        except Exception as exc:
            self.emit("error", message=f"could not remember that mapping: {exc}")
            return
        self._run_import(found, table, index, shape)

    def _dismiss(self, message: dict[str, Any]) -> None:
        from .panel import dismiss

        title = dismiss(self.agent.config, self.agent.vault, str(message.get("id", "")))
        if title is None:
            self.emit("error", message="that notice is not in the inbox, or is already dismissed")
        else:
            self.emit("dismissed", title=title)
        self.push_panel()

    # -- the turn ------------------------------------------------------

    async def _run(self, text: str) -> None:
        from .events import TextDelta, ToolFinished
        from .speech import SentenceStream

        speaking = SentenceStream() if self.speaker is not None else None
        spoken = 0
        #: The sentence just spoken, sent as context with the next one. Each
        #: sentence is its own request, so without it prosody restarts at every
        #: boundary and a sentence opening with a figure starts cold.
        previous = ""
        self.speech = []
        self.spoken_to = -1
        # What the tools returned this turn, and what Jarvis said about it. The
        # two are compared at the end: a figure in the reply that no tool
        # computed is reported rather than left to look like the rest.
        computed: list[str] = [text]
        said: list[str] = []
        if self.window is not None:
            self.window.begin()

        try:
            async for event in self.agent.turn(text):
                if isinstance(event, ToolFinished):
                    self._remember_tool(event)
                    self._document_landed(event)
                    self._analysis_landed(event)
                    computed.append(event.summary or "")
                    computed.append(getattr(event, "content", "") or "")
                if isinstance(event, TextDelta):
                    said.append(event.text)
                self.send(event.as_dict())
                if speaking is not None and isinstance(event, TextDelta):
                    batch = speaking.feed(event.text)
                    for position, sentence in enumerate(batch):
                        # `after` only when it costs nothing. One chunk of the
                        # model's output often yields several sentences, and
                        # then the next one is already known. Holding a
                        # sentence back to learn what follows would add a
                        # sentence of latency to every reply, which is the
                        # thing sentence-by-sentence speech exists to remove.
                        ahead = batch[position + 1] if position + 1 < len(batch) else ""
                        await self._speak(sentence, spoken, before=previous, after=ahead)
                        previous = sentence
                        spoken += 1
            if speaking is not None:
                leftover = speaking.flush().strip()
                if leftover:
                    # Nothing after it, and that is worth saying: the last
                    # sentence of a reply should sound like the last one.
                    await self._speak(leftover, spoken, before=previous)
            self._check_figures("".join(said), computed)
        except asyncio.CancelledError:
            # Barge-in, and the replacement turn is already starting. Emitting
            # idle or done here would tell the front end the new turn had
            # finished before it began.
            raise
        except Exception as exc:  # a failed turn is an event, not a dead socket
            # Reporting the failure must not assume the socket that just failed
            # is available to report it on. If the turn died *because* the
            # connection went, emitting here is the second exception and the
            # one that actually escapes.
            self._safely(lambda: self.emit("error", message=f"{type(exc).__name__}: {exc}"))
            self._safely(self._finish)
        else:
            self._finish()

    def _safely(self, action: Callable[[], None]) -> None:
        """Run an error-path action that must not raise a second time.

        Only for the paths that report a failure. Everything else raises
        normally: swallowing exceptions on the way *in* would hide real bugs,
        while swallowing them on the way out of a failure hides nothing that
        was not already being reported.
        """
        try:
            action()
        except Exception:
            pass

    def _finish(self) -> None:
        self.busy = False
        self.emit("state", state=State.IDLE.value)
        self.push_panel()
        self.emit("done")
        if self.window is not None:
            # Half the anchor. The other half is the browser saying it has
            # stopped talking, which may already have arrived or may not.
            self.window.finished()
            self._maybe_open()

    async def _speak(self, sentence: str, index: int, *, before: str = "",
                     after: str = "") -> None:
        """One sentence of audio, sent as soon as it exists.

        Sentence by sentence rather than a whole reply, because that is where
        the perceived latency lives: waiting for a full answer before saying
        any of it adds a second or more to every turn for nothing. Failure here
        is a notice, never the end of a turn: losing the voice is bad, losing
        the answer is worse.
        """
        from .listen import encode_audio, speak, speech_format

        try:
            audio = await speak(self.speaker, sentence, before=before, after=after)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.emit("notice", level="warn", message=f"could not speak that: {exc}")
            return
        if audio:
            if self.window is not None:
                self.window.sent(index)
            # The detector is told what is playing rather than working it out
            # from the microphone, which is the only way it can tell Jarvis's
            # own voice from anybody else's.
            while len(self.speech) <= index:
                self.speech.append("")
            self.speech[index] = sentence
            if self.bargein is not None:
                self.bargein.speaking(True)
            self.emit(
                "speech",
                index=index,
                text=sentence,
                audio=encode_audio(audio),
                # The browser cannot work this out from the bytes: pcm_24000
                # has no header to work it out from.
                format=speech_format(self.agent.config.tts.output_format),
            )

    def _check_figures(self, said: str, computed: list[str]) -> None:
        """Every figure Jarvis stated, checked against what Python returned.

        **This is what makes "the model does not do arithmetic" a property
        rather than an intention.** The prompt says it, the analysis tool has
        nowhere to put a number, and this is the third thing: a figure in the
        reply that no tool produced is named in the panel and written to the
        log.

        It reports rather than rewrites. A wrong number that has been pointed
        at is recoverable; silently editing what Jarvis said would be worse
        than the problem, and would hide the fact that it happened at all.
        """
        from .figures import notice, unverified

        if not self.agent.config.analysis.check_figures:
            return
        found = unverified(said, computed)
        if not found:
            return
        message = notice(found)
        self.emit("notice", level="warn", message=message)
        audit = getattr(self.agent, "audit", None)
        if audit is not None:
            try:
                audit.write("unverified figure", message)
            except Exception:
                pass

    def _remember_tool(self, event: Any) -> None:
        self.tools.insert(0, {"name": event.name, "ok": event.ok, "summary": event.summary})
        del self.tools[RECENT_TOOLS:]

    def close(self) -> None:
        """The socket went away. Nothing is left waiting, and the microphone
        is put down: a closed tab must never leave Jarvis listening."""
        if self.listener is not None:
            self.listener.hotword.disarm("the interface went away")
            self.listener.stop()
            self.listener = None
            self._log_wake("disarmed", "the interface went away")
        if isinstance(self.agent.gate, SocketGate):
            self.agent.gate.abandon()
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()


def build_voice(config: Config) -> tuple[Any, Any, bool]:
    """Ears and a mouth for a browser session, or nothing if the keys are not set.

    Missing keys are not an error here. The interface has to open and take
    typed turns whether or not speech is configured, so this reports what is
    available and the front end shows a microphone only if there is one.
    """
    import os

    transcriber = speaker = None
    hinted = False

    deepgram = os.environ.get("DEEPGRAM_API_KEY", "").strip()
    if deepgram:
        from dataclasses import replace as _replace

        from .cli import _keyterm_plan
        from .stt import build_transcriber

        stt = config.stt
        try:
            plan = _keyterm_plan(config)
            if plan.terms:
                stt = _replace(stt, keyterms=plan.terms)
                hinted = True
        except Exception:
            pass
        transcriber = build_transcriber(stt, deepgram)

    elevenlabs = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if elevenlabs and config.tts.voice_id:
        from .tts import build_speaker

        speaker = build_speaker(config.tts, elevenlabs)

    return transcriber, speaker, hinted


def build_session(
    config: Config,
    send: Callable[[dict[str, Any]], None],
    *,
    gate: Gate | None = None,
    origin: str = "browser",
) -> Session:
    """Wire a core for one connection.

    The browser gets a `SocketGate`: there is a person at a keyboard and a card
    to click, which is the same situation the terminal is in. What it does not
    get is a gate it can talk its way past. Nothing runs until the gate returns
    approved, and the gate only returns approved when a decision arrives
    carrying the token of the question that is actually open.
    """
    from .assembly import build_agent

    def emit(payload: dict[str, Any]) -> None:
        send(payload)

    # Session.__post_init__ redirects a card gate's emit through Session.watch,
    # so this one is only what the gate uses before the session exists.
    agent = build_agent(config, gate=gate or SocketGate(emit), origin=origin)
    transcriber, speaker, hinted = build_voice(config)
    return Session(
        agent=agent,
        send=send,
        transcriber=transcriber,
        speaker=speaker,
        keyterms=hinted,
    )
