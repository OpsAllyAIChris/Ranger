"""'ranger audio devices' and 'ranger audio check'.

Tier 3a. No transcription, no network, no API keys. If this fails the problem
is a device or a driver, and the report below is built to say which.

The verdict matters more than the recording. Silence is the most common failure
on Windows and it throws no exception: the microphone privacy setting is off,
the stream opens, and every sample comes back zero. So this measures the audio
and says what it found rather than leaving the operator to guess.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from .audio import (
    AudioBackend,
    AudioError,
    Levels,
    duration_seconds,
    levels,
    resolve_device,
    write_wav,
)
from .config import Config
from .trigger import Trigger, TriggerError, build_trigger


def format_devices(devices: list, input_index: int | None, output_index: int | None) -> str:
    if not devices:
        return "  no audio devices found at all."
    lines = ["  idx  in out   rate  name", "  ---  -- ---  -----  " + "-" * 40]
    for d in devices:
        marks = []
        if d.is_default_input:
            marks.append("system default in")
        if d.is_default_output:
            marks.append("system default out")
        if d.index == input_index:
            marks.append("<- Ranger records here")
        if d.index == output_index:
            marks.append("<- Ranger plays here")
        note = ("   " + ", ".join(marks)) if marks else ""
        lines.append(
            f"  {d.index:>3}  {d.max_input_channels:>2} {d.max_output_channels:>3}  "
            f"{int(d.default_samplerate):>5}  {d.name}{note}"
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    headline: str
    detail: tuple[str, ...] = ()


def judge(measured: Levels, seconds: float, expected: float) -> Verdict:
    """Turn the numbers into something worth reading."""
    if measured.samples == 0:
        return Verdict(
            False,
            "Nothing was captured at all.",
            (
                "The stream opened but produced no frames.",
                "Try another input device, or a different voice.sample_rate.",
            ),
        )

    if seconds < expected * 0.5:
        return Verdict(
            False,
            f"Only {seconds:.1f}s of audio arrived, expected about {expected:.1f}s.",
            ("The device may have been taken by another application mid-recording.",),
        )

    if measured.silent:
        return Verdict(
            False,
            "The microphone produced pure digital silence.",
            (
                "Every sample was zero, which is not a quiet room, it is no signal.",
                "On Windows: Settings, Privacy and security, Microphone, and turn on",
                "'Let desktop apps access your microphone'. It fails exactly like this",
                "when off, with no error.",
                "If that is already on, the wrong input device is selected.",
            ),
        )

    if measured.very_quiet:
        return Verdict(
            False,
            f"Signal is very faint, peaking at {measured.peak_dbfs:.0f} dBFS.",
            (
                "Something was captured, so the device works and the privacy setting is on.",
                "Raise the input level in Windows sound settings, or move closer to the mic.",
                "Speech should peak somewhere around -20 to -6 dBFS.",
            ),
        )

    detail = [
        f"Peak {measured.peak_dbfs:.1f} dBFS, average {measured.rms_dbfs:.1f} dBFS, "
        f"{seconds:.1f}s captured."
    ]
    if measured.clipped:
        detail.append(
            f"{measured.clipped} samples clipped. Lower the input level a little; "
            "clipping hurts transcription accuracy."
        )
    return Verdict(True, "The microphone is working.", tuple(detail))


def run_check(
    config: Config,
    backend: AudioBackend,
    *,
    seconds: float | None,
    use_trigger: bool,
    keep: Path | None,
    out,
    trigger: Trigger | None = None,
) -> int:
    """Record, measure, save, play back. The whole of Tier 3a."""
    voice = config.voice

    def say(text: str = "") -> None:
        print(text, file=out, flush=True)

    try:
        devices = backend.devices()
        input_index = resolve_device(voice.input_device, devices, kind="input")
        output_index = resolve_device(voice.output_device, devices, kind="output")
    except AudioError as exc:
        say(f"  {exc}")
        return 1

    chosen_in = next((d for d in devices if d.index == input_index), None)
    chosen_out = next((d for d in devices if d.index == output_index), None)
    say(f"  recording from  {chosen_in.name if chosen_in else 'system default'}")
    say(f"  playing back to {chosen_out.name if chosen_out else 'system default'}")
    say(f"  {voice.sample_rate} Hz, {voice.channels} channel, 16 bit")
    say()

    if trigger is None:
        try:
            trigger = (
                build_trigger(voice.trigger, voice.key)
                if use_trigger
                else build_trigger("hold", voice.key, seconds=seconds or 3.0)
            )
        except TriggerError as exc:
            say(f"  {exc}")
            return 1

    expected = seconds or 3.0
    ceiling: threading.Timer | None = None
    try:
        say(f"  {trigger.hint}")
        if not trigger.wait_to_start():
            say("  nothing recorded.")
            return 1

        stop = trigger.stop_event()
        # A stuck key must not record all afternoon. Daemon, and cancelled in
        # the finally: a live non-daemon timer holds the whole process open
        # until it fires, so the command would look hung for a minute after it
        # had actually finished.
        ceiling = threading.Timer(voice.max_seconds, stop.set)
        ceiling.daemon = True
        ceiling.start()
        say("  listening ...")

        pcm = backend.record(
            samplerate=voice.sample_rate,
            channels=voice.channels,
            device=input_index,
            stop=stop,
        )
        # The instant the key comes up, before anything slow happens, so silence
        # never reads as broken.
        say("  got it.")
    except AudioError as exc:
        say(f"  {exc}")
        return 1
    finally:
        if ceiling is not None:
            ceiling.cancel()
        trigger.close()

    measured = levels(pcm)
    length = duration_seconds(pcm, voice.sample_rate, voice.channels)
    if use_trigger:
        expected = max(length, 0.1)

    verdict = judge(measured, length, expected)
    say()
    say(f"  {verdict.headline}")
    for line in verdict.detail:
        say(f"    {line}")

    if keep:
        try:
            written = write_wav(keep, pcm, samplerate=voice.sample_rate, channels=voice.channels)
            say(f"    saved {written}")
        except OSError as exc:
            say(f"    could not save the recording: {exc}")

    if not pcm:
        return 1

    say()
    say("  playing it back ...")
    try:
        backend.play(
            pcm, samplerate=voice.sample_rate, channels=voice.channels, device=output_index
        )
    except AudioError as exc:
        say(f"  {exc}")
        return 1

    say()
    if verdict.ok:
        say("  If you heard yourself, 3a is done. If you heard nothing, the microphone")
        say("  is fine and the problem is the output device or the volume.")
    return 0 if verdict.ok else 1
