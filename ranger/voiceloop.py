"""Tier 3d. Push to talk, wrapped around the core.

This file contains no agent logic. It turns a key press into text, hands that
text to the same Ranger.turn() a typed line goes through, and turns the reply
back into sound. If anything here starts deciding what Ranger should say, it
belongs in core.py instead.

Four things it has to get right, all of them about how the wait feels:

  - Say something the instant the key comes up. Transcription takes the better
    part of a second and silence in that gap reads as broken.
  - Show what it heard next to what it said, so a wrong answer can be blamed on
    the ears or the brain without guessing.
  - Speak the first sentence while the rest is still being written.
  - Let a press interrupt the speech, and make that same press the start of the
    next thing said. Pressing to cut in and then having to press again would be
    infuriating.

It never records while it is speaking, because recording only ever starts after
playback has been stopped.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .audio import AudioError, duration_seconds, levels, write_wav
from .config import Config
from .core import Ranger
from .cost import TurnCost
from .events import Notice, State, StateChanged, TextDelta, ToolCalled, ToolFinished, TurnComplete
from .speech import SentenceStream
from .stt import TranscriptionError
from .trigger import Trigger, TriggerError
from .tts import SpeechError, decode

# Kept as names rather than inline escapes: a backslash inside an f-string
# expression is a syntax error before Python 3.12, and this package supports
# 3.11.
DIM = "\033[2m"
BOLD = "\033[1m"
RED = "\033[31m"
YELLOW = "\033[33m"


@dataclass
class TurnTiming:
    """What each stage cost, so slow can be told from broken."""

    recorded_seconds: float = 0.0
    heard_ms: int = 0
    first_text_ms: int = 0
    first_audio_ms: int = 0
    total_ms: int = 0

    def render(self) -> str:
        return (
            f"[{self.recorded_seconds:.1f}s said | heard {self.heard_ms} ms | "
            f"first word {self.first_text_ms} ms | first sound {self.first_audio_ms} ms]"
        )


def diagnose(pcm: bytes, seconds: float, measured: Any, heard_speech: bool | None) -> list[str]:
    """Say which kind of nothing this was.

    A missed key press, a microphone that never opened, and speaking too
    quietly all produce no transcript and are indistinguishable without the
    numbers. This is the silence-reads-as-broken case, so the numbers go in the
    message rather than in a separate command.
    """
    peak = "silent" if measured.silent else f"{measured.peak_dbfs:.0f} dBFS peak"
    header = f"nothing usable in that: {seconds:.1f}s captured, {peak}"

    if seconds < 0.25:
        return [
            header,
            "that is shorter than a word, so the key press probably did not register.",
            "hold the key down while you speak, and release when you finish.",
        ]
    if measured.silent:
        return [
            header,
            "every sample was zero, which is no signal rather than a quiet room.",
            "on Windows: Settings, Privacy and security, Microphone, and turn on",
            "'Let desktop apps access your microphone'. If that is already on, the",
            "wrong input device is selected. Run 'ranger audio devices'.",
        ]
    if measured.very_quiet:
        return [
            header,
            "the microphone is working but that was very faint. Speech should peak",
            "around -20 to -6 dBFS. Move closer, or raise the input level in Windows.",
        ]
    return [
        header,
        "the audio was fine, so this was transcription finding no speech in it.",
        "background noise, or the words did not land. Just say it again.",
    ]


class VoiceLoop:
    def __init__(
        self,
        agent: Ranger,
        config: Config,
        trigger: Trigger,
        backend: Any,
        transcriber: Any,
        speaker: Any,
        *,
        out: Any,
        input_device: int | None = None,
        output_device: int | None = None,
        paint: Callable[[str, str], str] | None = None,
    ) -> None:
        self.agent = agent
        self.config = config
        self.trigger = trigger
        self.backend = backend
        self.transcriber = transcriber
        self.speaker = speaker
        self.out = out
        self.input_device = input_device
        self.output_device = output_device
        self.paint = paint or (lambda text, code: text)
        #: One outstanding "the key went down" wait, shared between starting a
        #: turn and interrupting speech. A press during speech stops it and is
        #: the same press that begins the next recording.
        self._press: asyncio.Task | None = None
        self.turns = 0
        self.session_cost = TurnCost()

    def say(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)

    # -- the shared key press ---------------------------------------------

    def _press_task(self) -> asyncio.Task:
        if self._press is None or self._press.done() and self._press.cancelled():
            self._press = asyncio.create_task(asyncio.to_thread(self.trigger.wait_to_start))
        return self._press

    async def _wait_for_press(self) -> bool:
        task = self._press_task()
        started = await task
        self._press = None
        return started

    # -- one turn ----------------------------------------------------------

    async def run(self) -> int:
        self.say(self.paint(f"  {self.trigger.hint}", DIM))
        self.say(self.paint("  press it again while Ranger is talking to cut in", DIM))
        self.say()
        try:
            while True:
                if not await self.one_turn():
                    return 0
        except (KeyboardInterrupt, asyncio.CancelledError):
            return 0
        finally:
            self.trigger.close()

    async def one_turn(self) -> bool:
        if not await self._wait_for_press():
            return False

        stop = self.trigger.stop_event()
        ceiling = threading.Timer(self.config.voice.max_seconds, stop.set)
        ceiling.daemon = True
        ceiling.start()

        began = time.monotonic()
        try:
            pcm = await asyncio.to_thread(
                self.backend.record,
                samplerate=self.config.voice.sample_rate,
                channels=self.config.voice.channels,
                device=self.input_device,
                stop=stop,
            )
        except AudioError as exc:
            self.say(self.paint(f"  {exc}", RED))
            return True
        finally:
            ceiling.cancel()

        # The first thing that happens after the key comes up, before anything
        # that takes time. Silence here is what reads as broken.
        self.say(self.paint("  ...", DIM))

        timing = TurnTiming(
            recorded_seconds=duration_seconds(
                pcm, self.config.voice.sample_rate, self.config.voice.channels
            )
        )

        measured = levels(pcm)
        if measured.silent or timing.recorded_seconds < 0.25:
            for line in diagnose(pcm, timing.recorded_seconds, measured, heard_speech=None):
                self.say(self.paint(f"  {line}", YELLOW))
            return True

        heard_at = time.monotonic()
        try:
            transcript = await self.transcriber.transcribe(self._wav(pcm))
        except TranscriptionError as exc:
            self.say(self.paint(f"  {exc}", RED))
            return True
        timing.heard_ms = int((time.monotonic() - heard_at) * 1000)

        if transcript.empty:
            for line in diagnose(pcm, timing.recorded_seconds, measured, heard_speech=False):
                self.say(self.paint(f"  {line}", YELLOW))
            return True

        # Requirement 3: what it heard, always, right next to what it says.
        heard_label = self.paint("you:", BOLD)
        self.say(f"  {heard_label}    {transcript.text}")
        shaky = transcript.shaky_words
        if shaky:
            listing = ", ".join(f"{w.text} ({w.confidence:.0%})" for w in shaky[:5])
            self.say(self.paint(f"          least certain: {listing}", DIM))

        await self._answer(transcript.text, timing, began)
        self.turns += 1
        return True

    def _wav(self, pcm: bytes) -> bytes:
        import io
        import wave

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(self.config.voice.channels)
            handle.setsampwidth(2)
            handle.setframerate(self.config.voice.sample_rate)
            handle.writeframes(pcm)
        return buffer.getvalue()

    # -- the reply ---------------------------------------------------------

    async def _answer(self, text: str, timing: TurnTiming, began: float) -> None:
        sentences: asyncio.Queue = asyncio.Queue()
        stop_speaking = threading.Event()
        first_audio: list[float] = []

        speaking = asyncio.create_task(
            self._speak_queue(sentences, stop_speaking, first_audio, began)
        )
        stream = SentenceStream()
        cost_line = ""
        thinking_at = time.monotonic()
        printed_prefix = False
        notices: list[str] = []

        try:
            async for event in self.agent.turn(text):
                if isinstance(event, TextDelta):
                    if not printed_prefix:
                        printed_prefix = True
                        timing.first_text_ms = int((time.monotonic() - thinking_at) * 1000)
                        label = self.paint("ranger:", BOLD)
                        print(f"  {label} ", end="", file=self.out, flush=True)
                    print(event.text, end="", file=self.out, flush=True)
                    for sentence in stream.feed(event.text):
                        await sentences.put(sentence)
                elif isinstance(event, ToolCalled):
                    self.say(self.paint(f"\n  -> {event.name} {event.input}", DIM))
                    printed_prefix = False
                elif isinstance(event, ToolFinished):
                    mark = "ok" if event.ok else "failed"
                    self.say(self.paint(f"  <- {event.name} {mark}: {event.summary}", DIM))
                elif isinstance(event, Notice):
                    notices.append(f"[{event.level}] {event.message}")
                elif isinstance(event, TurnComplete):
                    remainder = stream.flush()
                    if remainder:
                        await sentences.put(remainder)
                    if event.usage:
                        turn = TurnCost.from_usage(event.usage)
                        self.session_cost = self.session_cost + turn
                        cost_line = turn.render(self.config.model)
        finally:
            await sentences.put(None)

        interrupted = await self._finish_speaking(speaking, stop_speaking)

        if printed_prefix:
            print(file=self.out, flush=True)
        if first_audio:
            timing.first_audio_ms = int((first_audio[0] - began) * 1000)
        timing.total_ms = int((time.monotonic() - began) * 1000)

        if interrupted:
            self.say(self.paint("  (cut off)", DIM))
        for notice in notices:
            self.say(self.paint(f"  {notice}", YELLOW))
        suffix = f"  [{cost_line}]" if cost_line else ""
        self.say(self.paint(f"  {timing.render()}{suffix}", DIM))
        self.say()

    async def _finish_speaking(self, speaking: asyncio.Task, stop: threading.Event) -> bool:
        """Wait for the speech, unless the key goes down first.

        Quitting and interrupting both end the speech but they are not the same
        event: only a real press is a barge-in, and only a real press carries
        forward as the start of the next recording. Treating the trigger's
        "the operator quit" return as an interrupt would report a cut-off turn
        every time Ranger was shut down mid-sentence.
        """
        press = self._press_task()
        done, _ = await asyncio.wait({speaking, press}, return_when=asyncio.FIRST_COMPLETED)
        if press not in done:
            return False

        stop.set()
        await speaking
        # The press is deliberately left unconsumed: it is the start of the
        # next recording, so cutting in and speaking is one action.
        return bool(press.result())

    async def _speak_queue(
        self,
        sentences: asyncio.Queue,
        stop: threading.Event,
        first_audio: list[float],
        began: float,
    ) -> None:
        while True:
            sentence = await sentences.get()
            if sentence is None or stop.is_set():
                return
            try:
                audio = b"".join([chunk async for chunk in self.speaker.stream(sentence)])
            except SpeechError as exc:
                self.say(self.paint(f"\n  {exc}", RED))
                return
            if stop.is_set() or not audio:
                continue
            try:
                pcm, rate = decode(audio, self.config.tts.output_format)
            except SpeechError as exc:
                self.say(self.paint(f"\n  {exc}", RED))
                return
            if not first_audio:
                first_audio.append(time.monotonic())
            try:
                await asyncio.to_thread(
                    self.backend.play,
                    pcm,
                    samplerate=rate,
                    channels=1,
                    device=self.output_device,
                    stop=stop,
                )
            except AudioError as exc:
                self.say(self.paint(f"\n  {exc}", RED))
                return
