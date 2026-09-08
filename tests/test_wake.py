"""Hands free: the hotword, and everything that keeps it honest.

The operator settled against a wake word twice before reopening it, under seven
conditions. Those conditions are the specification, so this file is organised
around them rather than around the code. The model itself is never loaded here:
what needs proving is the rules, and the rules are all in the state machine.
"""

from __future__ import annotations

import math
import struct

import pytest

from ranger.micuse import Verdict, may_arm, others_using, unmangle
from ranger.wake import (
    FRAME_SAMPLES,
    SAMPLE_RATE,
    Fire,
    Hotword,
    Outcome,
    Ring,
    State,
    available,
    rms,
    strip_phrase,
    wav_of,
)

SILENCE = b"\x00\x00" * FRAME_SAMPLES
SPEECH = b"".join(struct.pack("<h", int(6000 * math.sin(i / 8))) for i in range(FRAME_SAMPLES))


class Detector:
    """Fires on the frames it is told to, and nowhere else."""

    threshold = 0.5

    def __init__(self, fire_on: set[int] | None = None, score: float = 0.9) -> None:
        self.fire_on = fire_on or set()
        self.score = score
        self.seen = 0
        self.resets = 0

    def feed(self, frame: bytes) -> float:
        self.seen += 1
        return self.score if self.seen in self.fire_on else 0.0

    def reset(self) -> None:
        self.resets += 1


def clock(step: float = 0.08):
    held = [0.0]

    def now() -> float:
        held[0] += step
        return held[0]

    return now


def run(hotword: Hotword, pattern) -> tuple[list[bytes], list[Fire]]:
    """Feed frames, collect utterances and resolved firings."""
    utterances: list[bytes] = []
    fires: list[Fire] = []
    for loud in pattern:
        audio, fire = hotword.feed(SPEECH if loud else SILENCE)
        if audio:
            utterances.append(audio)
        if fire:
            fires.append(fire)
    return utterances, fires


# -- condition 1: off by default, never persisted on -----------------------


def test_it_is_off_until_something_arms_it():
    hotword = Hotword(detector=Detector())
    assert hotword.state is State.OFF
    assert not hotword.armed
    assert hotword.feed(SPEECH) == (None, None), "a frame arriving does not arm it"


def test_the_setting_defaults_to_off(config):
    """`enabled` only decides whether the control appears. Arming is a separate
    per session act, so a restart is always off."""
    assert config.wake.enabled is False


# -- condition 4: never arms while something else has the microphone -------


def test_it_refuses_to_arm_while_another_application_has_the_microphone():
    hotword = Hotword(
        detector=Detector(),
        check_microphone=lambda: Verdict(False, ("Teams.exe", "chrome.exe")),
    )
    ok, why = hotword.arm()
    assert not ok
    assert "Teams.exe" in why and "chrome.exe" in why
    assert not hotword.armed


def test_a_call_that_starts_while_it_is_armed_blocks_the_fire_and_turns_it_off():
    """The case the check exists for. Checking only at arming would miss it."""
    busy = [False]
    hotword = Hotword(
        detector=Detector({3}),
        now=clock(),
        check_microphone=lambda: Verdict(not busy[0], ("chrome.exe",) if busy[0] else ()),
    )
    assert hotword.arm()[0]

    busy[0] = True
    _, fires = run(hotword, [False] * 6)

    assert [f.outcome for f in fires] == [Outcome.BLOCKED]
    assert fires[0].blockers == ("chrome.exe",)
    assert not hotword.armed, "it turns itself off rather than waiting"


def test_a_blocked_fire_says_which_application_took_it():
    fire = Fire(0.0, Outcome.BLOCKED, 0.81, blockers=("Teams.exe",))
    said = fire.describe("hey ranger")
    assert "Teams.exe" in said and "nothing was recorded" in said


# -- condition 5: two words only -------------------------------------------


def test_a_one_word_phrase_is_refused_by_the_loader(make_config, config_file, tmp_path):
    from ranger.config import ConfigError, load_config

    path = config_file()
    path.write_text(
        path.read_text(encoding="utf-8") + '\n[wake]\nphrase = "ranger"\n', encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="one word"):
        load_config(path, load_env=False)


# -- condition 6: it disarms itself ----------------------------------------


def test_it_disarms_after_a_period_with_no_interaction():
    """The failure the operator said they are most likely to create."""
    hotword = Hotword(detector=Detector(), idle_disarm_seconds=1.0, now=clock())
    hotword.arm()
    run(hotword, [False] * 40)
    assert hotword.state is State.OFF


def test_speaking_to_it_starts_the_idle_clock_again():
    hotword = Hotword(detector=Detector({2}), idle_disarm_seconds=1.5, now=clock())
    hotword.arm()
    run(hotword, [False, False, True, True, True] + [False] * 15)
    assert hotword.armed, "a real interaction should keep it alive"


# -- what happens to a partial utterance -----------------------------------


def test_a_fire_with_nothing_after_it_is_discarded_and_never_sent():
    """Silence costs money and transcribes to nothing."""
    hotword = Hotword(detector=Detector({2}), grace_seconds=1.0, now=clock())
    hotword.arm()
    utterances, fires = run(hotword, [False] * 30)

    assert utterances == [], "nothing may be sent"
    assert [f.outcome for f in fires] == [Outcome.NO_SPEECH]
    assert hotword.state is State.ARMED, "and it keeps listening"


def test_the_grace_period_lets_the_operator_think_before_speaking():
    """Three seconds, because they said they would say the phrase and think."""
    hotword = Hotword(detector=Detector({2}), grace_seconds=3.0, now=clock())
    hotword.arm()
    # Two and a half seconds of nothing, then speech.
    utterances, fires = run(hotword, [False] * 30 + [True] * 12 + [False] * 20)
    assert len(utterances) == 1
    assert [f.outcome for f in fires] == [Outcome.SPOKE]


def test_an_utterance_ends_on_silence_rather_than_a_fixed_window():
    hotword = Hotword(detector=Detector({2}), silence_seconds=0.4, now=clock())
    hotword.arm()
    utterances, fires = run(hotword, [False, True] + [True] * 20 + [False] * 12)
    assert len(utterances) == 1
    assert fires[0].outcome is Outcome.SPOKE


def test_an_utterance_that_never_stops_hits_a_ceiling_and_is_sent_anyway():
    hotword = Hotword(detector=Detector({2}), max_seconds=1.0, now=clock())
    hotword.arm()
    utterances, fires = run(hotword, [False] + [True] * 40)
    assert len(utterances) == 1
    assert fires[0].outcome is Outcome.TOO_LONG


# -- the pre-roll ----------------------------------------------------------


def test_the_recording_starts_before_the_phrase_fired():
    """Detection lags the phrase, and people run it into the request. Without
    a rolling buffer the first word after the phrase is already gone."""
    hotword = Hotword(detector=Detector({10}), preroll_seconds=0.8, now=clock())
    hotword.arm()
    utterances, _ = run(hotword, [False] * 9 + [True] * 12 + [False] * 20)

    seconds = len(utterances[0]) / 2 / SAMPLE_RATE
    assert seconds > 1.0, "the utterance must include audio from before the fire"


def test_the_ring_keeps_only_what_it_is_asked_to():
    ring = Ring(frames=3)
    for index in range(10):
        ring.push(bytes([index]))
    assert ring.drain() == bytes([7, 8, 9])
    assert ring.drain() == b"", "draining empties it"


# -- taking the phrase off the front ---------------------------------------


@pytest.mark.parametrize(
    "heard, expected",
    [
        ("hey ranger where are we on Illes", "where are we on Illes"),
        ("Hey Ranger, where are we on Illes", "where are we on Illes"),
        ("HEY RANGER where are we", "where are we"),
        ("hey ranger. draft a note", "draft a note"),
        ("ranger where are we", "where are we"),
        ("where are we on Illes", "where are we on Illes"),
        ("hey ranger", ""),
    ],
)
def test_the_wake_phrase_is_stripped_as_text(heard, expected):
    """Text, never audio. The audio boundary is a guess and guessing it wrong
    eats the first word of the request."""
    assert strip_phrase(heard, "hey ranger") == expected


# -- the microphone check --------------------------------------------------


def test_a_desktop_application_path_becomes_a_program_name():
    """The full path is the operator's software inventory and has no business
    in a log or on a screen."""
    assert unmangle(r"C:#Program Files#Google#Chrome#Application#chrome.exe") == "chrome.exe"


def test_only_applications_currently_holding_the_microphone_count():
    store = [
        ("chrome.exe", False, 0),        # in use now
        ("Teams.exe", False, 133_000),   # used earlier
        ("Zoom.exe", False, 0),          # in use now
    ]
    assert others_using(lambda: store) == ["Zoom.exe", "chrome.exe"]


def test_rangers_own_capture_is_not_somebody_else():
    store = [("python.exe", False, 0), ("pythonw.exe", False, 0), ("ranger.exe", False, 0)]
    assert may_arm(lambda: store).allowed


def test_the_rule_is_strict_about_everything_that_is_not_ranger():
    """Most of the operator's meetings are browser calls, so lenient would
    leave uncovered exactly the case this exists for."""
    assert not may_arm(lambda: [("chrome.exe", False, 0)]).allowed
    assert not may_arm(lambda: [("ms-teams.exe", True, 0)]).allowed


def test_an_application_can_be_ignored_by_name():
    store = [("chrome.exe", False, 0)]
    assert may_arm(lambda: store, ignore=("chrome.exe",)).allowed


# -- the odds and ends -----------------------------------------------------


def test_loudness_tells_speech_from_silence():
    assert rms(SILENCE) == 0.0
    assert rms(SPEECH) > 1000
    assert rms(b"") == 0.0
    assert rms(b"\x01") == 0.0, "a half sample is not a crash"


def test_captured_audio_is_wrapped_with_a_header():
    """The same lesson as pcm_24000 in the browser: raw samples say nothing
    about themselves, and everything downstream would need telling separately."""
    import io
    import wave

    wav = wav_of(SPEECH)
    with wave.open(io.BytesIO(wav)) as read:
        assert read.getframerate() == SAMPLE_RATE
        assert read.getnchannels() == 1
        assert read.getsampwidth() == 2
        assert read.readframes(FRAME_SAMPLES) == SPEECH


def test_it_says_why_it_cannot_run_rather_than_failing_obscurely():
    ready, why = available()
    assert isinstance(ready, bool)
    if not ready:
        # The exact command, because "ranger[wake]" resolves the installed
        # package and succeeds having done nothing.
        assert 'pip install -e ".[wake]"' in why


def test_the_detector_is_reset_between_utterances():
    """Otherwise the tail of one utterance can trigger the next fire."""
    detector = Detector({2})
    hotword = Hotword(detector=detector, now=clock())
    hotword.arm()
    run(hotword, [False, True] + [True] * 8 + [False] * 20)
    assert detector.resets >= 1


def test_disarming_forgets_everything_it_had_buffered():
    hotword = Hotword(detector=Detector({2}), now=clock())
    hotword.arm()
    run(hotword, [False, True, True])
    hotword.disarm()
    assert hotword.state is State.OFF
    assert hotword._captured == []


# -- the check fails closed ------------------------------------------------


def test_a_check_that_cannot_run_refuses_rather_than_allows():
    """The whole mitigation for a wake word firing during a customer call.

    A check that cannot run has found nothing, not found nobody. The operator's
    words: they would rather it never work than work while they are on a call.
    """
    from ranger.micuse import Unreadable, may_arm

    def broken():
        raise Unreadable("the key is not there")

    verdict = may_arm(broken)
    assert not verdict.allowed
    assert verdict.unreadable
    assert "could not run" in verdict.reason


def test_it_refuses_where_there_is_no_registry_to_read_at_all():
    from ranger.micuse import may_arm

    verdict = may_arm()
    assert not verdict.allowed, "no consent store means nothing is known"
    assert "not Windows" in verdict.reason


def test_an_empty_store_is_a_real_answer_and_a_missing_one_is_not():
    from ranger.micuse import Unreadable, consumers, may_arm

    assert may_arm(lambda: []).allowed, "opened, nothing in use"
    with pytest.raises(Unreadable):
        consumers()


def test_hands_free_will_not_arm_when_the_check_failed():
    from ranger.micuse import Unreadable, may_arm

    def broken():
        raise Unreadable("winreg is not available")

    hotword = Hotword(detector=Detector(), check_microphone=lambda: may_arm(broken))
    ok, why = hotword.arm()
    assert not ok
    assert "could not run" in why
    assert not hotword.armed


# -- the periodic check ----------------------------------------------------


def test_a_call_starting_while_armed_disarms_without_waiting_for_a_fire():
    """Arming and then joining a call must not leave the microphone held for
    the whole call because the phrase happened never to fire."""
    from ranger.handsfree import Listener

    busy = [False]
    hotword = Hotword(
        detector=Detector(),
        now=clock(),
        check_microphone=lambda: Verdict(not busy[0], ("chrome.exe",) if busy[0] else ()),
    )
    assert hotword.arm()[0]

    fires: list[Fire] = []
    states: list[State] = []

    def frames():
        for index in range(200):
            if index == 5:
                busy[0] = True
            yield SILENCE

    listener = Listener(
        hotword=hotword,
        frames=frames,
        on_utterance=lambda audio: None,
        on_fire=fires.append,
        on_state=states.append,
        check_seconds=0.0,   # every frame, so the test does not sleep
    )
    listener._run()

    assert not hotword.armed
    assert State.OFF in states
    assert fires and fires[-1].outcome is Outcome.BLOCKED
    assert "chrome.exe" in fires[-1].blockers[0]


def test_a_microphone_check_that_starts_erroring_also_disarms():
    from ranger.handsfree import Listener

    calls = [0]

    def check():
        calls[0] += 1
        if calls[0] > 2:
            raise RuntimeError("the registry went away")
        return Verdict(True)

    hotword = Hotword(detector=Detector(), now=clock(), check_microphone=check)
    hotword.arm()

    fires: list[Fire] = []
    listener = Listener(
        hotword=hotword,
        frames=lambda: iter([SILENCE] * 50),
        on_utterance=lambda audio: None,
        on_fire=fires.append,
        check_seconds=0.0,
    )
    listener._run()

    assert not hotword.armed
    assert fires and "stopped working" in fires[-1].blockers[0]
