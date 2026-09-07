"""Audio in and out, behind a seam.

Tier 3a: record and play back raw audio. No transcription, no network, no keys.
If this step does not work nothing after it can, and every failure here is a
device or a driver rather than an API.

sounddevice is imported lazily and never at module import time, so the rest of
Ranger keeps working on a machine with no audio stack at all. The backend is a
Protocol so the logic below is testable without a microphone, which matters
because the sandbox this was written in has neither a microphone nor PortAudio.

Nothing here uses audioop: it was removed in Python 3.13.
"""

from __future__ import annotations

import array
import math
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

SAMPLE_WIDTH = 2  # int16 PCM everywhere


class AudioError(Exception):
    """Something about the audio device, said in a sentence."""


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float
    is_default_input: bool = False
    is_default_output: bool = False

    @property
    def can_record(self) -> bool:
        return self.max_input_channels > 0

    @property
    def can_play(self) -> bool:
        return self.max_output_channels > 0


@runtime_checkable
class AudioBackend(Protocol):
    def devices(self) -> list[AudioDevice]: ...

    def record(
        self, *, samplerate: int, channels: int, device: int | None, stop: threading.Event
    ) -> bytes: ...

    def play(
        self, pcm: bytes, *, samplerate: int, channels: int, device: int | None,
        stop: threading.Event | None = None,
    ) -> None: ...


# -- choosing a device ------------------------------------------------------


def resolve_device(spec: str | int | None, devices: list[AudioDevice], *, kind: str) -> int | None:
    """Turn a config value into a device index.

    Accepts an index, or a case-insensitive substring of the device name.
    Prefer the name: indices shuffle when a USB microphone is plugged in or a
    Bluetooth headset connects, and a stale index silently records from the
    wrong thing. An empty value means the Windows default.

    More than one match is a question, not a guess, exactly as with account
    names.
    """
    usable = [d for d in devices if (d.can_record if kind == "input" else d.can_play)]

    if spec is None or (isinstance(spec, str) and not spec.strip()):
        return None  # the system default

    if isinstance(spec, int) or (isinstance(spec, str) and spec.strip().lstrip("-").isdigit()):
        index = int(spec)
        match = next((d for d in devices if d.index == index), None)
        if match is None:
            raise AudioError(f"no audio device with index {index}. Run 'ranger audio devices'.")
        if match not in usable:
            raise AudioError(f"device {index} ({match.name}) has no {kind} channels.")
        return index

    wanted = str(spec).strip().lower()
    hits = [d for d in usable if wanted in d.name.lower()]
    if not hits:
        raise AudioError(
            f"no {kind} device matching {spec!r}. Run 'ranger audio devices' to see the list."
        )
    if len(hits) > 1:
        listing = "\n".join(f"  {d.index}: {d.name}" for d in hits)
        raise AudioError(
            f"{len(hits)} {kind} devices match {spec!r}. Narrow it or use the index:\n{listing}"
        )
    return hits[0].index


# -- is there actually any sound in here ------------------------------------


@dataclass(frozen=True)
class Levels:
    """What a recording sounds like, in numbers, before anyone listens to it."""

    samples: int
    peak: int
    rms: float
    clipped: int

    @property
    def seconds_at(self) -> float:
        return 0.0

    @property
    def peak_dbfs(self) -> float:
        return _dbfs(self.peak)

    @property
    def rms_dbfs(self) -> float:
        return _dbfs(self.rms)

    @property
    def silent(self) -> bool:
        """Digital silence. Not "quiet": nothing at all."""
        return self.peak == 0

    @property
    def very_quiet(self) -> bool:
        return not self.silent and self.peak_dbfs < -45


def _dbfs(value: float) -> float:
    if value <= 0:
        return -math.inf
    return 20 * math.log10(min(value, 32768) / 32768)


def levels(pcm: bytes) -> Levels:
    usable = len(pcm) - (len(pcm) % SAMPLE_WIDTH)
    samples = array.array("h")
    samples.frombytes(pcm[:usable])
    if not samples:
        return Levels(samples=0, peak=0, rms=0.0, clipped=0)

    peak = max(max(samples), -min(samples))
    total = 0
    clipped = 0
    for value in samples:
        total += value * value
        if value >= 32767 or value <= -32768:
            clipped += 1
    return Levels(
        samples=len(samples),
        peak=min(peak, 32768),
        rms=math.sqrt(total / len(samples)),
        clipped=clipped,
    )


def duration_seconds(pcm: bytes, samplerate: int, channels: int) -> float:
    frames = len(pcm) / (SAMPLE_WIDTH * max(1, channels))
    return frames / samplerate if samplerate else 0.0


# -- wav on disk ------------------------------------------------------------


def write_wav(path: Path, pcm: bytes, *, samplerate: int, channels: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(SAMPLE_WIDTH)
        handle.setframerate(samplerate)
        handle.writeframes(pcm)
    return path


def read_wav(path: Path) -> tuple[bytes, int, int]:
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != SAMPLE_WIDTH:
            raise AudioError(
                f"{path} is {handle.getsampwidth() * 8} bit; Ranger works in 16 bit PCM."
            )
        return handle.readframes(handle.getnframes()), handle.getframerate(), handle.getnchannels()


# -- the real backend -------------------------------------------------------


def _sounddevice() -> Any:
    try:
        import sounddevice
    except OSError as exc:
        # The generic wheel carries no PortAudio. On Windows the right wheel
        # bundles it, so this usually means a wrong or partial install.
        raise AudioError(
            f"sounddevice is installed but PortAudio is not available ({exc}). On Windows, "
            "reinstall it so pip picks the platform wheel: "
            'pip install --force-reinstall --only-binary :all: sounddevice'
        ) from exc
    except ImportError as exc:
        raise AudioError(
            "sounddevice is not installed. Run: pip install sounddevice numpy soundfile pynput. "
            "Do not install PyAudio; it has no wheel for Python 3.14 and builds from source."
        ) from exc
    return sounddevice


class SoundDeviceBackend:
    """PortAudio, via sounddevice. The only part of Tier 3a that touches hardware."""

    def __init__(self, blocksize: int = 1024) -> None:
        self.blocksize = blocksize
        self._sd = _sounddevice()

    def devices(self) -> list[AudioDevice]:
        sd = self._sd
        try:
            raw = sd.query_devices()
            default_in, default_out = sd.default.device
        except Exception as exc:
            raise AudioError(f"could not enumerate audio devices: {exc}") from exc

        found: list[AudioDevice] = []
        for index, item in enumerate(raw):
            found.append(
                AudioDevice(
                    index=index,
                    name=str(item.get("name", f"device {index}")),
                    max_input_channels=int(item.get("max_input_channels", 0)),
                    max_output_channels=int(item.get("max_output_channels", 0)),
                    default_samplerate=float(item.get("default_samplerate", 0) or 0),
                    is_default_input=(index == default_in),
                    is_default_output=(index == default_out),
                )
            )
        return found

    def record(
        self, *, samplerate: int, channels: int, device: int | None, stop: threading.Event
    ) -> bytes:
        sd = self._sd
        chunks: list[bytes] = []

        def callback(indata, frames, time_info, status):  # noqa: ANN001
            chunks.append(bytes(indata))

        try:
            with sd.RawInputStream(
                samplerate=samplerate,
                channels=channels,
                dtype="int16",
                device=device,
                blocksize=self.blocksize,
                callback=callback,
            ):
                stop.wait()
        except Exception as exc:
            raise AudioError(_device_hint("record", exc)) from exc
        return b"".join(chunks)

    def play(
        self, pcm: bytes, *, samplerate: int, channels: int, device: int | None,
        stop: threading.Event | None = None,
    ) -> None:
        sd = self._sd
        frame = SAMPLE_WIDTH * max(1, channels)
        step = self.blocksize * frame
        try:
            with sd.RawOutputStream(
                samplerate=samplerate,
                channels=channels,
                dtype="int16",
                device=device,
                blocksize=self.blocksize,
            ) as stream:
                for start in range(0, len(pcm), step):
                    # Checked every block, which is what makes barge-in possible
                    # in 3d without changing this method.
                    if stop is not None and stop.is_set():
                        return
                    stream.write(pcm[start : start + step])
        except Exception as exc:
            raise AudioError(_device_hint("play", exc)) from exc


def _device_hint(action: str, exc: Exception) -> str:
    text = f"could not {action} audio: {exc}"
    lowered = str(exc).lower()
    if "invalid" in lowered and "sample rate" in lowered:
        return text + ". Try voice.sample_rate = 48000, or another device."
    if action == "record":
        return (
            text
            + ". On Windows check Settings, Privacy and security, Microphone, and make sure "
            "'Let desktop apps access your microphone' is on. It fails silently when off."
        )
    return text
