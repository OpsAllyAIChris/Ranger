"""Tier 3a logic, against a fake backend.

The sandbox this was written in has no microphone, no speaker, and no
PortAudio: `import sounddevice` raises OSError there. So everything below tests
the parts that are not the hardware. Whether a real device opens, and whether
anything is audible, is the operator's to confirm and nobody else's.
"""

from __future__ import annotations

import array
import io
import math
import threading
from pathlib import Path

import pytest

from ranger.audio import (
    AudioDevice,
    AudioError,
    Levels,
    duration_seconds,
    levels,
    read_wav,
    resolve_device,
    write_wav,
)
from ranger.audiocheck import format_devices, judge, run_check
from ranger.trigger import FixedTrigger, ToggleTrigger, TriggerError, build_trigger


def tone(seconds=1.0, rate=16000, amplitude=12000):
    return array.array(
        "h",
        [int(amplitude * math.sin(2 * math.pi * 440 * t / rate)) for t in range(int(rate * seconds))],
    ).tobytes()


def silence(seconds=1.0, rate=16000):
    return b"\x00\x00" * int(rate * seconds)


DEVICES = [
    AudioDevice(0, "Microsoft Sound Mapper - Input", 2, 0, 44100, is_default_input=True),
    AudioDevice(1, "Blue Yeti Stereo Microphone", 2, 0, 48000),
    AudioDevice(2, "Headset Microphone (Jabra Evolve)", 1, 0, 16000),
    AudioDevice(3, "Speakers (Realtek High Definition)", 0, 2, 48000, is_default_output=True),
    AudioDevice(4, "Headphones (Jabra Evolve)", 0, 2, 48000),
]


class FakeBackend:
    """Stands in for PortAudio. Records whatever it was told to."""

    def __init__(self, pcm=b"", devices=None, fail_on=None):
        self.pcm = pcm
        self._devices = devices if devices is not None else DEVICES
        self.fail_on = fail_on
        self.played: list[bytes] = []
        self.record_device = None
        self.play_device = None

    def devices(self):
        if self.fail_on == "devices":
            raise AudioError("could not enumerate audio devices: fake")
        return list(self._devices)

    def record(self, *, samplerate, channels, device, stop):
        if self.fail_on == "record":
            raise AudioError("could not record audio: fake")
        self.record_device = device
        stop.wait(timeout=2)
        return self.pcm

    def play(self, pcm, *, samplerate, channels, device, stop=None):
        if self.fail_on == "play":
            raise AudioError("could not play audio: fake")
        self.play_device = device
        self.played.append(pcm)


# -- levels ----------------------------------------------------------------


def test_levels_of_a_tone():
    measured = levels(tone())
    assert measured.peak == 12000
    assert -9.0 < measured.peak_dbfs < -8.0
    assert not measured.silent and not measured.very_quiet
    assert measured.clipped == 0


def test_digital_silence_is_detected():
    measured = levels(silence())
    assert measured.silent and measured.peak == 0
    assert measured.peak_dbfs == -math.inf


def test_a_faint_signal_is_not_called_silence():
    measured = levels(tone(amplitude=60))
    assert not measured.silent
    assert measured.very_quiet


def test_clipping_is_counted():
    measured = levels(array.array("h", [32767, -32768, 0, 100]).tobytes())
    assert measured.clipped == 2


def test_levels_of_nothing():
    assert levels(b"").samples == 0
    assert levels(b"\x01").samples == 0     # odd byte, no crash


def test_duration():
    assert duration_seconds(tone(2.0), 16000, 1) == pytest.approx(2.0)
    assert duration_seconds(b"", 16000, 1) == 0.0


# -- wav -------------------------------------------------------------------


def test_wav_round_trip(tmp_path):
    original = tone(0.5)
    path = write_wav(tmp_path / "a" / "t.wav", original, samplerate=16000, channels=1)
    pcm, rate, channels = read_wav(path)
    assert pcm == original and rate == 16000 and channels == 1


def test_reading_a_non_16_bit_wav_says_so(tmp_path):
    import wave

    path = tmp_path / "eight.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(1)
        handle.setframerate(16000)
        handle.writeframes(b"\x80" * 100)
    with pytest.raises(AudioError, match="16 bit"):
        read_wav(path)


# -- choosing a device -----------------------------------------------------


def test_empty_means_the_system_default():
    assert resolve_device("", DEVICES, kind="input") is None
    assert resolve_device(None, DEVICES, kind="output") is None


def test_a_name_fragment_resolves():
    assert resolve_device("yeti", DEVICES, kind="input") == 1
    assert resolve_device("Realtek", DEVICES, kind="output") == 3


def test_an_index_resolves():
    assert resolve_device(2, DEVICES, kind="input") == 2
    assert resolve_device("2", DEVICES, kind="input") == 2


def test_an_ambiguous_fragment_is_never_guessed():
    """Half the devices on a Windows box have "Microphone" in the name."""
    with pytest.raises(AudioError) as caught:
        resolve_device("Microphone", DEVICES, kind="input")
    assert "2 input devices match" in str(caught.value)
    assert "Blue Yeti" in str(caught.value) and "Jabra" in str(caught.value)


def test_the_kind_filter_disambiguates_on_its_own():
    """"Jabra" names a microphone and a pair of headphones, so it is only
    ambiguous until you know which direction you are going."""
    assert resolve_device("Jabra", DEVICES, kind="input") == 2
    assert resolve_device("Jabra", DEVICES, kind="output") == 4


def test_an_unknown_name_says_to_list_them():
    with pytest.raises(AudioError, match="ranger audio devices"):
        resolve_device("Shure SM7B", DEVICES, kind="input")


def test_an_index_with_no_channels_of_that_kind():
    with pytest.raises(AudioError, match="no input channels"):
        resolve_device(3, DEVICES, kind="input")


def test_an_index_that_does_not_exist():
    with pytest.raises(AudioError, match="no audio device with index 99"):
        resolve_device(99, DEVICES, kind="input")


# -- the verdict -----------------------------------------------------------


def test_silence_blames_the_windows_privacy_setting():
    verdict = judge(levels(silence(3)), 3.0, 3.0)
    assert not verdict.ok
    assert "pure digital silence" in verdict.headline
    assert any("Privacy and security" in line for line in verdict.detail)


def test_a_faint_signal_gets_different_advice_from_silence():
    verdict = judge(levels(tone(3, amplitude=60)), 3.0, 3.0)
    assert not verdict.ok
    assert "faint" in verdict.headline
    assert any("device works" in line for line in verdict.detail)
    assert not any("Privacy" in line for line in verdict.detail)


def test_good_audio_passes():
    verdict = judge(levels(tone(3)), 3.0, 3.0)
    assert verdict.ok and "working" in verdict.headline


def test_clipping_is_mentioned_but_still_passes():
    loud = array.array("h", [32767, -32768] * 24000).tobytes()
    verdict = judge(levels(loud), 3.0, 3.0)
    assert verdict.ok
    assert any("clipped" in line for line in verdict.detail)


def test_nothing_captured_at_all():
    verdict = judge(levels(b""), 0.0, 3.0)
    assert not verdict.ok and "Nothing was captured" in verdict.headline


def test_a_truncated_recording_is_flagged():
    verdict = judge(levels(tone(0.5)), 0.5, 3.0)
    assert not verdict.ok and "expected about" in verdict.headline


# -- the check, end to end against the fake --------------------------------


def check(config, backend, **kwargs):
    out = io.StringIO()
    kwargs.setdefault("seconds", 0.05)
    kwargs.setdefault("use_trigger", False)
    kwargs.setdefault("keep", None)
    code = run_check(config, backend, out=out, **kwargs)
    return code, out.getvalue()


def test_check_records_measures_and_plays_back(config, tmp_path):
    backend = FakeBackend(tone(3))
    code, text = check(config, backend, keep=tmp_path / "out.wav")

    assert code == 0
    assert "The microphone is working." in text
    assert "playing it back" in text
    assert backend.played and backend.played[0] == backend.pcm
    assert (tmp_path / "out.wav").exists()


def test_check_signals_the_moment_recording_stops(config):
    """Silence must never read as broken."""
    _, text = check(config, FakeBackend(tone(1)))
    assert "listening ..." in text
    assert text.index("got it.") < text.index("playing it back")


def test_check_fails_loudly_on_silence(config):
    code, text = check(config, FakeBackend(silence(3)))
    assert code == 1
    assert "pure digital silence" in text
    assert "Let desktop apps access your microphone" in text


def test_check_reports_which_devices_it_chose(config, tmp_path):
    from dataclasses import replace

    tuned = replace(config, voice=replace(config.voice, input_device="yeti", output_device="Realtek"))
    backend = FakeBackend(tone(1))
    _, text = check(tuned, backend)
    assert "Blue Yeti" in text and "Realtek" in text
    assert backend.record_device == 1 and backend.play_device == 3


def test_check_stops_on_a_bad_device_before_recording(config):
    from dataclasses import replace

    tuned = replace(config, voice=replace(config.voice, input_device="Microphone"))
    backend = FakeBackend(tone(1))
    code, text = check(tuned, backend)
    assert code == 1
    assert "2 input devices match" in text
    assert backend.played == []


def test_check_survives_a_recording_failure(config):
    code, text = check(config, FakeBackend(tone(1), fail_on="record"))
    assert code == 1 and "could not record audio" in text


def test_check_survives_a_playback_failure(config):
    code, text = check(config, FakeBackend(tone(3), fail_on="play"))
    assert code == 1 and "could not play audio" in text


def test_check_can_use_the_trigger_instead_of_a_duration(config):
    backend = FakeBackend(tone(2))
    out = io.StringIO()
    code = run_check(
        config, backend, seconds=None, use_trigger=True, keep=None, out=out,
        trigger=FixedTrigger(0.05),
    )
    assert code == 0
    assert "recording for 0.05 seconds" in out.getvalue()


# -- devices listing -------------------------------------------------------


def test_device_listing_marks_what_ranger_will_use():
    text = format_devices(DEVICES, 1, 4)
    assert "Blue Yeti Stereo Microphone" in text
    assert "<- Jarvis records here" in text
    assert "<- Jarvis plays here" in text
    assert "system default in" in text


def test_device_listing_with_no_devices():
    assert "no audio devices found" in format_devices([], None, None)


# -- triggers --------------------------------------------------------------


def test_fixed_trigger_fires_once_then_stops():
    trigger = FixedTrigger(0.05)
    assert trigger.wait_to_start() is True
    assert trigger.wait_to_start() is False
    stop = trigger.stop_event()
    assert stop.wait(timeout=1.0)


def test_toggle_trigger_starts_and_stops_on_enter():
    lines = iter(["\n", "\n"])
    trigger = ToggleTrigger(readline=lambda: next(lines, ""))
    assert trigger.wait_to_start() is True
    assert trigger.stop_event().wait(timeout=1.0)


def test_toggle_trigger_quits_on_eof_or_quit():
    assert ToggleTrigger(readline=lambda: "").wait_to_start() is False
    assert ToggleTrigger(readline=lambda: "quit\n").wait_to_start() is False


def test_build_trigger_rejects_an_unknown_kind():
    with pytest.raises(TriggerError, match='"hold" or "toggle"'):
        build_trigger("wake-word", "space")


def test_build_trigger_with_seconds_never_needs_a_key_hook():
    """This is why 'ranger audio check' works before pynput is proven."""
    assert isinstance(build_trigger("hold", "space", seconds=1.0), FixedTrigger)


def test_hold_trigger_without_pynput_says_what_to_do():
    from ranger.trigger import HoldTrigger

    trigger = HoldTrigger("space")
    assert trigger.hint == "HOLD SPACE TO TALK"
    try:
        import pynput  # noqa: F401
    except ImportError:
        with pytest.raises(TriggerError) as caught:
            trigger.wait_to_start()
        assert "pip install pynput" in str(caught.value)
        assert 'voice.trigger = "toggle"' in str(caught.value)


def test_check_leaves_no_thread_holding_the_process_open(config):
    """A non-daemon Timer keeps Python alive until it fires.

    voice.max_seconds is 60, so a leaked one made the command look hung for a
    minute after it had already finished. Caught by the test suite taking 60
    seconds to exit while reporting that it passed in 1.6.
    """
    before = {t for t in threading.enumerate()}
    check(config, FakeBackend(tone(1)))

    leaked = [
        t for t in threading.enumerate()
        if t not in before and t.is_alive() and not t.daemon
    ]
    assert leaked == [], f"these would hold the process open: {leaked}"
