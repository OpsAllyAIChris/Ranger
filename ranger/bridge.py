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
    _turn: asyncio.Task | None = None

    # -- outbound ------------------------------------------------------

    def emit(self, kind: str, **fields: Any) -> None:
        self.send({"kind": kind, **fields})

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

        if self.busy:
            if not bool(message.get("interrupt", False)):
                self.emit("error", message="still working on the last one")
                return
            # Barge-in. The operator is talking over Ranger, so what Ranger was
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

        def utterance(pcm: bytes) -> None:
            loop.call_soon_threadsafe(self._heard_hands_free, pcm)

        def fired(fire) -> None:
            loop.call_soon_threadsafe(self._log_fire, fire)

        def state(value) -> None:
            loop.call_soon_threadsafe(
                lambda: self.emit("hands_free", **self._hands_free_state(), listening=value.value)
            )

        def level(value: float) -> None:
            loop.call_soon_threadsafe(lambda: self.emit("level", value=round(value, 3)))

        self.listener = Listener(
            hotword=hotword,
            frames=microphone_frames,
            on_utterance=utterance,
            on_fire=fired,
            on_state=state,
            on_level=level,
            check_seconds=wake.mic_check_seconds,
        )
        self.listener.start()
        self.emit("hands_free", **self._hands_free_state())
        self.emit("notice", level="info", message=why)

    def _disarm(self, why: str = "stopped at the keyboard") -> None:
        if self.listener is None:
            self.emit("error", message="hands free is not on")
            return
        self.listener.hotword.disarm(why)
        self.listener.stop()
        self.listener = None
        self._log_wake("disarmed", why)
        self.emit("hands_free", **self._hands_free_state())

    def _heard_hands_free(self, pcm: bytes) -> None:
        """An utterance captured by the hotword, taken as a turn."""
        from .listen import Utterance
        from .wake import wav_of

        if self.busy:
            self.stop("interrupted")
        self.busy = True
        self._turn = asyncio.create_task(
            self._run_audio(Utterance(audio=wav_of(pcm), mime="audio/wav",
                                      seconds=round(len(pcm) / 2 / 16000, 2)),
                            strip=self.agent.config.wake.phrase)
        )

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

        try:
            async for event in self.agent.turn(text):
                if isinstance(event, ToolFinished):
                    self._remember_tool(event)
                self.send(event.as_dict())
                if speaking is not None and isinstance(event, TextDelta):
                    for sentence in speaking.feed(event.text):
                        await self._speak(sentence, spoken)
                        spoken += 1
            if speaking is not None:
                leftover = speaking.flush().strip()
                if leftover:
                    await self._speak(leftover, spoken)
        except asyncio.CancelledError:
            # Barge-in, and the replacement turn is already starting. Emitting
            # idle or done here would tell the front end the new turn had
            # finished before it began.
            raise
        except Exception as exc:  # a failed turn is an event, not a dead socket
            self.emit("error", message=f"{type(exc).__name__}: {exc}")
            self._finish()
        else:
            self._finish()

    def _finish(self) -> None:
        self.busy = False
        self.emit("state", state=State.IDLE.value)
        self.push_panel()
        self.emit("done")

    async def _speak(self, sentence: str, index: int) -> None:
        """One sentence of audio, sent as soon as it exists.

        Sentence by sentence rather than a whole reply, because that is where
        the perceived latency lives: waiting for a full answer before saying
        any of it adds a second or more to every turn for nothing. Failure here
        is a notice, never the end of a turn: losing the voice is bad, losing
        the answer is worse.
        """
        from .listen import encode_audio, speak, speech_format

        try:
            audio = await speak(self.speaker, sentence)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.emit("notice", level="warn", message=f"could not speak that: {exc}")
            return
        if audio:
            self.emit(
                "speech",
                index=index,
                text=sentence,
                audio=encode_audio(audio),
                # The browser cannot work this out from the bytes: pcm_24000
                # has no header to work it out from.
                format=speech_format(self.agent.config.tts.output_format),
            )

    def _remember_tool(self, event: Any) -> None:
        self.tools.insert(0, {"name": event.name, "ok": event.ok, "summary": event.summary})
        del self.tools[RECENT_TOOLS:]

    def close(self) -> None:
        """The socket went away. Nothing is left waiting, and the microphone
        is put down: a closed tab must never leave Ranger listening."""
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

    agent = build_agent(config, gate=gate or SocketGate(emit), origin=origin)
    transcriber, speaker, hinted = build_voice(config)
    return Session(
        agent=agent,
        send=send,
        transcriber=transcriber,
        speaker=speaker,
        keyterms=hinted,
    )
