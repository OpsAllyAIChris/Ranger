"""Conversation mode: the window that stays open after Ranger finishes talking.

After a wake word turn, Ranger keeps listening for a follow-up so the phrase
does not have to be said again. The window is short, it is capped, and every
open and close is logged.

**This lives in the caller and nothing about it reaches `Ranger.turn()`.**
Amendment A: the window is voice harness state, the same as the hotword and the
microphone check. The core does not know whether the words it was handed came
from a keyboard, a click, a phrase, or a follow-up, and making it know would put
a second meaning on a conversation the core already has one meaning for.

Three things here are less obvious than they look.

**The anchor is the end of playback, not the end of the turn.** Sentences are
spoken as they are produced, so the speaker's queue empties whenever the model
is slower than the voice — several times in an ordinary reply. Anchoring on the
browser's "I have stopped talking" alone opens the window three times per
answer. Anchoring on the turn alone opens it while Ranger is still speaking, and
the eight seconds are gone before the operator can use them. So the window opens
when the turn is complete **and** the last chunk that was sent is the last chunk
that was played, and a latch makes sure that can happen at most once per turn.

**The reopen budget resets on the phrase, never on time.** A cap that reset
after a quiet minute would not be a cap: a room with a fan would refill it
forever. Saying the wake word is the operator deciding to start something, and
that is the only thing that refills it.

**Nothing spoken in the window is consent.** A follow-up arrives with no phrase
in front of it and reads exactly like continuation, which makes it far more
tempting to treat a spoken "yes" as an answer to a confirmation card. It is not,
it never becomes one, and a card opening closes the window and spends the
budget so the temptation does not arise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class Why(str, Enum):
    """Why a window closed. Every one of these reaches the audit log.

    A week of these is how the cap and the timer get tuned with data rather
    than feel: `timer` in bulk means the window is too long or too eager,
    `cap` in bulk means it is too short.
    """

    TIMER = "timer"            # ran out with nothing said
    SILENT = "silent"          # something was captured and it transcribed to nothing
    SPOKE = "spoke"            # a real follow-up. The window did its job
    GATED = "gated"            # a confirmation card opened
    TYPED = "typed"            # the keyboard or the mouse was used
    MIC_CHECK = "mic_check"    # another application took the microphone
    DISARMED = "disarmed"      # hands free was turned off, or the socket went
    CAP = "cap"                # the reopen budget is spent
    HIDDEN = "hidden"          # the interface is not on screen to show it is live


@dataclass
class Window:
    """Open or closed, and the rules for moving between the two.

    No clock of its own and no timer: `now` is passed in. The caller owns when
    time passes, which is what makes every rule here testable without waiting.
    """

    seconds: float = 8.0
    #: Windows per wake word firing. Three is a first week guess, and the
    #: logged close reasons are what will replace it.
    reopens: int = 3
    #: Refuse to open when the interface is not on screen. An open microphone
    #: whose only indication is on a window nobody can see is the thing the
    #: whole design is trying not to be. Turn this off once the window can
    #: bring itself to the front.
    requires_visible: bool = True

    #: Told, not asked: the caller reports whether another application has the
    #: microphone. Checked on open as well as on the poll, because a call that
    #: started four seconds ago would otherwise not be seen for another one.
    check_microphone: Callable[[], bool] | None = None

    open_at: float | None = None
    used: int = 0

    #: One open per turn, enforced by a latch rather than by hoping the events
    #: arrive in a helpful order.
    _armed: bool = False
    _turn_done: bool = False
    _sent: int = 0
    _played: int = -1
    _log: list[tuple[str, str]] = field(default_factory=list)

    # -- state ---------------------------------------------------------

    @property
    def open(self) -> bool:
        return self.open_at is not None

    @property
    def spent(self) -> bool:
        return self.used >= self.reopens

    # -- the turn ------------------------------------------------------

    def woke(self) -> None:
        """The wake phrase fired. The only thing that refills the budget."""
        self.used = 0
        self.begin()

    def begin(self) -> None:
        """A turn is starting. Arm the latch and forget the last turn's audio."""
        self._armed = True
        self._turn_done = False
        self._sent = 0
        self._played = -1

    def sent(self, index: int) -> None:
        """A chunk of speech went to the browser."""
        self._sent = max(self._sent, index + 1)

    def played(self, index: int) -> None:
        """The browser's speaker queue drained, having played up to `index`."""
        self._played = max(self._played, index)

    def finished(self) -> None:
        """The turn is complete. Not the same as having stopped talking."""
        self._turn_done = True

    @property
    def ready(self) -> bool:
        """Both halves of the anchor, and the latch still set.

        A turn that produced no speech never becomes ready. That is the answer
        to "does a turn with no reply open the window": there is nothing to
        listen after, so it does not.
        """
        if not (self._armed and self._turn_done):
            return False
        if self._sent == 0:
            return False
        return self._played >= self._sent - 1

    # -- opening and closing -------------------------------------------

    def opens(self, now: float) -> tuple[bool, str]:
        """Open if everything allows it. The reason is for the log either way.

        Spends the latch whatever the answer, so a refusal is not retried on
        the next drained-queue message.
        """
        if not self.ready:
            return False, "not ready"
        self._armed = False

        if self.spent:
            self.close(Why.CAP)
            return False, Why.CAP.value
        if self.requires_visible and not self._visible:
            self.close(Why.HIDDEN)
            return False, Why.HIDDEN.value
        if not self._microphone_free():
            self.close(Why.MIC_CHECK)
            return False, Why.MIC_CHECK.value

        self.open_at = now
        self.used += 1
        self._note("opened", f"{self.seconds:.0f}s, {self.used} of {self.reopens}")
        return True, "opened"

    def close(self, why: Why, detail: str = "") -> bool:
        """Shut it. False if it was not open, so callers can log once."""
        was_open = self.open
        self.open_at = None
        self._armed = False
        if why in (Why.GATED, Why.TYPED):
            # A hard close spends the budget. Both of these mean the operator
            # moved to a different way of answering, and the microphone should
            # not be waiting when they come back to it.
            self.used = self.reopens
        if was_open:
            self._note(f"closed {why.value}", detail)
        return was_open

    def expired(self, now: float) -> bool:
        if not self.open or self.open_at is None:
            return False
        return (now - self.open_at) >= self.seconds

    def tick(self, now: float) -> Why | None:
        """Called on the poll. The reason it closed, or None."""
        if not self.open:
            return None
        if not self._microphone_free():
            self.close(Why.MIC_CHECK)
            return Why.MIC_CHECK
        if self.expired(now):
            self.close(Why.TIMER)
            return Why.TIMER
        return None

    def heard(self, text: str) -> Why:
        """Something was captured in the window. Speech, or a room with a fan.

        Either way the window closes. A follow-up becomes a turn and the next
        window is opened by that turn finishing; noise that transcribes to
        nothing closes without one, which is what stops a fan holding the
        microphone open through the whole budget.
        """
        why = Why.SPOKE if text.strip() else Why.SILENT
        self.close(why, text.strip()[:60])
        return why

    # -- visibility ----------------------------------------------------

    _visible: bool = True

    #: **This guard is weaker than it looks, and the limitation is here rather
    #: than in a document because this is where someone will read it.**
    #:
    #: The browser reports `document.visibilityState`, which is a *tab*-level
    #: signal: it says whether the page is the active tab and whether the window
    #: is minimised. It says nothing about whether the window is on screen. A
    #: Ranger window fully covered by Teams during a screen share reports
    #: `visible`, and so does one behind any other maximised window. There is no
    #: page-level occlusion API to use instead — Chrome computes occlusion
    #: internally and does not expose it to the page.
    #:
    #: So this catches minimised and background-tab, and nothing else.
    #:
    #: The real fix belongs to window surfacing, which has an HWND and can ask
    #: Windows directly (`IsIconic`, and the occlusion the compositor already
    #: knows). **When surfacing lands, replace this signal and delete this
    #: comment** rather than inheriting it.
    def sees(self, visible: bool) -> None:
        self._visible = bool(visible)
        if self.open and self.requires_visible and not self._visible:
            self.close(Why.HIDDEN)

    # -- the log -------------------------------------------------------

    def _microphone_free(self) -> bool:
        if self.check_microphone is None:
            return True
        try:
            return bool(self.check_microphone())
        except Exception:
            # A check that errors is a check that failed, same as arming.
            return False

    def _note(self, kind: str, detail: str) -> None:
        self._log.append((kind, detail))

    def drain(self) -> list[tuple[str, str]]:
        """Everything worth logging since the last call."""
        entries, self._log = self._log, []
        return entries


def build_window(config, check_microphone=None) -> Window:
    """A Window wired from config."""
    wake = config.wake
    return Window(
        seconds=wake.conversation_seconds,
        reopens=wake.conversation_reopens,
        requires_visible=wake.conversation_requires_visible,
        check_microphone=check_microphone,
    )
