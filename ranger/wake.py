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


# -- the models -------------------------------------------------------------
#
# The wheel ships no models at all. Not the hotwords, and not the two feature
# models every hotword runs on top of: `pip install openwakeword` leaves no
# `resources/models` directory behind, which is why enabling hands free after a
# clean install says there is no model rather than misbehaving. They are
# downloaded on first use from the openWakeWord project's own GitHub release
# assets, and `ranger wake install` is that download made explicit rather than
# something that happens quietly the first time the microphone opens.

#: Where the files come from. dscripka/openWakeWord is the project itself, not
#: a mirror and not a model hub, and this is written down because a wake word
#: is a network fetch of a binary that then listens to a room.
MODEL_SOURCE = "https://github.com/dscripka/openWakeWord/releases (v0.5.1 assets)"

#: Every hotword runs on these two. They are shared, downloaded once, and
#: their absence is the failure that looks like a broken hotword.
FEATURE_MODELS = ("melspectrogram.onnx", "embedding_model.onnx")

#: The names openWakeWord publishes. Anything else has to be trained.
PUBLISHED = ("alexa", "hey_mycroft", "hey_jarvis", "hey_rhasspy", "timer", "weather")

#: A download that never reached GitHub still writes a file: openWakeWord's
#: downloader streams the response body whatever the status code was, so a
#: proxy error page lands on disk named like a model. Every real one is over a
#: megabyte, so anything this small is that error page.
SMALLEST_PLAUSIBLE_MODEL = 100_000


def models_folder() -> Path:
    """Where openWakeWord looks for models, which is inside its own package.

    Not a Ranger folder and deliberately not configurable. The two feature
    models are found by a hardcoded path inside openWakeWord's preprocessor,
    so a hotword downloaded anywhere else would load and then fail on the
    first frame.
    """
    ok, why = available()
    if not ok:
        raise WakeUnavailable(why)

    import openwakeword

    return Path(openwakeword.__file__).parent / "resources" / "models"


def find_model(name: str, folder: Path | None = None) -> Path | None:
    """The file for a published name, or None.

    The published names are not the file names. `hey_jarvis` is released as
    `hey_jarvis_v0.1.onnx`, so an exact match is not enough and looking for one
    is why a correct download still reported no model. Highest version wins.

    ONNX only. Ranger's extra installs onnxruntime and not tflite_runtime, and
    handing a `.tflite` to a model built for ONNX raises deep inside
    openWakeWord rather than here.
    """
    folder = folder if folder is not None else models_folder()
    if not folder.is_dir():
        return None

    exact = folder / f"{name}.onnx"
    if exact.is_file():
        return exact

    versioned = sorted(
        (path for path in folder.glob(f"{name}_v*.onnx") if path.is_file()),
        key=_version_of,
    )
    return versioned[-1] if versioned else None


def _version_of(path: Path) -> tuple[int, ...]:
    """0.10 is after 0.9, which sorting the file names would get backwards."""
    tail = path.stem.rsplit("_v", 1)[-1]
    try:
        return tuple(int(part) for part in tail.split("."))
    except ValueError:
        return (-1,)


def resolve_model(setting: str, folder: Path | None = None) -> Path:
    """The path config's `wake.model` means, or WakeUnavailable saying why not.

    A setting with a suffix is a file the operator trained and placed; anything
    else is a published name to look up.
    """
    named = Path(setting)
    if named.suffix:
        if named.is_file():
            return named
        raise WakeUnavailable(
            f"no wake word model at {named}. Train one with scripts/train_wake_word.py, "
            f"or set wake.model to one of: {', '.join(PUBLISHED)}"
        )

    found = find_model(setting, folder)
    if found is not None:
        return found

    if setting in PUBLISHED:
        raise WakeUnavailable(
            f"the {setting!r} model is not downloaded. openWakeWord ships no models in "
            "the wheel at all. Run: ranger wake install"
        )
    raise WakeUnavailable(
        f"{setting!r} is not a model openWakeWord publishes and not a path to a file. "
        "Train it with scripts/train_wake_word.py and point wake.model at the .onnx, "
        f"or set wake.model to one of: {', '.join(PUBLISHED)}"
    )


def missing_models(setting: str, folder: Path | None = None) -> list[str]:
    """What hands free needs and has not got, most fundamental first.

    Answers `ranger doctor` at the point hands free is switched on, rather than
    letting the first arm find out. Returns [] when everything is present, and
    raises only if openWakeWord itself is missing, which is a different answer.
    """
    folder = folder if folder is not None else models_folder()
    gaps = [name for name in FEATURE_MODELS if not (folder / name).is_file()]

    named = Path(setting)
    if named.suffix:
        if not named.is_file():
            gaps.append(str(named))
    elif find_model(setting, folder) is None:
        gaps.append(f"{setting} (openWakeWord's own)")
    return gaps


def install_models(
    name: str = "hey_jarvis",
    folder: Path | None = None,
    log: Callable[[str], None] = print,
) -> list[Path]:
    """Download the feature models and one hotword. Idempotent.

    openWakeWord's downloader skips what already exists and reports success
    whatever the server said, so what is on disk afterwards is checked here
    rather than trusted.
    """
    if name not in PUBLISHED:
        raise WakeUnavailable(
            f"{name!r} is not a model openWakeWord publishes. Published: "
            f"{', '.join(PUBLISHED)}. Anything else is trained, not downloaded: "
            "see scripts/train_wake_word.py"
        )

    folder = folder if folder is not None else models_folder()
    folder.mkdir(parents=True, exist_ok=True)

    log(f"downloading from {MODEL_SOURCE}")
    log(f"into {folder}")

    from openwakeword.utils import download_models

    download_models([name], target_directory=str(folder))

    wanted = [folder / feature for feature in FEATURE_MODELS]
    hotword = find_model(name, folder)
    if hotword is None:
        raise WakeUnavailable(
            f"the download finished but there is no {name} model in {folder}. "
            "Check the network, then run it again"
        )
    wanted.append(hotword)

    for path in wanted:
        if not path.is_file():
            raise WakeUnavailable(f"{path.name} did not download into {folder}")
        size = path.stat().st_size
        if size < SMALLEST_PLAUSIBLE_MODEL:
            raise WakeUnavailable(
                f"{path.name} downloaded as {size} bytes, which is too small to be a "
                "model. That is usually a proxy or a network error page saved under the "
                f"model's name. Delete it from {folder} and run this again"
            )
    return wanted


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
                f"no wake word model at {path}. Run: ranger wake install, or train one "
                "with scripts/train_wake_word.py"
            )
        if path.suffix != ".onnx":
            raise WakeUnavailable(
                f"{path.name} is not an ONNX model. Ranger's wake extra installs "
                "onnxruntime and not tflite_runtime, so a .tflite fails several layers "
                "down inside openWakeWord rather than here"
            )

        for feature in FEATURE_MODELS:
            # openWakeWord's preprocessor finds these by a path hardcoded inside
            # its own package, and a missing one raises from onnxruntime with no
            # mention of which file. Say it here instead.
            if not (models_folder() / feature).is_file():
                raise WakeUnavailable(
                    f"{feature} is missing, and every hotword runs on top of it. "
                    "Run: ranger wake install"
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
    model = resolve_model(wake.model)

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
