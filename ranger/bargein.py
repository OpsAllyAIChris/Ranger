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
from dataclasses import dataclass, field
from typing import Callable

#: Below this, nothing is speech however the margin works out. The same floor
#: the hotword uses for silence, so "quiet" means one thing.
from .wake import SILENCE_RMS

#: How much louder than the measured echo the operator has to be. A laptop
#: speaker at a normal volume puts Jarvis somewhere near ordinary speech at the
#: microphone, so this is deliberately not subtle.
DEFAULT_MARGIN = 2.2

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


@dataclass
class Detector:
    """Frames in, one interruption out. **Knows nothing about microphones.**

    Fed the same 80ms frames the hotword is fed, and told when playback starts
    and stops. Every rule in it is a number and a clock, so all of them are
    testable without a sound card.
    """

    margin: float = DEFAULT_MARGIN
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
    def threshold(self) -> float:
        """What the operator has to beat, right now."""
        return max(self.floor, self._echo * self.margin)

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

        if not self._playing or self._fired:
            return False

        moment = self.now()
        level = rms(frame)
        self.levels.append(level)

        # The learning window. Whatever is heard here is Jarvis, because a
        # reply's first half second is the part nobody talks over -- and if
        # they do, the echo floor comes out high and this reply is simply
        # harder to interrupt, which is the safe direction to be wrong in.
        if moment - self._started_at < self.learn_seconds:
            self._echo = max(self._echo, level)
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
            "threshold": round(self.threshold, 1),
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
