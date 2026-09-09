"""Talking over Jarvis stops him talking.

The hard part is not detecting speech. It is telling the operator's voice from
Jarvis's own coming back through the microphone, on a laptop speaker, with no
echo canceller anywhere in the path. A detector that ignores that stops Jarvis
on his own second syllable, every reply, forever.

So the separation is three things, and each is tested here on its own:

- **Time.** Barge-in is only looked for while a reply is actually playing.
- **A tail.** For a short while after the browser reports its queue drained,
  nothing counts, because frames already in the microphone buffer still contain
  the last syllable.
- **A measured level.** While Jarvis is speaking and nobody has interrupted,
  whatever the microphone hears *is* the echo. It is learned per reply, and the
  operator has to beat it by a margin, for long enough that a peak in Jarvis's
  own speech cannot do it.

Every frame here is synthesised, so all of it runs without a sound card. What
none of it can tell anybody is whether the numbers feel right in a room, which
is why the interruption is logged with how much was left unspoken and why there
is a command that measures both levels on a real machine.
"""

from __future__ import annotations

import math
import struct

import pytest

from ranger.bargein import DEFAULT_TAIL, Detector, Interruption

#: 80ms of 16kHz mono, which is what the hotword is fed.
FRAME_SAMPLES = 1280


def frame(level: float) -> bytes:
    """One frame at roughly this RMS. A sine, so it is not a DC block."""
    samples = [
        int(level * math.sqrt(2) * math.sin(2 * math.pi * 220 * n / 16000))
        for n in range(FRAME_SAMPLES)
    ]
    return struct.pack(f"<{len(samples)}h", *samples)


SILENCE = frame(50)
ECHO = frame(1200)        # Jarvis, at the microphone, on a laptop speaker
VOICE = frame(4200)       # the operator, talking over him
QUIET_VOICE = frame(1800) # someone speaking softly, under the margin


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float = 0.08) -> None:
        self.now += seconds


def detector(**overrides) -> tuple[Detector, Clock]:
    clock = Clock()
    return Detector(now=clock, **overrides), clock


def play(detector, clock, frames, level=ECHO):
    """Run the learning window, then feed frames, returning when it fired."""
    detector.speaking(True)
    for _ in range(int(detector.learn_seconds / 0.08) + 1):
        detector.feed(level)
        clock.tick()
    fired = []
    for item in frames:
        fired.append(detector.feed(item))
        clock.tick()
    return fired


# -- it only looks while Jarvis is talking ---------------------------------


def test_nothing_is_detected_when_nothing_is_playing():
    """The wake phrase and the conversation window own the microphone the rest
    of the time, and they always did."""
    watcher, clock = detector()

    assert not watcher.listening
    assert not any(watcher.feed(VOICE) for _ in range(20))


def test_talking_over_a_reply_interrupts_it():
    watcher, clock = detector()
    fired = play(watcher, clock, [VOICE] * 6)

    assert any(fired), "the operator has to be able to interrupt"
    assert fired.count(True) == 1, "once per interruption, not once per frame"


def test_the_wake_phrase_is_not_needed():
    """Nobody says a machine's name to interrupt it mid-sentence. The detector
    knows nothing about the phrase and has no access to the model that does."""
    import inspect

    from ranger import bargein

    # Asked of the code rather than the comments: the docstring may mention
    # the hotword, since saying what this is *not* is half of what it is for.
    import ast

    tree = ast.parse(inspect.getsource(bargein.Detector))
    names = {
        node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")
        for node in ast.walk(tree)
    }
    assert not {"phrase", "hotword", "wake_word"} & names


# -- Jarvis does not interrupt himself -------------------------------------


def test_jarvis_own_voice_does_not_interrupt_him():
    """**The whole problem, on a laptop speaker.** The echo is loud, it is
    speech-shaped, and it lasts for the entire reply."""
    watcher, clock = detector()
    fired = play(watcher, clock, [ECHO] * 40)

    assert not any(fired)


def test_a_louder_reply_raises_the_bar_by_itself():
    """The level is measured per reply rather than configured once: turning the
    speaker up must not turn Jarvis into something that interrupts himself."""
    loud_echo = frame(2600)
    watcher, clock = detector()
    fired = play(watcher, clock, [loud_echo] * 20, level=loud_echo)

    assert not any(fired)
    assert watcher.threshold > 2600, "the threshold followed the echo up"


def test_someone_speaking_under_the_margin_is_not_an_interruption():
    """A margin is a decision about what counts, and it has to be crossable
    only by someone who means it."""
    watcher, clock = detector()
    fired = play(watcher, clock, [QUIET_VOICE] * 10)

    assert not any(fired)


def test_a_peak_in_jarvis_own_speech_cannot_do_it():
    """One frame over the line is a consonant, not a person. The sustain is
    what tells them apart."""
    watcher, clock = detector()
    fired = play(watcher, clock, [ECHO, VOICE, ECHO, ECHO, VOICE, ECHO])

    assert not any(fired)


def test_two_loud_frames_are_not_yet_a_person():
    """**The sustain, on its own.** Removing it passed every other test in this
    file, because a single frame over the line only ever records the time. Two
    consecutive frames is a syllable; it takes about three to be somebody
    talking, and the difference is what stops a burst in Jarvis's own speech.
    """
    watcher, clock = detector()
    fired = play(watcher, clock, [VOICE, VOICE, ECHO, ECHO])

    assert not any(fired), "two frames is 160ms, which is a syllable"

    watcher, clock = detector()
    assert any(play(watcher, clock, [VOICE] * 5)), "five is a person"


def test_a_bang_that_is_not_speech_is_not_an_interruption():
    """A door, a dropped mug, a hand on the desk: loud, and over in one frame."""
    watcher, clock = detector()
    fired = play(watcher, clock, [frame(9000)] + [SILENCE] * 6)

    assert not any(fired)


def test_sustained_noise_that_is_not_speech_does_interrupt():
    """Stated rather than pretended otherwise: this is an energy detector, so a
    vacuum cleaner next to the microphone will stop the speech. It stops the
    speech and nothing else, which is a cheap thing to be wrong about."""
    watcher, clock = detector()
    fired = play(watcher, clock, [frame(5000)] * 8)

    assert any(fired)


# -- the tail ---------------------------------------------------------------


def test_nothing_counts_once_the_queue_has_drained():
    """The browser says the queue drained, but this process has a frame in hand
    and PortAudio has more behind it, all of them containing the last syllable.

    None of them count, and the reason is the playback gate rather than the
    tail: detection only ever looks while a reply is playing. The tail is a
    separate job -- see the next-sentence test -- and this is asserted here so
    that changing the tail cannot quietly change this.
    """
    watcher, clock = detector()
    play(watcher, clock, [ECHO] * 3)

    watcher.speaking(False)
    assert watcher.guarded
    assert not any(watcher.feed(VOICE) for _ in range(4)), "still inside the tail"

    clock.tick(DEFAULT_TAIL + 0.01)
    assert not watcher.guarded


def test_the_tail_is_long_enough_for_the_audio_path_and_short_enough_for_a_person():
    """Both halves matter and they pull in opposite directions. The number is
    in config; these are the bounds it has to sit inside.

    Below about 250ms it is inside the round trip plus PortAudio's own input
    latency on Windows shared mode, so an ordinary gap between two sentences
    reads as the end of the reply and the echo is re-learned for each one.
    Above about 600ms a genuinely finished reply is still setting the bar for
    the next one, and after a loud reply that bar is one the operator has to
    shout over.
    """
    assert 0.25 <= DEFAULT_TAIL <= 0.6


def test_the_next_sentence_of_a_reply_is_interruptible_immediately():
    """**What the tail is actually for.**

    The browser reports its queue drained between sentences whenever the model
    is slower than the voice, which in an ordinary reply is most of them. If
    every sentence restarted the learning window, the operator could not
    interrupt during the first 0.6s of any of them -- and in a slow reply that
    is nearly all of it. The tail is what tells the next sentence of a reply
    from the next reply.
    """
    watcher, clock = detector()
    play(watcher, clock, [ECHO] * 3)          # sentence one, echo learned
    learned = watcher.threshold

    watcher.speaking(False)                    # the queue drained
    clock.tick(0.1)                            # ...and the next sentence starts
    watcher.speaking(True)

    assert watcher.threshold == learned, "the echo was carried, not re-measured"
    fired = []
    for _ in range(6):
        fired.append(watcher.feed(VOICE))
        clock.tick()
    assert any(fired), "the operator can interrupt the second sentence at once"


def test_a_new_reply_forgets_the_last_one():
    """The echo is measured per reply. A quiet reply after a loud one must not
    inherit a bar nobody can clear."""
    watcher, clock = detector()
    play(watcher, clock, [frame(2600)] * 6, level=frame(2600))
    high = watcher.threshold

    watcher.speaking(False)
    clock.tick(1.0)
    play(watcher, clock, [ECHO] * 3)

    assert watcher.threshold < high


# -- what gets logged -------------------------------------------------------


def test_the_log_says_how_much_was_left_unspoken():
    """A week of these says whether the margin is right."""
    record = Interruption(spoken=1, sent=5, unspoken_sentences=3, unspoken_chars=214)

    assert "stopped after 2 of 5 sentences" in record.describe()
    assert "3 unspoken (214 characters)" in record.describe()


def test_an_interruption_with_nothing_left_unspoken_says_what_it_looks_like():
    """The shape of Jarvis interrupting himself, named in the log so a week of
    them is readable without going back to the audio."""
    record = Interruption(spoken=4, sent=5, unspoken_sentences=0, unspoken_chars=0)

    assert "nothing left unspoken" in record.describe()
    assert "the margin may be too low" in record.describe()


# -- through the session ----------------------------------------------------


class Agent:
    registry = None

    def __init__(self, config, vault, audit=None):
        self.config = config
        self.vault = vault
        self.audit = audit


def session(config, vault, audit=None):
    from ranger.bridge import Session

    sent: list[dict] = []
    talk = Session(agent=Agent(config, vault, audit), send=sent.append)
    return talk, sent


def test_an_interruption_drains_the_browser_queue(config, vault):
    """Immediately, and everything queued. A trailing sentence after an
    interruption is what makes interrupting feel like it did not work."""
    talk, sent = session(config, vault)
    talk.bargein = Detector()
    talk.speech = ["One.", "Two.", "Three."]
    talk.spoken_to = 0

    talk._interrupted()

    stop = [event for event in sent if event["kind"] == "stop_speaking"]
    assert stop, "the browser is told to drain"
    assert "talking" in stop[0]["reason"]


async def test_an_interruption_does_not_end_the_turn(config, vault):
    """**The turn is not cancelled, only the speaking.**

    By the time a sentence is being spoken the tools have run, and a
    half-spoken answer that still changed something is harder to recover from
    than one that finishes into a room where nobody is listening.

    Run against a real turn, interrupted while it is in flight. Two weaker
    shapes of this test both passed with `stop("interrupted")` wired into the
    interruption: asserting on flags alone, because there was no turn there to
    stop, and then starting the task by hand, because `stop()` cuts
    `Session._turn` and a task this test created is not that. So the turn is
    started through the message the interface actually sends.
    """
    import asyncio

    from ranger.bridge import Session
    from ranger.core import Ranger
    from ranger.testing import ScriptedProvider
    from ranger.toolset import build_registry

    sent: list[dict] = []
    holding = asyncio.Event()

    class Slow:
        """A provider that waits, so the turn is genuinely mid-flight."""

        def __init__(self) -> None:
            self.inner = ScriptedProvider([{"text": "One. Two. Three."}])

        async def stream(self, **kwargs):
            await holding.wait()
            async for event in self.inner.stream(**kwargs):
                yield event

    agent = Ranger(
        config=config, provider=Slow(),
        registry=build_registry(config, vault), vault=vault,
    )
    talk = Session(agent=agent, send=sent.append)
    talk.bargein = Detector()
    talk.speech = ["One.", "Two."]

    talk._start_turn({"text": "say three sentences"})
    await asyncio.sleep(0)
    turn = talk._turn
    assert turn is not None and not turn.done(), "the turn is in flight"

    talk._interrupted()
    assert talk._turn is turn, "the turn was not cut"
    assert not turn.done(), "the turn is still in flight"

    holding.set()
    await asyncio.wait_for(turn, timeout=5)

    assert [event for event in sent if event["kind"] == "stop_speaking"]
    assert [event for event in sent if event["kind"] == "turn_complete"], (
        "the turn finished: interrupting the speech does not abandon the work"
    )
    assert any(
        event.get("reply", "").startswith("One.")
        for event in sent if event["kind"] == "turn_complete"
    )


def test_an_interruption_does_not_close_the_conversation_window(config, vault):
    """Conversation mode's own rule, and it holds: the operator interrupting is
    the most engaged they get."""
    from ranger.conversation import build_window

    talk, sent = session(config, vault)
    talk.bargein = Detector()
    talk.window = build_window(config)
    talk.window.begin()
    talk.speech = ["One."]

    talk._interrupted()

    assert not talk.window.spent, "barge-in does not spend the window"


def test_the_interruption_is_written_to_the_log(config, vault):
    from ranger.audit import AuditLog
    from ranger.vault import Vault

    audit = AuditLog(Vault(config.vault), config.vault.log)
    talk, _ = session(config, vault, audit)
    talk.bargein = Detector()
    talk.speech = ["One.", "Two.", "Three."]
    talk.spoken_to = 0

    talk._interrupted()

    written = "\n".join(
        path.read_text(encoding="utf-8") for path in config.vault.log.glob("*.md")
    )
    assert "speech interrupted" in written
    assert "2 unspoken" in written


def test_speaking_state_is_told_not_inferred(config, vault):
    """The detector is told what is playing by the thing that plays it. Working
    it out from the microphone is the mistake that makes the whole approach
    circular."""
    import inspect

    from ranger import bargein

    source = inspect.getsource(bargein.Detector.speaking)
    assert "rms" not in source
    assert "frame" not in source


def test_the_frame_loop_asks_the_detector_before_the_hotword():
    """Barge-in is not a wake word, and the hotword is armed but idle during
    playback, so nothing competes."""
    import inspect

    from ranger import handsfree

    source = inspect.getsource(handsfree)
    assert "self.bargein.feed(frame)" in source
    assert source.index("self.bargein.feed(frame)") < source.index(
        "utterance, fire = self.hotword.feed(frame)"
    )
