"""'That's all Jarvis': put the window away, stay armed.

A whole-utterance rule on a completed transcript, **not a second hotword**. By
the time the phrase is said the transcript already exists, so a second model
would double the false-fire surface and need its own hour on a GPU to answer a
question already answered.

The test that earns its place here is `test_the_counterexample`. Whole-utterance
rules rot into substring matches under later edits — someone adds a `in` where
there was an `==`, every other test still passes, and the operator loses their
window mid-sentence for the rest of the year. That test is what stands on it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from ranger.conversation import Why, Window
from ranger.wake import dismissals, is_dismissal


def dismissed(text: str, phrase: str = "hey jarvis") -> bool:
    return is_dismissal(text, phrase)


# -- real transcripts, verbatim --------------------------------------------
#
# The first version required the whole utterance to be the dismissal and never
# fired once in a live session. Three things stack, and none of them is a bug:
#
#   - the wake word echoes into the transcript as a leading "Jarvis."
#   - conversation mode means the previous reply's tail is transcribed into the
#     next turn, so the utterance is almost never clean
#   - Deepgram writes "That's all, Jarvis" with a comma
#
# So the design assumed a clean utterance and the pipeline never produces one.
# These four are copied out of the operator's log exactly as Deepgram wrote
# them, trailing "So" included. **Do not tidy them.** Synthetic clean input is
# what let the first version pass its tests and fail every real attempt.

REAL_TRANSCRIPTS = [
    "Jarvis. That's all Jarvis.",
    "In writing. Anything else? That's all, Jarvis.",
    (
        "Got it. I'll go by Jarvis from here on. Done. That's locked in. "
        "Anything else? Well, no. That's all Jarvis."
    ),
    "Jarvis. That's all Jarvis. So",
]


@pytest.mark.parametrize("said", REAL_TRANSCRIPTS)
def test_a_real_transcript_dismisses(said):
    assert dismissed(said) is True, said


def test_the_leading_wake_word_echo_does_not_prevent_it():
    """The echo sits in front, and a trailing-clause rule never looks there."""
    assert dismissed("Jarvis. That's all Jarvis.") is True


def test_deepgrams_comma_does_not_split_the_phrase():
    """Commas are not clause separators, and this is the whole reason why:
    splitting on one would tear "That's all, Jarvis" in half."""
    assert dismissed("Anything else? That's all, Jarvis.") is True


def test_a_hanging_fragment_at_the_end_is_ignored():
    """Deepgram ends an utterance on a stray word constantly. Treating that
    "So" as the final clause is the difference between working and never
    firing."""
    assert dismissed("That's all Jarvis. So") is True
    assert dismissed("That's all Jarvis. Um") is True
    assert dismissed("That's all Jarvis. Ok then") is True


def test_a_substantive_trailing_clause_is_not_ignored():
    """The fragment rule is for filler, and only filler. Anything carrying a
    request blocks the dismissal, which is what keeps this from being a
    substring match with extra steps."""
    assert dismissed("That's all Jarvis. Tell Rusty we're done") is False
    assert dismissed("That's all Jarvis. What about Illes") is False


# -- the counterexample ----------------------------------------------------


def test_the_counterexample():
    """The clause the operator named, and it must never be a dismissal.

    It survives the move from whole-utterance to trailing-clause for a reason
    rather than by luck: it is a single clause, and that clause is not the
    dismissal phrase. A substring rule takes it; neither of these does.
    """
    assert dismissed("tell Rusty that's all we need from Jarvis") is False


def test_the_counterexample_holds_wherever_it_sits():
    """Not just at the end. Nothing about its position is what saves it."""
    assert dismissed("Tell Rusty that's all we need from Jarvis. Anything else?") is False
    assert dismissed("First, tell Rusty that's all we need from Jarvis") is False


def test_a_dismissal_after_a_request_now_fires_and_that_is_deliberate():
    """The one behaviour that changed, stated so it is a decision and not a
    surprise.

    This is the operator's 12:25 transcript. Under the whole-utterance rule it
    did not dismiss. Under the trailing-clause rule it does, because the last
    thing said was the dismissal, as its own clause, and they meant it.
    """
    said = (
        "Just checking the name sticks. Tell Rusty that's all we need from "
        "Jarvis. That's all Jarvis."
    )

    assert dismissed(said) is True


# -- and the ones that should fire -----------------------------------------


@pytest.mark.parametrize(
    "said",
    [
        "that's all Jarvis",
        "That's all, Jarvis.",
        "  thats all jarvis  ",
        "that is all Jarvis",
        "thanks Jarvis",
        "thank you Jarvis",
        "that's all",
        "goodbye Jarvis",
        "bye Jarvis",
        "we're done Jarvis",
    ],
)
def test_the_whole_utterance_forms_fire(said):
    assert dismissed(said) is True


def test_punctuation_and_case_do_not_matter():
    assert dismissed("THAT'S ALL, JARVIS!") is True


def test_an_empty_utterance_is_not_a_dismissal():
    assert dismissed("") is False
    assert dismissed("   ") is False


def test_the_phrase_decides_the_name():
    """Derived from the wake phrase's last word, so training "hey ranger"
    changes the dismissal without a second edit."""
    assert dismissed("that's all ranger", phrase="hey ranger") is True
    assert dismissed("that's all jarvis", phrase="hey ranger") is False
    assert "that's all ranger" in dismissals("hey ranger")


def test_a_transcription_wobble_does_not_fire():
    """Deepgram will produce near misses. None of them is a dismissal."""
    for said in ("that's all jarvis please", "ok that's all jarvis", "that's alright jarvis"):
        assert dismissed(said) is False, said


# -- what it does, and what it must not --------------------------------------


def test_a_dismissal_closes_an_open_window():
    window = Window(seconds=8.0, reopens=3, requires_visible=False)
    window.woke()
    window.sent(0)
    window.played(0)
    window.finished()
    assert window.opens(0.0)[0] is True

    window.close(Why.DISMISSED, "that's all jarvis")

    assert not window.open


def test_a_dismissal_spends_nothing():
    """It closes the window and leaves the budget alone. The wake word still
    refills it, as before."""
    window = Window(seconds=8.0, reopens=3, requires_visible=False)
    window.woke()
    window.sent(0)
    window.played(0)
    window.finished()
    window.opens(0.0)

    window.close(Why.DISMISSED)

    assert not window.spent, "a dismissal spent the reopen budget"
    assert window.used == 1


def test_a_dismissal_reaches_the_log_with_its_reason():
    window = Window(seconds=8.0, reopens=3, requires_visible=False)
    window.woke()
    window.sent(0)
    window.played(0)
    window.finished()
    window.opens(0.0)

    window.close(Why.DISMISSED, "that's all jarvis")

    assert any(kind == "closed dismissed" for kind, _ in window.drain())


# -- through the bridge -----------------------------------------------------


@pytest.fixture
def session(config):
    from ranger.audit import AuditLog
    from ranger.bridge import Session
    from ranger.conversation import build_window
    from ranger.core import Ranger
    from ranger.knowledge import KnowledgeLoader
    from ranger.testing import ScriptedProvider
    from ranger.toolset import build_registry
    from ranger.vault import Vault

    sent: list[dict] = []
    vault = Vault(config.vault)

    class Ears:
        def __init__(self):
            self.text = "that's all Jarvis"

        async def transcribe(self, audio, *, hints=True, content_type="audio/wav"):
            from ranger.stt import Transcript

            return Transcript(text=self.text, confidence=0.97, words=(), model="nova-3")

    class Listening:
        armed = True

        def listen(self, seconds=None):
            return True, "listening for a follow-up"

        def disarm(self, why=""):
            self.armed = False
            return why

    class FakeListener:
        def __init__(self):
            self.hotword = Listening()

        def stop(self):
            pass

    ears = Ears()
    built = Session(
        agent=Ranger(
            config=config,
            provider=ScriptedProvider([{"text": "should never run"}]),
            registry=build_registry(config, vault),
            vault=vault,
            knowledge_loader=KnowledgeLoader(vault, config.vault, config.knowledge),
            audit=AuditLog(vault, config.vault.log),
            origin="browser",
        ),
        send=sent.append,
        transcriber=ears,
    )
    built.listener = FakeListener()
    built.window = build_window(config, check_microphone=lambda: True)
    built.window.requires_visible = False
    built.window.woke()
    return built, sent, ears


async def test_a_spoken_dismissal_never_becomes_a_turn(session):
    """It costs no tokens and never enters the conversation history. The
    transcript already exists by the time it is checked."""
    built, sent, _ = session
    from ranger.listen import Utterance

    await built._run_audio(Utterance(audio=b"RIFF", mime="audio/wav", seconds=1.0))

    assert not built.agent.messages, "the dismissal reached the model"
    assert any(event.get("kind") == "dismissed_aloud" for event in sent)


async def test_a_dismissal_does_not_disarm(session):
    """The microphone stays on. A phrase that changed the safety state is
    exactly what the operator did not want: escape and the control are the only
    things that disarm."""
    built, _, _ = session
    from ranger.listen import Utterance

    await built._run_audio(Utterance(audio=b"RIFF", mime="audio/wav", seconds=1.0))

    assert built.listener is not None
    assert built.listener.hotword.armed is True


async def test_the_counterexample_runs_as_a_turn(session):
    """End to end, through the real path: it transcribes, it is not a
    dismissal, and it becomes a turn."""
    built, sent, ears = session
    ears.text = "tell Rusty that's all we need from Jarvis"
    from ranger.listen import Utterance

    await built._run_audio(Utterance(audio=b"RIFF", mime="audio/wav", seconds=1.0))

    assert not any(event.get("kind") == "dismissed_aloud" for event in sent)
    assert built.agent.messages, "the request was swallowed as a dismissal"


async def test_a_dismissal_is_written_to_the_audit_log(session, config):
    built, _, _ = session
    from ranger.audit import AuditLog
    from ranger.listen import Utterance
    from ranger.vault import Vault

    await built._run_audio(Utterance(audio=b"RIFF", mime="audio/wav", seconds=1.0))

    written = AuditLog(Vault(config.vault), config.vault.log).read()
    assert "hands-free dismissed" in written
    assert "still armed" in written


# -- the minimise itself ----------------------------------------------------
#
# `window.blur()` was tried first and is ignored in Chrome's app mode --
# confirmed on the operator's machine over several attempts. So the dismissal
# goes through the window handle, and these tests exist because the browser
# path is now KNOWN not to work here: a regression back to it would look fine
# in every other test.


async def test_the_dismissal_minimises_through_the_window_handle(session, monkeypatch):
    """Not a browser blur. ShowWindow(SW_MINIMIZE) is not foreground-gated, so
    it does not hit the refusal that makes surfacing degrade to a flash."""
    from ranger.desktop import FocusResult
    from ranger.listen import Utterance

    called: list[str] = []

    def minimise(title="Ranger"):
        called.append(title)
        return FocusResult("minimised", "minimised")

    monkeypatch.setattr("ranger.desktop.minimise_window", minimise)
    built, sent, _ = session

    await built._run_audio(Utterance(audio=b"RIFF", mime="audio/wav", seconds=1.0))

    assert called, "the dismissal did not reach the ctypes minimise"


def test_the_browser_no_longer_tries_to_blur_itself():
    """It does not work in app mode, so it must not look like the mechanism.

    A future edit putting it back would pass every behavioural test in this
    file, because the browser is not exercised by any of them.
    """
    from pathlib import Path as _Path

    shell = _Path(__file__).resolve().parent.parent / "ranger" / "web" / "shell.js"
    # Comments stripped: the reason it was removed is written there and should
    # stay, so a future reader does not helpfully add it back.
    code = "\n".join(
        line for line in shell.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("//")
    )

    assert "window.blur()" not in code
    assert "dismissed_aloud" in code, "the browser stopped hearing about it entirely"


async def test_the_log_says_whether_the_minimise_happened(session, monkeypatch, config):
    """What happened, not what was attempted. The same rule that made
    focus_window stop reporting unconditional success."""
    from ranger.audit import AuditLog
    from ranger.desktop import FocusResult
    from ranger.listen import Utterance
    from ranger.vault import Vault

    monkeypatch.setattr(
        "ranger.desktop.minimise_window",
        lambda title="Ranger": FocusResult("failed", "ShowWindow was called and the window is still up"),
    )
    built, _, _ = session

    await built._run_audio(Utterance(audio=b"RIFF", mime="audio/wav", seconds=1.0))

    written = AuditLog(Vault(config.vault), config.vault.log).read()
    assert "dismissed failed" in written
    assert "still up" in written, "the log does not say what actually happened"


async def test_a_minimise_that_raises_does_not_break_the_dismissal(session, monkeypatch):
    """Putting a window away must never be why a turn fails."""
    from ranger.listen import Utterance

    def explode(title="Ranger"):
        raise OSError("user32 fell over")

    monkeypatch.setattr("ranger.desktop.minimise_window", explode)
    built, sent, _ = session

    await built._run_audio(Utterance(audio=b"RIFF", mime="audio/wav", seconds=1.0))

    assert any(event.get("kind") == "dismissed_aloud" for event in sent)
    assert built.listener.hotword.armed is True


async def test_minimising_does_not_disarm_or_close_the_socket(session, monkeypatch):
    """The two things the operator asked to be held."""
    from ranger.desktop import FocusResult
    from ranger.listen import Utterance

    monkeypatch.setattr(
        "ranger.desktop.minimise_window",
        lambda title="Ranger": FocusResult("minimised", "minimised"),
    )
    built, sent, _ = session

    await built._run_audio(Utterance(audio=b"RIFF", mime="audio/wav", seconds=1.0))

    assert built.listener is not None, "the listener was thrown away"
    assert built.listener.hotword.armed is True, "the dismissal disarmed the hotword"
    assert built.window is not None, "the conversation window was destroyed"
    # And nothing told the front end to go away.
    assert not any(event.get("kind") == "stopped" for event in sent)

