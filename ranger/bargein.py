"""Talking over Jarvis stops him talking.

Hands free, mid-reply, the operator starts speaking. The speech stops. They do
not have to say the wake phrase to interrupt a machine that is already talking
to them, because nobody does that to a person.

**The hard part is not detecting speech. It is telling the operator's voice
from Jarvis's own, coming back through the microphone.** On a laptop speaker
that is the whole problem: the microphone hears the reply at a level that is
often comparable to a person talking, and a detector that ignores it stops
Jarvis on his own second syllable, every reply, forever.

There is no echo canceller here. The microphone is PortAudio's, in this
process, and nothing in that path subtracts a reference signal. So the
separation is three cheap things stacked, each of which is honest about what it
is:

1. **Time.** Barge-in is only ever looked for while Jarvis is actually
   playing. Before and after, the wake phrase and the conversation window own
   the microphone, and they always did.
2. **A tail.** The browser reports its speaker queue drained between
   sentences, not just at the end of a reply, because the model is often
   slower than the voice. A drain inside the tail is the same reply carrying
   on; a drain older than the tail is the end of one. That is what stops every
   sentence from re-learning an echo level already measured, which would leave
   a slow reply un-interruptible for most of its length.
3. **A measured level, not a guessed one.** While Jarvis is speaking and
   nobody has interrupted, whatever the microphone hears *is* the echo. That
   level is learned per reply and the operator has to beat it by a margin, for
   long enough that a peak in Jarvis's own speech cannot do it.

The third is the one that will need tuning on a real machine, which is why
`ranger mic bargein` measures both levels and prints them rather than leaving
the operator to guess at a number.

**With a headset none of this is needed and all of it is harmless**: the echo
floor measures near silence, so the margin is met by any speech at all.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Sequence

#: Below this, nothing is speech however the margin works out. The same floor
#: the hotword uses for silence, so "quiet" means one thing.
from .wake import SILENCE_RMS

#: How much louder than the measured echo the operator has to be.
#:
#: This compares two *held* levels -- what Jarvis's echo sustains against what
#: the operator sustains -- so it is a like-for-like ratio and a number in this
#: range means something. An earlier version took the loudest single frame of
#: the reply as the reference, which on a real machine came out four to five
#: times the echo's own average, so a margin of 2.2 was really asking the
#: operator to hold nine times Jarvis's average level. Measured on hardware, no
#: margin at or above 1.0 could work at all.
DEFAULT_MARGIN = 1.4

#: How much louder than the room the operator has to be.
#:
#: Separate from the margin because the room and the echo move independently,
#: and measurement says the room moves most: three calibration runs minutes
#: apart in one seat put the room's held level at 1264, 85 and 423 while the
#: operator's barely moved. A bar set only from the echo is therefore
#: sometimes underneath the room, and the room then interrupts with nobody in
#: the chair -- which is a worse failure than not stopping.
#:
#: Lower than the echo margin on purpose: the room is measured continuously
#: from the frames between replies, so this multiplies a level that is current
#: rather than one sampled once.
DEFAULT_ROOM_MARGIN = 1.4

#: How much of the recent room to keep, in seconds. The median of it is the
#: floor. A median rather than a mean or a peak so that somebody talking, or a
#: door, moves it barely at all while a fan being switched on moves it fully.
ROOM_SECONDS = 10.0

#: How long that has to hold. Long enough that a consonant burst in Jarvis's
#: own speech cannot do it, short enough to feel immediate: about three frames.
DEFAULT_SUSTAIN = 0.22

#: How long a drained speaker queue can stay drained and still be the same
#: reply, rather than the end of one.
#:
#: It covers the distance between "the browser says it has finished this
#: sentence" and "the next sentence is playing": the socket hop each way, one
#: 80ms microphone frame already in hand, and PortAudio's own input latency,
#: which on Windows shared mode is routinely 100-150ms. 350ms clears all of
#: that with room to spare, and is well under the gap that means the reply
#: really has ended.
#:
#: Too short and every sentence re-learns the echo, so the first 0.6s of each
#: one cannot be interrupted. Too long and the echo measured for the last
#: reply is still setting the bar for the next one, which after a loud reply
#: is a bar the operator has to shout over. The log line says how much was
#: left unspoken on every interruption, so a week of them says whether this
#: number is right.
DEFAULT_TAIL = 0.35

#: How long at the start of each reply is used to learn the echo level. The
#: first moments of a reply are the ones the operator is least likely to talk
#: over, so they are the cleanest sample of Jarvis alone.
LEARN_SECONDS = 0.6


def sustained(levels: "Sequence[float]", frames: int = 3) -> float:
    """The highest level held for `frames` consecutive frames.

    **The statistic the detector actually responds to**, and the one to quote.
    A peak is one frame and the sustain deliberately ignores it; a mean is
    dragged down by the gaps between words. This is neither: the level the
    recording stayed above for long enough to count.
    """
    if frames <= 0 or len(levels) < frames:
        return 0.0
    return max(
        min(levels[start:start + frames])
        for start in range(len(levels) - frames + 1)
    )


@dataclass
class Detector:
    """Frames in, one interruption out. **Knows nothing about microphones.**

    Fed the same 80ms frames the hotword is fed, and told when playback starts
    and stops. Every rule in it is a number and a clock, so all of them are
    testable without a sound card.
    """

    margin: float = DEFAULT_MARGIN
    room_margin: float = DEFAULT_ROOM_MARGIN
    room_seconds: float = ROOM_SECONDS
    sustain_seconds: float = DEFAULT_SUSTAIN
    tail_seconds: float = DEFAULT_TAIL
    learn_seconds: float = LEARN_SECONDS
    floor: float = SILENCE_RMS
    now: Callable[[], float] = time.monotonic

    #: True from the moment audio is sent until the browser says it drained.
    _playing: bool = False
    _started_at: float = 0.0
    _drained_at: float = 0.0
    #: The loudest thing heard during the learning window: Jarvis, at the mic.
    _echo: float = 0.0
    _learned: bool = False
    _above_since: float = 0.0
    _fired: bool = False
    #: Every level seen while playing, for the calibration command.
    levels: list[float] = field(default_factory=list)
    #: The learning window's levels, from which the held echo is taken.
    _learning: list[float] = field(default_factory=list)
    #: Recent frames from between replies. The room, as it is right now.
    #: Sized from room_seconds in __post_init__ -- a window that ignored its
    #: own setting would be the third inert knob in this file.
    _room: "deque[float]" = field(default_factory=deque)

    def __post_init__(self) -> None:
        self._room = deque(self._room, maxlen=max(1, round(self.room_seconds / 0.08)))

    def speaking(self, playing: bool) -> None:
        """Told by whoever owns the speaker. Not inferred from the microphone."""
        moment = self.now()
        if playing and not self._playing:
            self._playing = True
            self._above_since = 0.0
            self._fired = False
            if self._resumed(moment):
                # The same reply, carrying on. The browser reports its queue
                # drained between sentences whenever the model is slower than
                # the voice, which in an ordinary reply is most of them. Left
                # to restart, every sentence would spend its first 0.6s
                # learning an echo level already measured, and a slow reply
                # would be un-interruptible for most of its length. The tail is
                # what tells "the next sentence" from "the next reply".
                self._started_at = moment - self.learn_seconds
            else:
                self._started_at = moment
                self._echo = 0.0
                self._learned = False
                self.levels = []
                self._learning = []
        elif not playing and self._playing:
            self._playing = False
            self._drained_at = moment
            self._above_since = 0.0

    @property
    def listening(self) -> bool:
        """Is a frame worth looking at right now?

        Only while playing. The tail is not a period of reduced sensitivity, it
        is a period of none: after the queue drains the conversation window is
        opening anyway, and that path has owned the follow-up since it was
        built.
        """
        return self._playing

    @property
    def guarded(self) -> bool:
        """Is the microphone still inside the tail after a reply?"""
        return not self._playing and self._within_tail(self.now())

    def _within_tail(self, moment: float) -> bool:
        return (
            self._drained_at > 0.0 and moment - self._drained_at < self.tail_seconds
        )

    def _resumed(self, moment: float) -> bool:
        """Is this the next sentence of a reply, rather than a new reply?"""
        return self._learned and self._within_tail(moment)

    @property
    def sustain_frames(self) -> int:
        """The sustain, in frames. What "held" means everywhere in here."""
        return max(1, round(self.sustain_seconds / 0.08))

    @property
    def room(self) -> float:
        """The room as it is now: the median of the recent frames between
        replies. Zero until enough of them have been heard to mean anything."""
        # A second of it, at least. A median of three frames is not a room,
        # and reporting one would put a bar under the first thing heard.
        if len(self._room) < min(12, self._room.maxlen or 12):
            return 0.0
        recent = sorted(self._room)
        return recent[len(recent) // 2]

    @property
    def threshold(self) -> float:
        """What the operator has to beat, right now.

        Three terms, because there are three things in the microphone and the
        bar has to clear all of them: an absolute floor so that "quiet" means
        one thing everywhere, the room as measured a moment ago, and the echo
        as measured at the start of this reply.
        """
        return max(
            self.floor,
            self.room * self.room_margin,
            self._echo * self.margin,
        )

    @property
    def echo(self) -> float:
        return self._echo

    def feed(self, frame: bytes) -> bool:
        """One frame. True exactly once, on the frame that decides it.

        Returns False for everything else, including every frame of an
        interruption after the first: stopping the speech twice is not a second
        interruption, and the log would say it was.
        """
        from .wake import rms

        if not self._playing:
            # Between replies, and outside the tail, whatever the microphone
            # hears is the room. This is the only place the room is measured,
            # and it is measured constantly rather than once, because the room
            # is the term that moves most. Inside the tail it is still Jarvis.
            if not self.guarded:
                self._room.append(rms(frame))
            return False
        if self._fired:
            return False

        moment = self.now()
        level = rms(frame)
        self.levels.append(level)

        # The learning window. Whatever is heard here is Jarvis, because a
        # reply's first half second is the part nobody talks over -- and if
        # they do, the echo floor comes out high and this reply is simply
        # harder to interrupt, which is the safe direction to be wrong in.
        if moment - self._started_at < self.learn_seconds:
            # The level the echo *holds*, not its loudest frame. The operator
            # has to hold a level for the sustain window to interrupt, so the
            # thing they are being compared against has to be the same kind of
            # measurement or the ratio between them means nothing.
            self._learning.append(level)
            self._echo = sustained(self._learning, self.sustain_frames)
            return False
        if not self._learned:
            self._learned = True

        if level < self.threshold:
            self._above_since = 0.0
            return False

        if self._above_since == 0.0:
            self._above_since = moment
            return False
        if moment - self._above_since < self.sustain_seconds:
            return False

        self._fired = True
        return True

    def measured(self) -> dict[str, float]:
        """What this reply sounded like, for the calibration command."""
        heard = self.levels or [0.0]
        return {
            "echo": round(self._echo, 1),
            "room": round(self.room, 1),
            "threshold": round(self.threshold, 1),
            "held": round(sustained(heard, self.sustain_frames), 1),
            "loudest": round(max(heard), 1),
            "frames": float(len(heard)),
        }


@dataclass
class Interruption:
    """What was cut off, for the log.

    Counted in sentences and characters rather than seconds, because sentences
    are what was queued and characters are what nobody heard. A week of these
    says whether the margin is right: an interruption with almost nothing left
    unspoken is Jarvis stopping on his own last syllable, which means his own
    voice cleared the bar the reply set for itself.
    """

    spoken: int
    sent: int
    unspoken_sentences: int
    unspoken_chars: int

    def describe(self) -> str:
        if self.unspoken_sentences <= 0:
            return (
                f"stopped after {self.spoken + 1} of {self.sent} sentences, with "
                "nothing left unspoken. That is the shape of Jarvis interrupting "
                "himself: the margin may be too low"
            )
        return (
            f"stopped after {self.spoken + 1} of {self.sent} sentences, "
            f"{self.unspoken_sentences} unspoken ({self.unspoken_chars} characters)"
        )


# -- measuring it on a real machine -----------------------------------------
#
# Everything below is for `ranger mic-bargein`. None of it runs during a reply.
#
# **It does not compute a proxy for the detector, it runs the detector.** An
# earlier version compared summary numbers -- a peak against a threshold -- and
# recommended a margin that arithmetic said would work and the detector would
# not have honoured, because the detector needs a level *held* for the sustain
# window and a peak is one frame. A recommendation that disagrees with the code
# is worse than none, so the recommendation is made by replaying the recording
# through a real Detector at each candidate margin and seeing what it does.


def mix(one: bytes, two: bytes) -> bytes:
    """Two recordings, summed as sound. Clipped rather than wrapped."""
    import struct

    count = min(len(one), len(two)) // 2
    a = struct.unpack(f"<{count}h", one[: count * 2])
    b = struct.unpack(f"<{count}h", two[: count * 2])
    return struct.pack(
        f"<{count}h",
        *(max(-32768, min(32767, x + y)) for x, y in zip(a, b)),
    )


@dataclass
class Pass:
    """One recording, and the three numbers worth printing.

    `held` is the one the detector uses. `peak` is printed because it is what
    the operator hears themselves do, and `mean` because the distance between
    mean and peak is the whole reason a margin over a peak is the wrong bar.
    """

    kind: str
    levels: list[float]
    frames: list[bytes] = field(default_factory=list)

    @property
    def mean(self) -> float:
        return sum(self.levels) / len(self.levels) if self.levels else 0.0

    @property
    def peak(self) -> float:
        return max(self.levels) if self.levels else 0.0

    def held(self, frames: int = 3) -> float:
        return sustained(self.levels, frames)


@dataclass
class Spread:
    """The same measurement, several times over.

    One four-second sample of a voice is not a voice, and one sample of a room
    is not a room. The range across passes is the finding; a single reading
    that happens to sit in the middle of it is how a number gets chosen that is
    wrong the next minute.
    """

    kind: str
    passes: list[Pass]

    def _across(self, name: str) -> tuple[float, float]:
        values = [getattr(item, name) if name != "held" else item.held()
                  for item in self.passes] or [0.0]
        return min(values), max(values)

    @property
    def held(self) -> tuple[float, float]:
        return self._across("held")

    @property
    def peak(self) -> tuple[float, float]:
        return self._across("peak")

    @property
    def mean(self) -> tuple[float, float]:
        return self._across("mean")

    def swing(self) -> float:
        """How many times the quietest pass the loudest one was."""
        low, high = self.held
        return high / low if low > 0 else float("inf")

    def describe(self) -> str:
        low, high = self.held
        mean_low, mean_high = self.mean
        peak_low, peak_high = self.peak
        return (
            f"{self.kind}: held {low:.0f}-{high:.0f}, "
            f"mean {mean_low:.0f}-{mean_high:.0f}, peak {peak_low:.0f}-{peak_high:.0f}"
        )


class _Ticker:
    """A clock that advances one frame per call, for replaying a recording."""

    def __init__(self, step: float = 0.08) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        return self.now

    def tick(self) -> None:
        self.now += self.step


def _levels(frames: "Sequence[bytes]") -> list[float]:
    from .wake import rms

    return [rms(frame) for frame in frames]


def _mixed(one: "Sequence[bytes]", other: "Sequence[bytes]") -> list[bytes]:
    """One recording with another laid over it, repeating the shorter."""
    if not other:
        return list(one)
    return [
        mix(frame, other[index % len(other)]) for index, frame in enumerate(one)
    ]


def replay(learn: "Sequence[bytes]", frames: "Sequence[bytes]", *, margin: float,
           room: "Sequence[bytes]" = (), **settings) -> bool:
    """Would the real detector interrupt on this recording?

    Fed in the order the detector meets them on a real machine: the room first,
    with nothing playing, because that is where the floor comes from; then the
    start of a reply, because that is where the echo level comes from; then the
    recording being asked about.

    Passing the operator's own recording without a reply in front of it would
    measure a reply that began with them already talking, which is a different
    question and a rarer one.
    """
    clock = _Ticker()
    watcher = Detector(margin=margin, now=clock, **settings)
    for frame in room:
        watcher.feed(frame)
        clock.tick()
    watcher.speaking(True)
    for frame in learn:
        watcher.feed(frame)
        clock.tick()
    for frame in frames:
        if watcher.feed(frame):
            return True
        clock.tick()
    return False


@dataclass
class Advice:
    """What margin, if any, is left once every pass has had its say.

    Bounded below by the room and above by the operator. An empty range is a
    real answer and is reported as one: no margin works, here is which two
    measurements collided, and here is what to do that is not a number.
    """

    low: float | None
    high: float | None
    binding: str
    lines: list[str] = field(default_factory=list)

    @property
    def workable(self) -> bool:
        return self.low is not None and self.high is not None

    @property
    def suggested(self) -> float | None:
        """The middle of the workable range, geometrically.

        The middle, not the bottom: the bottom is the margin that only just
        keeps the room out, and the room measured today is not the room.
        """
        if not self.workable:
            return None
        return round((self.low * self.high) ** 0.5, 1)


def scan(
    echo: "Sequence[Pass]",
    voice: "Sequence[Pass]",
    room: "Sequence[Pass]",
    *,
    floor: float = SILENCE_RMS,
    lowest: float = 1.0,
    highest: float = 6.0,
    step: float = 0.05,
    **settings,
) -> "tuple[float | None, float | None]":
    """Scan margins, keep the ones every pass agrees with.

    Three constraints, and all three are measured rather than assumed:

    - **Never on an echo pass.** Jarvis must not interrupt himself. Each reply
      learns its own echo, so each echo pass is replayed behind its own
      learning window rather than behind the first one -- using one pass's
      learning window for another compares a quiet reply's bar against a loud
      reply's speech and reports a problem that does not exist.
    - **Never on the room during a reply.** The room is judged during a reply
      rather than on its own, because a room with nothing playing cannot
      interrupt: detection only ever looks while a reply is in flight. The room
      pass is both fed as the ambient the floor is measured from and laid over
      the echo pass. Laying it over double-counts whatever room the echo pass
      already contained, which makes this bound conservative -- the safe
      direction for a lower bound.
    - **Always on every voice pass.** Not the loudest one, and against the
      loudest room measured. A margin that works when the operator happens to
      lean in, in the quietest minute of the day, is a margin that fails the
      next time.
    """
    if not echo or not voice:
        return None, None

    head = int(LEARN_SECONDS / 0.08) + 1
    ambients: list[list[bytes]] = [list(item.frames) for item in room] or [[]]

    def learning(item: Pass) -> list[bytes]:
        return list(item.frames[:head])

    # **Every pass against every room, not each round against its own.**
    #
    # An earlier version paired round 1's room with round 1's voice, on the
    # reasoning that within a round the passes are seconds apart. That is true
    # and it is the wrong question. The room floor is continuous and the
    # operator's level is independent of it, so over an afternoon the loudest
    # room and the quietest speech meet -- and the detector will be in that
    # state when they do. Pairing by round meant the loud-room round and the
    # quiet-voice round never met, so a machine whose room is louder than its
    # operator got a workable-looking answer.
    #
    # It was introduced to stop a combined verdict coming out pessimistic, and
    # it worked by not asking the question that made it pessimistic. Worst case
    # against worst case is the operational question.
    rounds: list[tuple[Pass, Pass | None, list[bytes]]] = [
        (reply, spoken, ambient)
        for reply in echo
        for spoken in voice
        for ambient in ambients
    ]

    def interrupts_on_nothing(margin: float) -> bool:
        for reply, _, ambient in rounds:
            learn = learning(reply)
            if replay(learn, reply.frames, margin=margin, room=ambient,
                      floor=floor, **settings):
                return True
            if ambient and replay(
                learn, _mixed(reply.frames, ambient),
                margin=margin, room=ambient, floor=floor, **settings
            ):
                return True
        return False

    def hears_every_voice(margin: float) -> bool:
        return all(
            replay(learning(reply), spoken.frames, margin=margin, room=ambient,
                   floor=floor, **settings)
            for reply, spoken, ambient in rounds
            if spoken is not None
        )

    # Both constraints are monotonic in the margin -- a higher bar is harder
    # for anything to cross -- so each side is found by walking in from its own
    # end and stopping, rather than testing every margin against everything.
    steps = int(round((highest - lowest) / step)) + 1
    low = high = None
    for index in range(steps):
        margin = round(lowest + index * step, 2)
        if not interrupts_on_nothing(margin):
            low = margin
            break
    for index in range(steps):
        margin = round(highest - index * step, 2)
        if hears_every_voice(margin):
            high = margin
            break
    return low, high


def advise(
    echo: "Sequence[Pass]",
    voice: "Sequence[Pass]",
    room: "Sequence[Pass]",
    *,
    floor: float = SILENCE_RMS,
    lowest: float = 1.0,
    highest: float = 6.0,
    step: float = 0.05,
    **settings,
) -> Advice:
    """What margin to use, if any, and what to say when there is none.

    **The scan decides and the levels explain.** `scan` replays the recordings
    through a real Detector and is the only thing that decides whether a margin
    exists. `overlaps` says the same thing in levels the operator can act on --
    a room the size of their voice, an echo the size of their voice -- because
    "no margin works" is not advice and "turn the speaker down" is.

    The two are near enough equivalent by construction: both compare the worst
    room and the worst echo against the quietest speech, one by replaying and
    one by arithmetic. That redundancy is deliberate and `reconcile` treats a
    disagreement between them as a fault, because printing a margin next to
    "no margin can work" is how the wrong number gets typed in.
    """
    if not echo or not voice:
        return Advice(None, None, "nothing was recorded")

    low, high = scan(
        echo, voice, room,
        floor=floor, lowest=lowest, highest=highest, step=step, **settings
    )
    warnings, overlapping = overlaps(
        echo, voice, room,
        room_margin=float(settings.get("room_margin", DEFAULT_ROOM_MARGIN)),
    )
    return reconcile(low, high, warnings, overlapping,
                     lowest=lowest, highest=highest)


def overlaps(
    echo: "Sequence[Pass]",
    voice: "Sequence[Pass]",
    room: "Sequence[Pass]",
    *,
    room_margin: float = DEFAULT_ROOM_MARGIN,
) -> "tuple[list[str], bool]":
    """Which levels are the same size as the operator's voice.

    Two signals the same size cannot be separated by size, so this is where
    "no margin can work" comes from -- and, more usefully, which lever moves
    it. A room the size of the voice is answered by a headset; an echo the
    size of the voice is answered by turning the speaker down. Neither is
    answered by a number in the config.

    Explanation, not decision. `scan` decides.
    """
    warnings: list[str] = []
    overlapping = False
    if not voice:
        return warnings, overlapping
    quietest = min(item.held() for item in voice)
    if room:
        loudest = max(item.held() for item in room)
        if loudest * room_margin >= quietest:
            overlapping = True
            warnings.append(
                f"the loudest room you measured holds {loudest:.0f} and the "
                f"quietest you spoke holds {quietest:.0f}. In that room, at that "
                "volume, nothing separates you from it -- no margin can, because "
                "the two levels are the same size. A headset changes both numbers "
                "at once; nothing in this file does."
            )
    if echo:
        loudest = max(item.held() for item in echo)
        if loudest >= quietest:
            overlapping = True
            warnings.append(
                f"the loudest echo you measured holds {loudest:.0f} and the "
                f"quietest you spoke holds {quietest:.0f}. Jarvis is reaching the "
                "microphone as loudly as you do, so no margin above 1.0 can let "
                "you through without letting him through. Turning the speaker "
                "down moves this one; the margin does not."
            )
    return warnings, overlapping


def reconcile(
    low: "float | None",
    high: "float | None",
    warnings: "Sequence[str]",
    overlapping: bool,
    *,
    lowest: float = 1.0,
    highest: float = 6.0,
) -> Advice:
    """One scan and one set of overlaps, into one answer or into none.

    **A number is never returned alongside "no margin can work."** A run
    printed both at once, and the number is the half that gets typed in.

    The two findings should agree, since they compare the same worst cases.
    If they ever stop agreeing, this says so and offers nothing, rather than
    picking whichever of the two is friendlier.
    """
    said = list(warnings)
    found = low is not None and high is not None and low <= high
    if overlapping:
        if found:
            said.append(
                f"the margin scan still found {low:.2f} to {high:.2f} despite "
                "that, which it should not have. No margin is offered, because "
                "the two checks disagree and one of them is wrong."
            )
        return Advice(None, None, "no gap: the levels overlap", said)
    if found:
        return Advice(low, high, "measured on every pass", said)

    if low is None and high is None:
        binding = (
            "no gap: nothing was quiet enough to set a bar, and nothing was "
            "loud enough to clear one"
        )
    elif low is None:
        binding = (
            f"no gap: the echo or the room is still interrupting at {highest:.1f}, "
            "the highest margin worth trying"
        )
    elif high is None:
        binding = (
            f"no gap: your voice does not clear the bar even at {lowest:.1f}, "
            "where the bar is the echo itself"
        )
    else:
        binding = (
            f"no gap: keeping the room and the echo out needs {low:.2f}, "
            f"and your voice only holds up to {high:.2f}"
        )
    return Advice(None, None, binding, said)
