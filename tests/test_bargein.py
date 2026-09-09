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
import random
import struct

import pytest

from ranger.bargein import (
    DEFAULT_MARGIN,
    DEFAULT_TAIL,
    Detector,
    Interruption,
)

#: 80ms of 16kHz mono, which is what the hotword is fed.
FRAME_SAMPLES = 1280


def frame(level: float, seed: int = 0) -> bytes:
    """One frame at exactly this RMS.

    Noise rather than a tone, and a different seed per source, because these
    frames get mixed. Two copies of the same sine add in phase, so a room and
    an echo both at 600 came out at 1200 instead of the 850 that uncorrelated
    sound actually gives -- and the calibration then demanded a margin twice
    what the hardware needs. Sound in a room is uncorrelated; the fixture has
    to be too.
    """
    rng = random.Random(seed)
    raw = [rng.uniform(-1.0, 1.0) for _ in range(FRAME_SAMPLES)]
    scale = math.sqrt(sum(value * value for value in raw) / len(raw))
    samples = [
        max(-32768, min(32767, int(value * level / scale))) for value in raw
    ]
    return struct.pack(f"<{len(samples)}h", *samples)


ECHO_LEVEL = 1200         # Jarvis, at the microphone, on a laptop speaker

SILENCE = frame(50)
ECHO = frame(ECHO_LEVEL)
VOICE = frame(4200)       # the operator, talking over him
#: Someone speaking softly: over the echo, but not by the margin. Tied to the
#: margin rather than written as a number, so that changing the default cannot
#: quietly turn this into a test of nothing.
QUIET_VOICE = frame(ECHO_LEVEL * DEFAULT_MARGIN * 0.85)


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


# -- calibration ------------------------------------------------------------


from ranger.bargein import Pass, Spread, advise, mix, replay, sustained


def levels_of(frames):
    from ranger.wake import rms

    return [rms(item) for item in frames]


def recording(*, mostly: float, peaks: float = 0.0, seconds: float = 4.0,
              every: int = 4, seed: int = 1) -> Pass:
    """A pass with a known sustained level and occasional peaks.

    The sustained level is what the detector responds to; the peaks are there
    because every real recording has them and because a peak four times the
    sustained level is exactly what made the old recommendation wrong.

    `every` has to be small enough that peaks land inside the first 0.6s. The
    first version spaced them every twelve frames, which put none of them in
    the learning window, so the detector learned the *mean* of each pass and
    every recommendation came out flattering. A reply's first half second
    contains consonants like the rest of it does.
    """
    count = int(seconds / 0.08)
    frames = [
        frame(peaks if (peaks and index and index % every == 0) else mostly,
              seed=seed * 1000 + index)
        for index in range(count)
    ]
    assert not peaks or any(
        level > mostly for level in levels_of(frames[: int(0.6 / 0.08) + 1])
    ), "the learning window must contain a peak, or this fixture flatters"
    return Pass(kind="test", levels=levels_of(frames), frames=frames)


def test_sustained_is_not_the_peak_and_not_the_mean():
    """**The statistic the detector actually responds to.**

    The sustain means a single loud frame is ignored, so a peak overstates what
    the detector will honour. The gaps between words drag a mean below it. This
    is the level the recording stayed above long enough to count.
    """
    assert sustained([100, 9000, 100, 100], 3) == 100
    assert sustained([100, 4000, 4000, 4000, 100], 3) == 4000
    assert sustained([100, 4000, 4000, 100], 3) == 100, "two frames is not held"
    assert sustained([500, 500], 3) == 0.0, "shorter than the window"


def test_the_echo_peak_is_several_times_the_echo_it_holds():
    """Why a margin over a peak stopped meaning anything.

    Measured on a real machine the echo's peak came out four to five times its
    mean, so a margin of 2.2 over the peak asked the operator to hold roughly
    nine times the level of Jarvis's own average. This is that shape, in a
    fixture, so a change that reintroduces peak-based advice fails here.
    """
    item = recording(mostly=900, peaks=4000)

    assert item.peak / item.held() > 4
    assert item.held() < item.peak


def test_a_recommendation_is_bounded_below_by_the_room():
    """**The room was measured and then ignored.**

    The old recommendation divided the operator's peak by the echo's peak and
    never looked at pass one at all, so it could -- and did -- suggest a
    threshold underneath the room's own level. Ambient noise would then
    interrupt with nobody in the chair.
    """
    echo = [recording(mostly=900, peaks=3600)]
    voice = [recording(mostly=6000)]
    room = [recording(mostly=2600)]

    answer = advise(echo, voice, room)
    assert answer.workable

    # Asserted as the guarantee rather than as a mechanism: whatever the advice
    # is arrived at by, the room must not interrupt at the margin it names.
    # The old recommendation divided the operator's peak by the echo's peak and
    # never looked at pass one at all, so it could -- and did -- name a
    # threshold underneath the room's own level.
    from ranger.bargein import _mixed

    head = echo[0].frames[:9]
    assert not replay(
        head, _mixed(echo[0].frames, room[0].frames),
        margin=answer.suggested, room=room[0].frames,
    ), "the room does not interrupt at the margin the advice names"
    assert replay(
        head, voice[0].frames, margin=answer.suggested, room=room[0].frames,
    ), "and the operator still does"


def test_a_room_louder_than_the_voice_is_reported_as_no_gap():
    """Rather than a number that cannot work.

    There is a real state of the world where energy alone cannot separate the
    operator from their own room, and saying so is the useful answer. Offering
    a margin anyway is how somebody spends a week tuning a setting that had no
    correct value.
    """
    echo = [recording(mostly=900, peaks=3600)]
    voice = [recording(mostly=1500)]
    room = [recording(mostly=2400)]

    answer = advise(echo, voice, room)

    assert not answer.workable
    assert answer.suggested is None
    assert "no gap" in answer.binding


def test_the_advice_is_what_the_detector_does_not_a_proxy_for_it():
    """**Tests that agree only with each other are the bug**, and so is a
    calibration that agrees only with itself.

    The recommended margin is checked here against a real Detector fed the same
    recordings: it must not fire on the echo, and it must fire on the voice. If
    the advice ever goes back to arithmetic on summary numbers, this is what
    notices.
    """
    echo = [recording(mostly=900, peaks=3600)]
    voice = [recording(mostly=6000)]
    answer = advise(echo, voice, [recording(mostly=60)])

    assert answer.suggested is not None
    head = echo[0].frames[:9]
    assert not replay(head, echo[0].frames, margin=answer.suggested)
    assert replay(head, voice[0].frames, margin=answer.suggested)


def test_the_spread_across_passes_is_reported_not_one_reading():
    """One four-second sample of a voice is not a voice."""
    spread = Spread("you over it", [
        recording(mostly=1500), recording(mostly=2000), recording(mostly=1700),
    ])

    low, high = spread.held
    assert (low, high) == (1500, 2000) or low < high
    assert spread.swing() > 1.2
    assert "held" in spread.describe() and "peak" in spread.describe()


def test_mixing_two_recordings_clips_rather_than_wrapping():
    """A wrap turns two loud frames into a quiet one, which would read as the
    room making a reply easier to hear."""
    from ranger.wake import rms

    loud = frame(20000)
    both = mix(loud, loud)

    assert rms(both) > rms(loud), "summing sound does not make it quieter"
    assert rms(both) < 2 * rms(loud), "and it clipped rather than overflowing"


def test_three_measured_runs_minutes_apart_have_no_common_margin():
    """**The operator's own numbers, as a fixture.**

    Three `mic-bargein` runs, same seat, same speaker volume, minutes apart::

        run 1  room 1267/4064   echo 598/3219   voice 2011/5911
        run 2  room   86/ 142   echo 944/3665   voice 1581/8138
        run 3  room  424/3040   echo 894/4289   voice 1744/6273

    The room swings fifteen times over between runs while the voice barely
    moves. Run 1's room needs a margin above roughly twice the echo; run 2's
    voice cannot clear anything like that. No single number satisfies both,
    which is why a margin picked from one run was wrong on the next.

    The passes here are reconstructed from those means and peaks, since the
    version that produced them did not report a held level -- that is the
    reason this test exists. What is asserted is the shape, not a figure.
    """
    runs = [
        (1267, 4064, 598, 3219, 2011, 5911),
        (86, 142, 944, 3665, 1581, 8138),
        (424, 3040, 894, 4289, 1744, 6273),
    ]
    rooms, echoes, voices = [], [], []
    for index, numbers in enumerate(runs):
        room_mean, room_peak, echo_mean, echo_peak, voice_mean, voice_peak = numbers
        rooms.append(recording(mostly=room_mean, peaks=room_peak, seed=10 + index))
        echoes.append(recording(mostly=echo_mean, peaks=echo_peak, seed=20 + index))
        voices.append(recording(mostly=voice_mean, peaks=voice_peak, seed=30 + index))

    together = advise(echoes, voices, rooms)

    assert together.workable, "one margin now covers all three runs"
    assert together.low <= DEFAULT_MARGIN <= together.high, (
        "and the shipped default is inside it"
    )

    # The room in run 1 is the thing to say out loud rather than tune around:
    # it holds a level close to the operator's own, and no threshold separates
    # two signals that are the same size.
    assert any("headset" in line for line in together.lines)

    # And why it did not work before. The detector took the loudest single
    # frame of the reply as its reference, so the bar at margin 1.0 was already
    # the echo's peak -- above the level the operator held, on every run. That
    # is a detector with no valid setting rather than a badly tuned one, and it
    # is why "sometimes it stops and sometimes it does not" was the symptom:
    # only a chance alignment of voice peaks ever cleared it.
    for index in range(3):
        assert voices[index].held() < echoes[index].peak, (
            f"run {index + 1}: no margin at or above 1.0 could ever have worked"
        )


# -- the room, measured rather than assumed ---------------------------------


def test_the_room_is_measured_from_the_frames_between_replies():
    """**The room was the term that moved most and the only one never
    measured.** The microphone loop runs the whole time hands free is on, so
    the frames between replies are free and they are the room."""
    watcher, clock = detector()

    assert watcher.room == 0.0, "nothing heard yet is not a claim about silence"
    for _ in range(40):
        watcher.feed(frame(900))
        clock.tick()

    assert 850 <= watcher.room <= 950


def test_a_noisy_room_raises_the_bar_by_itself():
    """The failure this exists to stop is not missing an interruption. It is
    Jarvis stopping mid-sentence with nobody in the chair."""
    quiet, clock = detector()
    for _ in range(40):
        quiet.feed(frame(80))
        clock.tick()
    play(quiet, clock, [ECHO] * 3)
    low = quiet.threshold

    noisy, clock = detector()
    for _ in range(40):
        noisy.feed(frame(2600))
        clock.tick()
    play(noisy, clock, [ECHO] * 3)

    assert noisy.threshold > low
    assert noisy.threshold > 2600, "the bar is above the room, not level with it"


def test_room_noise_alone_does_not_interrupt_a_reply():
    """The room was measured while nothing played, so when it carries on at the
    same level during a reply it is below its own bar."""
    watcher, clock = detector()
    noise = frame(2400)
    for _ in range(40):
        watcher.feed(noise)
        clock.tick()

    fired = play(watcher, clock, [noise] * 12, level=noise)

    assert not any(fired)


def test_a_talker_does_not_become_the_room():
    """A median over ten seconds, so a sentence moves it barely at all. A mean
    would let anyone talking near the microphone raise the bar on themselves."""
    watcher, clock = detector()
    for index in range(125):
        watcher.feed(VOICE if index % 5 == 0 else frame(200))
        clock.tick()

    assert watcher.room < 400, "one voice in five frames is not the room"


def test_the_tail_keeps_jarvis_out_of_the_room_measurement():
    """The frames just after a reply still contain Jarvis. Counting them as
    room would let a loud reply raise the bar for the next one.

    Asserted across a conversation rather than one reply. A first version fed
    three loud frames into a settled window and asserted the median had not
    moved -- which it had not, with or without the guard, because that is what
    a median is for. What actually breaks is accumulation: in back-and-forth
    conversation the tail frames of every reply are a large share of the last
    ten seconds, and then they are the median.
    """
    watcher, clock = detector()
    quiet, jarvis = frame(200), frame(6000)

    for _ in range(20):
        watcher.speaking(True)
        for _ in range(3):
            watcher.feed(jarvis)
            clock.tick()
        watcher.speaking(False)
        for _ in range(4):          # still in the buffer: inside the tail
            watcher.feed(jarvis)
            clock.tick()
        for _ in range(2):          # actually the room again
            watcher.feed(quiet)
            clock.tick()

    assert watcher.room < 400, "a talkative reply is not the room getting louder"


# -- comparing like with like -----------------------------------------------


def test_the_echo_reference_is_a_held_level_not_a_peak():
    """**The defect three calibration runs exposed.**

    Taking the loudest frame of the learning window made the reference four to
    five times the echo's own average, so the operator had to hold roughly nine
    times Jarvis's average level to clear a margin of 2.2. Measured on
    hardware, no margin at or above 1.0 could work at all -- which is a
    detector with no valid setting rather than a badly tuned one.
    """
    watcher, clock = detector()
    watcher.speaking(True)
    for index in range(9):
        watcher.feed(frame(9000) if index % 4 == 0 else frame(900))
        clock.tick()

    assert watcher.echo < 2000, "a consonant burst is not the level Jarvis holds"
    assert 800 <= watcher.echo <= 1000


def test_an_ordinary_voice_clears_an_ordinary_echo():
    """The whole point of the ratio meaning something. Levels here are the ones
    a real machine reported: the echo holding about 900, the operator about
    1700, which is the middle run of three."""
    watcher, clock = detector()
    for _ in range(40):
        watcher.feed(frame(420))
        clock.tick()

    fired = play(watcher, clock, [frame(1700)] * 8, level=frame(900))

    assert any(fired), "1700 over 900 is a person talking over a laptop speaker"


def test_the_room_window_is_the_length_it_is_configured_to_be():
    """A setting the code does not read is the same as no setting. The tail
    spent a build in exactly that state, so this is asserted rather than
    assumed: a short window forgets, a long one does not."""
    short, clock = detector(room_seconds=1.6)
    for _ in range(20):
        short.feed(frame(3000))
        clock.tick()
    for _ in range(20):
        short.feed(frame(100))
        clock.tick()

    assert short.room < 200, "a 1.6s window has forgotten the noise"

    long, clock = detector(room_seconds=30.0)
    for _ in range(20):
        long.feed(frame(3000))
        clock.tick()
    for _ in range(20):
        long.feed(frame(100))
        clock.tick()

    assert long.room > 2000, "a 30s window still remembers it"
