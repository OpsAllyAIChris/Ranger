"""Tier 3d, against fakes for every part that touches the world.

No microphone, no speaker, no Deepgram, no ElevenLabs. What is testable is the
orchestration: that the transcript reaches the same core entry point a typed
turn uses, that the signal comes before the slow work, that speech starts on
the first sentence rather than the whole reply, that a press stops the speech,
and that recording never overlaps speaking.
"""

from __future__ import annotations

import array
import asyncio
import io
import math
import shutil
import threading
from pathlib import Path

import pytest

from ranger.core import Ranger
from ranger.speech import SentenceStream
from ranger.stt import Transcript, TranscriptionError, Word
from ranger.testing import ScriptedProvider
from ranger.toolset import build_registry
from ranger.tts import SpeechError
from ranger.vault import Vault
from ranger.voiceloop import VoiceLoop

FIXTURES = Path(__file__).parent / "fixtures" / "vault"


def tone(seconds=1.0, rate=16000):
    return array.array(
        "h", [int(9000 * math.sin(2 * math.pi * 440 * t / rate)) for t in range(int(rate * seconds))]
    ).tobytes()


class FakeTrigger:
    """Fires a set number of times, then quits."""

    hint = "HOLD SPACE TO TALK"

    def __init__(self, presses=1, hold_for=0.01, quit_after=0.25):
        self.presses = presses
        self.hold_for = hold_for
        self.quit_after = quit_after
        self.used = 0
        self.released = threading.Event()

    def wait_to_start(self):
        if self.used >= self.presses:
            # A real trigger blocks until a key goes down. Returning instantly
            # would fake a quit arriving mid-sentence on every turn.
            import time

            time.sleep(self.quit_after)
            return False
        self.used += 1
        return True

    def stop_event(self):
        stop = threading.Event()
        timer = threading.Timer(self.hold_for, stop.set)
        timer.daemon = True
        timer.start()
        return stop

    def close(self):
        return None


class SlowTrigger(FakeTrigger):
    """A press that arrives partway through the speech, i.e. an interruption."""

    def __init__(self, first_delay=0.0, second_delay=0.05):
        super().__init__(presses=2)
        self.delays = [first_delay, second_delay]

    def wait_to_start(self):
        import time

        if self.used >= self.presses:
            time.sleep(self.quit_after)
            return False

        time.sleep(self.delays[self.used])
        self.used += 1
        return True


class FakeBackend:
    def __init__(self, pcm=None):
        self.pcm = tone() if pcm is None else pcm
        self.played: list[bytes] = []
        self.events: list[str] = []
        self.play_delay = 0.02

    def devices(self):
        return []

    def record(self, *, samplerate, channels, device, stop):
        self.events.append("record start")
        stop.wait(timeout=2)
        self.events.append("record stop")
        return self.pcm

    def play(self, pcm, *, samplerate, channels, device, stop=None):
        self.events.append("play start")
        import time

        waited = 0.0
        while waited < self.play_delay:
            if stop is not None and stop.is_set():
                self.events.append("play stopped")
                return
            time.sleep(0.005)
            waited += 0.005
        self.played.append(pcm)
        self.events.append("play done")


class FakeTranscriber:
    def __init__(self, text="where are we on Illes Foods", words=(), fail=None):
        self.text = text
        self.words = words
        self.fail = fail
        self.calls = 0

    async def transcribe(self, wav, *, hints=True):
        self.calls += 1
        if self.fail:
            raise TranscriptionError(self.fail)
        return Transcript(text=self.text, confidence=0.98, words=self.words, latency_ms=42)


class FakeSpeaker:
    def __init__(self, fail=None):
        self.said: list[str] = []
        self.fail = fail

    async def stream(self, text):
        self.said.append(text)
        if self.fail:
            raise SpeechError(self.fail)
        yield b"\x00\x01" * 200


@pytest.fixture
def loop_parts(vault_root, config):
    for name in ("Accounts", "Knowledge"):
        shutil.rmtree(vault_root / name, ignore_errors=True)
        shutil.copytree(FIXTURES / name, vault_root / name)
    vault = Vault(config.vault)

    def build(script, trigger=None, transcriber=None, speaker=None, backend=None):
        agent = Ranger(
            config=config,
            provider=ScriptedProvider(script),
            registry=build_registry(config, vault),
            vault=vault,
        )
        out = io.StringIO()
        parts = {
            "backend": backend or FakeBackend(),
            "transcriber": transcriber or FakeTranscriber(),
            "speaker": speaker or FakeSpeaker(),
            "trigger": trigger or FakeTrigger(),
            "out": out,
        }
        return VoiceLoop(agent=agent, config=config, **parts), out, parts

    return build


# -- the core is not forked ------------------------------------------------


async def test_the_transcript_goes_through_the_same_core_entry_point(loop_parts):
    """Amendment A. A spoken turn is a typed turn with different ends."""
    loop, out, parts = loop_parts([{"text": "Rod owes you volumes."}])
    await loop.run()

    assert loop.agent.messages[0] == {
        "role": "user",
        "content": "where are we on Illes Foods",
    }
    assert loop.turns == 1


async def test_a_spoken_turn_can_call_a_tool(loop_parts):
    loop, out, parts = loop_parts(
        [
            {"tools": [{"name": "account_recall", "input": {"account": "Illes"}}]},
            {"text": "Rod owes you confirmed volumes."},
        ]
    )
    await loop.run()
    text = out.getvalue()
    assert "-> account_recall" in text
    assert "<- account_recall ok" in text


# -- what the operator sees ------------------------------------------------


async def test_the_transcript_is_shown_next_to_the_reply(loop_parts):
    loop, out, _ = loop_parts([{"text": "Rod owes you volumes."}])
    await loop.run()
    text = out.getvalue()

    assert "you:" in text and "where are we on Illes Foods" in text
    assert "ranger:" in text and "Rod owes you volumes." in text
    assert text.index("you:") < text.index("ranger:")


async def test_something_is_printed_the_instant_the_key_comes_up(loop_parts):
    """Transcription takes most of a second and silence there reads as broken."""
    loop, out, _ = loop_parts([{"text": "ok."}])
    await loop.run()
    text = out.getvalue()
    assert "..." in text
    assert text.index("...") < text.index("you:")


async def test_low_confidence_words_are_surfaced(loop_parts):
    words = (Word("Ellis", confidence=0.41), Word("Foods", confidence=0.99))
    loop, out, _ = loop_parts(
        [{"text": "ok."}], transcriber=FakeTranscriber("where are we on Ellis Foods", words)
    )
    await loop.run()
    assert "least certain: Ellis (41%)" in out.getvalue()


async def test_timings_are_reported_for_every_turn(loop_parts):
    loop, out, _ = loop_parts([{"text": "ok."}])
    await loop.run()
    text = out.getvalue()
    assert "said |" in text and "heard" in text and "first word" in text and "first sound" in text


# -- speaking ---------------------------------------------------------------


async def test_speech_starts_on_the_first_sentence_not_the_whole_reply(loop_parts):
    speaker = FakeSpeaker()
    loop, _, _ = loop_parts(
        [{"text": "Rod owes you confirmed volumes. Marcy has not replied yet. Want the detail?"}],
        speaker=speaker,
    )
    await loop.run()
    assert len(speaker.said) >= 2
    assert speaker.said[0] == "Rod owes you confirmed volumes."


async def test_a_short_reply_is_still_spoken(loop_parts):
    speaker = FakeSpeaker()
    loop, _, _ = loop_parts([{"text": "No."}], speaker=speaker)
    await loop.run()
    assert speaker.said == ["No."]


async def test_nothing_is_played_when_the_reply_is_empty(loop_parts):
    speaker = FakeSpeaker()
    loop, _, _ = loop_parts([{"text": ""}], speaker=speaker)
    await loop.run()
    assert speaker.said == []


# -- interruption -----------------------------------------------------------


async def test_a_press_during_speech_stops_it(loop_parts):
    backend = FakeBackend()
    backend.play_delay = 1.0
    speaker = FakeSpeaker()
    loop, out, _ = loop_parts(
        [{"text": "One sentence here. Two sentence here. Three sentence here."},
         {"text": "Second turn."}],
        trigger=SlowTrigger(first_delay=0.0, second_delay=0.05),
        backend=backend,
        speaker=speaker,
    )
    await loop.run()

    assert "play stopped" in backend.events
    assert "(cut off)" in out.getvalue()


async def test_recording_never_overlaps_speaking(loop_parts):
    """It must not listen to itself."""
    backend = FakeBackend()
    backend.play_delay = 0.05
    loop, _, _ = loop_parts(
        [{"text": "One sentence here. Two sentence here."}, {"text": "Second."}],
        trigger=SlowTrigger(first_delay=0.0, second_delay=0.02),
        backend=backend,
    )
    await loop.run()

    speaking = False
    for event in backend.events:
        if event == "play start":
            speaking = True
        elif event in ("play done", "play stopped"):
            speaking = False
        elif event == "record start":
            assert not speaking, f"recording began while speaking: {backend.events}"


# -- failures ---------------------------------------------------------------


async def test_a_transcription_failure_does_not_end_the_loop(loop_parts):
    loop, out, _ = loop_parts(
        [{"text": "never reached"}],
        transcriber=FakeTranscriber(fail="Deepgram rejected the API key."),
        trigger=FakeTrigger(presses=2),
    )
    await loop.run()
    assert "Deepgram rejected the API key." in out.getvalue()
    assert loop.turns == 0


async def test_silence_is_reported_rather_than_transcribed(loop_parts):
    transcriber = FakeTranscriber()
    loop, out, _ = loop_parts(
        [{"text": "never reached"}], backend=FakeBackend(pcm=b"\x00\x00" * 16000),
        transcriber=transcriber,
    )
    await loop.run()
    text = out.getvalue()
    # The numbers have to be in the message: a missed key press, a mic that
    # never opened and speaking too quietly are indistinguishable without them.
    assert "1.0s captured, silent" in text
    assert "Let desktop apps access your microphone" in text
    assert transcriber.calls == 0


async def test_an_empty_transcript_is_reported(loop_parts):
    loop, out, _ = loop_parts([{"text": "x"}], transcriber=FakeTranscriber(text="   "))
    await loop.run()
    text = out.getvalue()
    assert "nothing usable in that" in text
    assert "1.0s captured" in text and "dBFS peak" in text
    assert "transcription finding no speech" in text


async def test_a_speech_failure_does_not_end_the_loop(loop_parts):
    loop, out, _ = loop_parts(
        [{"text": "Rod owes you volumes."}],
        speaker=FakeSpeaker(fail="ElevenLabs rejected the API key."),
        trigger=FakeTrigger(presses=2),
    )
    await loop.run()
    assert "ElevenLabs rejected the API key." in out.getvalue()
    # The reply was still shown, so the turn was not wasted.
    assert "Rod owes you volumes." in out.getvalue()


# -- sentence splitting -----------------------------------------------------


def test_sentences_come_out_whole():
    stream = SentenceStream()
    assert stream.feed("Rod owes you confirmed volumes before pricing. ") == [
        "Rod owes you confirmed volumes before pricing."
    ]


def test_an_abbreviation_is_not_a_sentence_end():
    stream = SentenceStream()
    got = stream.feed("I spoke to Dr. Vance at Illes Foods Inc. about the retort line. Next?")
    assert got == ["I spoke to Dr. Vance at Illes Foods Inc. about the retort line."]


def test_a_decimal_is_not_a_sentence_end():
    stream = SentenceStream()
    assert stream.feed("Waste runs at 9.2 percent on that line. ") == [
        "Waste runs at 9.2 percent on that line."
    ]


def test_an_initial_is_not_a_sentence_end():
    stream = SentenceStream()
    assert stream.feed("Ask J. Smith about the quote when you get a chance. ") == [
        "Ask J. Smith about the quote when you get a chance."
    ]


def test_a_paragraph_break_ends_a_sentence_without_punctuation():
    stream = SentenceStream()
    assert stream.feed("Three accounts have gone quiet\n\nRusty is the worst") == [
        "Three accounts have gone quiet"
    ]


def test_the_first_sentence_has_a_lower_bar_than_later_ones():
    """It is the one being waited on."""
    stream = SentenceStream(min_chars=40, first_min_chars=5)
    assert stream.feed("Not yet. ") == ["Not yet."]
    assert stream.feed("Soon. ") == []


def test_flush_returns_the_tail():
    stream = SentenceStream()
    stream.feed("A complete sentence here goes out. And a trailing fragment")
    assert stream.flush() == "And a trailing fragment"
    assert stream.flush() == ""


async def test_quitting_mid_sentence_is_not_reported_as_an_interruption(loop_parts):
    """Quit and barge-in both stop the speech; only one is a cut-off turn."""
    backend = FakeBackend()
    backend.play_delay = 0.6
    loop, out, _ = loop_parts(
        [{"text": "One sentence here. Two sentence here."}],
        trigger=FakeTrigger(presses=1, quit_after=0.05),
        backend=backend,
    )
    await loop.run()
    assert "(cut off)" not in out.getvalue()


async def test_an_interrupting_press_starts_the_next_turn(loop_parts):
    """Cutting in and then speaking has to be one action, not two presses."""
    backend = FakeBackend()
    backend.play_delay = 1.0
    trigger = SlowTrigger(first_delay=0.0, second_delay=0.05)
    loop, out, _ = loop_parts(
        [{"text": "One sentence here. Two sentence here."}, {"text": "Second turn."}],
        trigger=trigger,
        backend=backend,
    )
    await loop.run()

    assert loop.turns == 2, "the interrupting press should have begun a second turn"
    assert trigger.used == 2, "it should not have needed a third press"
    assert "(cut off)" in out.getvalue()


@pytest.mark.parametrize(
    "pcm,seconds,marker",
    [
        (tone(0.1), 0.1, "the key press probably did not register"),
        (b"\x00\x00" * 32000, 2.0, "Let desktop apps access your microphone"),
        (
            array.array("h", [int(60 * math.sin(i / 9)) for i in range(32000)]).tobytes(),
            2.0,
            "very faint",
        ),
        (tone(2.0), 2.0, "transcription finding no speech"),
    ],
    ids=["missed keypress", "mic never opened", "spoke too quietly", "audio fine"],
)
def test_the_four_kinds_of_nothing_are_distinguishable(pcm, seconds, marker):
    """They all look identical to the operator without the numbers."""
    from ranger.audio import levels
    from ranger.voiceloop import diagnose

    lines = diagnose(pcm, seconds, levels(pcm), heard_speech=False)
    joined = " ".join(lines)
    assert marker in joined
    assert f"{seconds:.1f}s captured" in lines[0]
    assert "dBFS peak" in lines[0] or "silent" in lines[0]
