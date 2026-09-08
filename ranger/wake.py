"""Hands free. A local hotword, and everything that keeps it honest.

The operator settled against a wake word twice and then reopened it, with
conditions. Those conditions are the design, so they are written here rather
than left in a conversation:

  1. Off by default, opted into per session, never persisted on.
  2. Unmistakable in the interface while the microphone is live.
  3. One click to kill, and escape kills it too.
  4. Never arms while another application has the microphone open.
  5. A two word phrase only.
  6. Auto-disarms after a period with no interaction.
  7. openWakeWord is an optional extra, never a hard dependency.

**Nothing streams anywhere until the phrase fires.** The model runs locally on
80ms frames and the audio never leaves the machine until there is a reason for
it to. That is the whole reason for a hotword rather than an open connection to
a transcription service.

Two things that are not obvious and matter more than they look.

**The pre-roll buffer.** Detection lags the phrase by a few hundred
milliseconds, and people run the phrase into the request: "Hey Ranger what is
happening with Illes". Without a rolling buffer the first word after the phrase
is already gone by the time anything starts recording. So the buffer runs
continuously while armed and the utterance starts *before* the fire. The phrase
then appears at the front of the transcript, where it is stripped as text.
Cutting it out of the audio at a guessed boundary is how the first word gets
lost.

**A fire with no speech after it is discarded, and still logged.** Silence is
never sent: it costs money, it produces an empty transcript that looks like a
bug, and it is the most common outcome of a false fire. Logging it anyway is
what lets the operator count false fires over a week rather than guess at them.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

#: openWakeWord expects 16kHz mono, 80ms at a time.
SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280

#: Below this a frame is silence. Root mean square of int16 samples, so it is
#: measured in the same units the audio arrives in.
SILENCE_RMS = 380.0


class State(str, Enum):
    OFF = "off"
    ARMED = "armed"          # listening for the phrase, nothing recorded
    WAITING = "waiting"      # the phrase fired, waiting for speech to start
    RECORDING = "recording"  # speech is being captured


class Outcome(str, Enum):
    """How a fire ended. Every one of these reaches the audit log."""

    SPOKE = "spoke"              # a real utterance was captured and sent
    NO_SPEECH = "no_speech"      # fired, nothing followed, discarded
    TOO_LONG = "too_long"        # hit the ceiling and was sent anyway
    BLOCKED = "blocked"          # fired while another app had the microphone


class WakeUnavailable(Exception):
    """openWakeWord is not installed, or its model is not where it should be."""


def available() -> tuple[bool, str]:
    """Whether hands free can run at all, and why not if it cannot.

    An optional extra by the operator's condition seven. They are on Python
    3.14 where wheel availability for onnxruntime cannot be checked from here,
    and the rest of Ranger has to be unaffected if it will not install.
    """
    try:
        import openwakeword  # noqa: F401
    except Exception as exc:
        return False, (
            f"openwakeword is not installed ({type(exc).__name__}). "
            'Run: pip install -e ".[wake]"'
        )
    try:
        import onnxruntime  # noqa: F401
    except Exception as exc:
        return False, (
            f"onnxruntime is not installed ({type(exc).__name__}), which openwakeword needs. "
            'Run: pip install -e ".[wake]"'
        )
    return True, "openwakeword is installed"


def rms(frame: bytes) -> float:
    """Loudness of one frame of 16 bit little endian mono samples."""
    if len(frame) < 2:
        return 0.0
    import array

    samples = array.array("h")
    samples.frombytes(frame[: len(frame) // 2 * 2])
    if not samples:
        return 0.0
    total = 0
    for sample in samples:
        total += sample * sample
    return math.sqrt(total / len(samples))


def strip_phrase(text: str, phrase: str) -> str:
    """Take the wake phrase off the front of a transcript.

    Text rather than audio, deliberately. The audio boundary is a guess and
    guessing it wrong eats the first word of the request.
    """
    words = phrase.split()
    if not words:
        return text.strip()

    spoken = text.strip()
    lowered = spoken.lower()

    # Every contiguous run of the phrase's words, longest first. Transcription
    # drops the leading "hey" often enough that "ranger where are we" has to be
    # handled, and "hey" alone has to be handled for the same reason.
    candidates: list[str] = []
    for start in range(len(words)):
        for end in range(len(words), start, -1):
            candidates.append(" ".join(words[start:end]))
    candidates.sort(key=len, reverse=True)

    for candidate in candidates:
        prefix = candidate.lower()
        if lowered.startswith(prefix):
            return spoken[len(prefix):].lstrip(" ,.!?-—").strip()
    return spoken


@dataclass
class Ring:
    """The pre-roll. A fixed number of frames, oldest dropped."""

    frames: int
    _held: list[bytes] = field(default_factory=list)

    def push(self, frame: bytes) -> None:
        self._held.append(frame)
        if len(self._held) > self.frames:
            del self._held[: len(self._held) - self.frames]

    def drain(self) -> bytes:
        audio = b"".join(self._held)
        self._held.clear()
        return audio

    def clear(self) -> None:
        self._held.clear()


@dataclass(frozen=True)
class Fire:
    """One firing of the hotword, whatever became of it."""

    at: float
    outcome: Outcome
    confidence: float
    seconds: float = 0.0
    blockers: tuple[str, ...] = ()

    def describe(self, phrase: str) -> str:
        """The audit line. Counting these over a week is the point."""
        if self.outcome is Outcome.NO_SPEECH:
            return (
                f"{phrase!r} fired at {self.confidence:.2f} and nothing followed, "
                "so it was discarded"
            )
        if self.outcome is Outcome.BLOCKED:
            return (
                f"{phrase!r} fired at {self.confidence:.2f} while the microphone was "
                "held by " + ", ".join(self.blockers) + ", so nothing was recorded"
            )
        if self.outcome is Outcome.TOO_LONG:
            return (
                f"{phrase!r} fired at {self.confidence:.2f} and ran to the "
                f"{self.seconds:.0f}s ceiling"
            )
        return f"{phrase!r} fired at {self.confidence:.2f}, {self.seconds:.1f}s captured"


class Detector:
    """openWakeWord behind a seam, so the loop is testable without it."""

    def __init__(self, model_path: Path | str, threshold: float) -> None:
        ok, why = available()
        if not ok:
            raise WakeUnavailable(why)

        path = Path(model_path)
        if not path.is_file():
            raise WakeUnavailable(
                f"no wake word model at {path}. Train one with "
                "scripts/train_wake_word.py, or point wake.model at a bundled one"
            )

        from openwakeword.model import Model

        self.threshold = threshold
        self.name = path.stem
        self._model = Model(wakeword_models=[str(path)], inference_framework="onnx")

    def feed(self, frame: bytes) -> float:
        """Highest score across the loaded models for this frame."""
        import numpy

        samples = numpy.frombuffer(frame, dtype=numpy.int16)
        scores = self._model.predict(samples)
        return max(scores.values()) if scores else 0.0

    def reset(self) -> None:
        try:
            self._model.reset()
        except Exception:
            pass


@dataclass
class Hotword:
    """The state machine. Frames in, utterances and audit lines out.

    Deliberately knows nothing about microphones, sockets or the core. It is
    fed 80ms frames by whoever has the audio and it says what to do, which is
    what makes every rule in it testable without a microphone.
    """

    detector: Any
    phrase: str = "hey jarvis"
    grace_seconds: float = 3.0
    silence_seconds: float = 0.9
    max_seconds: float = 30.0
    preroll_seconds: float = 1.5
    idle_disarm_seconds: float = 15 * 60
    #: Asked before arming and while armed. Returns a micuse.Verdict.
    check_microphone: Callable[[], Any] | None = None
    now: Callable[[], float] = time.monotonic

    state: State = State.OFF
    _ring: Ring = field(init=False)
    _captured: list[bytes] = field(default_factory=list)
    _fired_at: float = 0.0
    _confidence: float = 0.0
    _last_voice: float = 0.0
    _armed_at: float = 0.0
    _last_interaction: float = 0.0
    _spoke: bool = False

    def __post_init__(self) -> None:
        self._ring = Ring(frames=max(1, int(self.preroll_seconds * SAMPLE_RATE / FRAME_SAMPLES)))

    # -- arming --------------------------------------------------------

    def arm(self) -> tuple[bool, str]:
        """Turn it on, unless something else has the microphone."""
        verdict = self._verdict()
        if verdict is not None and not verdict.allowed:
            return False, verdict.reason
        self.state = State.ARMED
        self._armed_at = self._last_interaction = self.now()
        self._ring.clear()
        self._captured.clear()
        return True, f"listening for {self.phrase!r}"

    def disarm(self, why: str = "stopped") -> str:
        self.state = State.OFF
        self._ring.clear()
        self._captured.clear()
        if self.detector is not None:
            self.detector.reset()
        return why

    @property
    def armed(self) -> bool:
        return self.state is not State.OFF

    def touch(self) -> None:
        """Something happened, so the idle clock starts again."""
        self._last_interaction = self.now()

    def _verdict(self):
        return self.check_microphone() if self.check_microphone else None

    # -- the loop ------------------------------------------------------

    def feed(self, frame: bytes) -> tuple[bytes | None, Fire | None]:
        """One 80ms frame in. Returns an utterance when there is one.

        The second half of the pair is a Fire whenever a firing *resolved*, in
        any way at all, including the discarded ones. A firing that has only
        just started resolves nothing yet: the interface learns about it from
        `state`, and the log gets one line per firing rather than two. Both
        halves are None for almost every frame, which is the point.
        """
        if self.state is State.OFF:
            return None, None

        moment = self.now()

        if self.state is State.ARMED:
            if moment - self._last_interaction > self.idle_disarm_seconds:
                # Condition six. An open microphone nobody remembers is the
                # failure the operator said they are most likely to create.
                self.disarm("auto-disarmed after no interaction")
                return None, None
            self._ring.push(frame)
            score = self.detector.feed(frame)
            if score < getattr(self.detector, "threshold", 0.5):
                return None, None
            return None, self._fired(score, moment)  # None unless it was blocked

        loud = rms(frame) > SILENCE_RMS
        self._captured.append(frame)

        if self.state is State.WAITING:
            if loud:
                self.state = State.RECORDING
                self._spoke = True
                self._last_voice = moment
                return None, None
            if moment - self._fired_at > self.grace_seconds:
                return None, self._finish(Outcome.NO_SPEECH, moment)
            return None, None

        # RECORDING
        if loud:
            self._last_voice = moment
        elif moment - self._last_voice > self.silence_seconds:
            return self._utterance(), self._finish(Outcome.SPOKE, moment)

        if moment - self._fired_at > self.max_seconds:
            return self._utterance(), self._finish(Outcome.TOO_LONG, moment)
        return None, None

    def _fired(self, score: float, moment: float) -> Fire | None:
        """The phrase was heard. Check the microphone again before recording.

        Checked twice on purpose: once at arming, and again here. A call that
        started while Ranger sat armed is the case that matters, and it is the
        one a check only at arming would miss entirely.
        """
        verdict = self._verdict()
        if verdict is not None and not verdict.allowed:
            self.disarm("something else took the microphone")
            return Fire(moment, Outcome.BLOCKED, score, blockers=tuple(verdict.blockers))

        self.state = State.WAITING
        self._fired_at = moment
        self._confidence = score
        self._spoke = False
        self._last_voice = moment
        # The pre-roll becomes the front of the utterance, so the phrase and
        # anything said straight after it are both already captured.
        self._captured = [self._ring.drain()]
        self.touch()
        return None

    def _utterance(self) -> bytes:
        return b"".join(self._captured)

    def _finish(self, outcome: Outcome, moment: float) -> Fire:
        seconds = moment - self._fired_at
        self.state = State.ARMED
        self._captured.clear()
        self._ring.clear()
        self.detector.reset()
        self.touch()
        return Fire(self._fired_at, outcome, self._confidence, seconds)


# -- the microphone loop ----------------------------------------------------


def build_hotword(config: Any, *, check_microphone: Callable[[], Any] | None = None) -> "Hotword":
    """A Hotword wired from config, or WakeUnavailable saying why not."""
    wake = config.wake
    model = Path(wake.model)
    if not model.suffix:
        # A name openWakeWord ships rather than a path. Its own loader finds it.
        model = _bundled(wake.model)

    return Hotword(
        detector=Detector(model, wake.threshold),
        phrase=wake.phrase,
        grace_seconds=wake.grace_seconds,
        silence_seconds=wake.silence_seconds,
        max_seconds=wake.max_seconds,
        preroll_seconds=wake.preroll_seconds,
        idle_disarm_seconds=wake.idle_disarm_seconds,
        check_microphone=check_microphone,
    )


def _bundled(name: str) -> Path:
    """Where openWakeWord keeps the models it ships with."""
    ok, why = available()
    if not ok:
        raise WakeUnavailable(why)

    import openwakeword

    folder = Path(openwakeword.__file__).parent / "resources" / "models"
    for suffix in (".onnx", ".tflite"):
        candidate = folder / f"{name}{suffix}"
        if candidate.is_file():
            return candidate
    raise WakeUnavailable(
        f"no bundled model called {name!r} in {folder}. openWakeWord ships a small "
        "fixed set, and anything else has to be trained: see scripts/train_wake_word.py"
    )


def wav_of(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    """Wrap raw samples so the transcriber gets something with a header.

    The same reason the browser could not play `pcm_24000`: raw samples say
    nothing about themselves, and every consumer downstream would otherwise
    need telling separately.
    """
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    return buffer.getvalue()
