"""Conversation mode: the window that stays open after Ranger stops talking.

The operator named the two failures that matter before this was built, and both
of them are the kind that a happy-path test passes straight through.

The first is not "does it open" but "does it open three times". Sentences are
spoken as they are produced, so the browser's speaker queue empties several
times in one reply, and anchoring on that alone opens a window per drain. Every
test here that touches the anchor uses a reply of several chunks with the queue
draining between them, because a single chunk reply proves nothing and would go
on passing forever.

The second is that a spoken "yes" is never consent. A follow-up arrives with no
wake phrase in front of it and reads exactly like continuation, which is what
makes it tempting. It is not consent, and a card opening closes the window.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from ranger.conversation import Why, Window


def armed(**kwargs) -> Window:
    """A window whose turn has begun, ready to be fed audio events."""
    window = Window(seconds=8.0, reopens=3, requires_visible=False, **kwargs)
    window.woke()
    return window


def reply(window: Window, chunks: int, *, drain_between: bool = True) -> None:
    """A reply of `chunks` sentences, with the queue draining between them.

    This is the shape that matters. Each drain is a `spoken` message from the
    browser, and every one of them except the last arrives while the turn is
    still running.
    """
    for index in range(chunks):
        window.sent(index)
        if drain_between or index == chunks - 1:
            window.played(index)
    window.finished()


# -- the anchor -------------------------------------------------------------


def test_a_reply_that_drains_three_times_opens_one_window():
    """The failure the whole anchor exists for.

    Three sentences, three drained queues, one window. Anchoring on the
    browser's "I stopped talking" alone would open three.
    """
    window = armed()
    opens = []

    for index in range(3):
        window.sent(index)
        window.played(index)          # the queue empties mid-reply, twice
        opens.append(window.opens(index)[0])
    window.finished()
    opens.append(window.opens(9.0)[0])

    assert opens == [False, False, False, True]
    assert window.used == 1


def test_the_window_does_not_open_while_ranger_is_still_speaking():
    """The other half. Anchoring on the turn alone spends the eight seconds
    while the operator is still being talked at."""
    window = armed()
    window.sent(0)
    window.sent(1)
    window.finished()               # the turn is done; chunk 1 is still playing

    assert window.opens(0.0) == (False, "not ready")

    window.played(1)
    assert window.opens(0.0)[0] is True


def test_a_drain_after_the_window_opened_does_not_open_it_again():
    """The latch. A late `spoken` message must not reopen anything."""
    window = armed()
    reply(window, 2)
    assert window.opens(0.0)[0] is True

    window.played(1)
    assert window.opens(1.0) == (False, "not ready")
    assert window.used == 1


def test_a_turn_that_says_nothing_opens_no_window():
    """No reply means nothing to listen after."""
    window = armed()
    window.finished()

    assert window.opens(0.0) == (False, "not ready")
    assert not window.open


def test_a_second_turn_gets_its_own_window():
    window = armed()
    reply(window, 2)
    window.opens(0.0)
    window.heard("what about Illes")

    window.begin()
    reply(window, 3)

    assert window.opens(10.0)[0] is True
    assert window.used == 2


# -- the reopen budget ------------------------------------------------------


def test_the_budget_is_spent_after_three_windows():
    window = armed()
    for _ in range(3):
        window.begin()
        reply(window, 2)
        assert window.opens(0.0)[0] is True
        window.heard("and Rusty's")

    window.begin()
    reply(window, 2)
    opened, why = window.opens(0.0)

    assert opened is False
    assert why == Why.CAP.value


def test_only_the_wake_word_refills_the_budget():
    """Not elapsed time. A budget that refilled after a quiet period would mean
    a room with a fan refills it forever, and the cap would not be a cap.
    """
    window = armed()
    for _ in range(3):
        window.begin()
        reply(window, 1)
        window.opens(0.0)
        window.heard("more")
    assert window.spent

    # An hour goes by. Still spent.
    assert window.tick(3600.0) is None
    assert window.spent

    window.woke()
    assert not window.spent
    assert window.used == 0


def test_a_cap_of_zero_turns_conversation_mode_off():
    window = Window(seconds=8.0, reopens=0, requires_visible=False)
    window.woke()
    reply(window, 2)

    assert window.opens(0.0) == (False, Why.CAP.value)


# -- what closes it ---------------------------------------------------------


def _open(**kwargs) -> Window:
    window = armed(**kwargs)
    reply(window, 2)
    assert window.opens(0.0)[0] is True
    return window


def test_silence_closes_it():
    window = _open()

    assert window.tick(8.0) is Why.TIMER
    assert not window.open


def test_it_is_still_open_a_moment_before_the_timer():
    window = _open()

    assert window.tick(7.9) is None
    assert window.open


def test_noise_that_transcribes_to_nothing_closes_it():
    """Otherwise a room with a fan holds the microphone open through the whole
    budget: each window captures noise, and each capture would reopen it."""
    window = _open()

    assert window.heard("   ") is Why.SILENT
    assert not window.open


def test_a_real_follow_up_closes_it_too():
    """The next window is opened by the next turn finishing, not by this one
    staying open across it."""
    window = _open()

    assert window.heard("what about Illes Foods") is Why.SPOKE
    assert not window.open


def test_a_card_closes_it_and_spends_the_budget():
    """A window held open under a confirmation card is an open microphone next
    to a decision. Hard close: the operator says the phrase again."""
    window = _open()

    window.close(Why.GATED)

    assert not window.open
    assert window.spent, "a gated action spends the budget, not just this window"


def test_typing_closes_it_and_spends_the_budget():
    window = _open()

    window.close(Why.TYPED)

    assert not window.open
    assert window.spent


def test_disarming_closes_it_without_spending_the_budget():
    """Disarming already means the phrase is needed again, so there is nothing
    for spending the budget to protect against."""
    window = _open()

    window.close(Why.DISARMED)

    assert not window.open
    assert not window.spent


# -- the microphone check ---------------------------------------------------


def test_it_will_not_open_while_something_else_has_the_microphone():
    """Checked on open as well as on the poll: a call that started four seconds
    ago would otherwise not be noticed for another one."""
    free = [False]
    window = armed(check_microphone=lambda: free[0])
    reply(window, 2)

    assert window.opens(0.0) == (False, Why.MIC_CHECK.value)
    assert not window.open


def test_a_call_starting_mid_window_closes_it():
    free = [True]
    window = _open(check_microphone=lambda: free[0])

    free[0] = False

    assert window.tick(1.0) is Why.MIC_CHECK
    assert not window.open


def test_a_microphone_check_that_errors_counts_as_taken():
    """Fail closed, the same rule as arming. The operator inverted this once
    already and it must not come back."""
    def broken():
        raise OSError("the registry would not open")

    window = armed(check_microphone=broken)
    reply(window, 2)

    assert window.opens(0.0) == (False, Why.MIC_CHECK.value)


# -- the not-visible guard --------------------------------------------------


def test_it_will_not_open_behind_a_window_nobody_can_see():
    """Until the window can bring itself to the front, an open microphone whose
    only indication is on a minimised window indicates nothing."""
    window = Window(seconds=8.0, reopens=3, requires_visible=True)
    window.woke()
    window.sees(False)
    reply(window, 2)

    assert window.opens(0.0) == (False, Why.HIDDEN.value)


def test_the_page_going_away_closes_an_open_window():
    window = Window(seconds=8.0, reopens=3, requires_visible=True)
    window.woke()
    reply(window, 2)
    assert window.opens(0.0)[0] is True

    window.sees(False)

    assert not window.open


def test_the_guard_can_be_turned_off_once_surfacing_lands():
    window = Window(seconds=8.0, reopens=3, requires_visible=False)
    window.woke()
    window.sees(False)
    reply(window, 2)

    assert window.opens(0.0)[0] is True


# -- the log ----------------------------------------------------------------


def test_every_open_and_close_is_logged_with_a_reason():
    """A week of these is how the cap and the timer get set with data. Without
    the reason they are a count of nothing."""
    window = _open()
    window.tick(8.0)

    entries = dict(window.drain())

    assert "opened" in entries
    assert "closed timer" in entries
    assert "1 of 3" in entries["opened"]


def test_the_log_is_drained_not_accumulated():
    window = _open()
    window.drain()

    assert window.drain() == []


@pytest.mark.parametrize(
    "why",
    [Why.TIMER, Why.SILENT, Why.SPOKE, Why.GATED, Why.TYPED, Why.MIC_CHECK, Why.DISARMED],
)
def test_each_close_reason_reaches_the_log(why):
    window = _open()

    window.close(why)

    assert any(kind == f"closed {why.value}" for kind, _ in window.drain())


def test_a_close_that_closes_nothing_logs_nothing():
    """So `ranger log` counts windows, not calls to close."""
    window = armed()

    assert window.close(Why.TYPED) is False
    assert window.drain() == []


# -- config -----------------------------------------------------------------


def test_the_window_is_built_from_config(config):
    from ranger.conversation import build_window

    tuned = replace(
        config,
        wake=replace(
            config.wake,
            conversation_seconds=12.0,
            conversation_reopens=5,
            conversation_requires_visible=False,
        ),
    )
    window = build_window(tuned)

    assert window.seconds == 12.0
    assert window.reopens == 5
    assert window.requires_visible is False


def test_conversation_settings_are_in_the_shipped_config():
    """Not hand-edited into a local file. ranger.toml carries the defaults and
    the reason for each, and ranger.local.toml overrides them."""
    from pathlib import Path

    text = Path(__file__).resolve().parent.parent.joinpath("ranger.toml").read_text(
        encoding="utf-8"
    )

    assert "conversation_seconds = 8.0" in text
    assert "conversation_reopens = 3" in text
    assert "conversation_requires_visible = true" in text


# -- end to end, through the real bridge ------------------------------------
#
# Everything above tests the rules. These test the wiring, which is where the
# anchor either works or opens three windows an answer.


@pytest.fixture
def talking(config):
    """A session with a voice, a card gate, and a window, but no microphone.

    The hotword is a stand-in: what is under test is what the bridge does with
    the events, not whether openWakeWord fires, and requiring a real model here
    would mean this suite only ran on a machine that had one.
    """
    from ranger.audit import AuditLog
    from ranger.bridge import Session
    from ranger.conversation import build_window
    from ranger.core import Ranger
    from ranger.gate import SocketGate
    from ranger.knowledge import KnowledgeLoader
    from ranger.testing import ScriptedProvider
    from ranger.toolset import build_registry
    from ranger.vault import Vault

    (config.vault.memory / "facts.md").write_text(
        "- 2026-09-01 | Chris prefers morning meetings\n", encoding="utf-8"
    )

    sent: list[dict] = []
    vault = Vault(config.vault)

    class Mouth:
        """One chunk of audio per sentence, so a reply is several chunks."""

        async def stream(self, text, *, before="", after=""):
            yield b"\x00" * max(1, len(text))

    class Listening:
        """A hotword that records what it was told to do."""

        def __init__(self):
            self.listened: list[float] = []
            self.armed = True

        def listen(self, seconds=None):
            self.listened.append(seconds)
            return True, "listening for a follow-up"

        def disarm(self, why=""):
            self.armed = False
            return why

    class FakeListener:
        def __init__(self):
            self.hotword = Listening()

        def stop(self):
            pass

    session = Session(
        agent=Ranger(
            config=config,
            provider=ScriptedProvider([
                {"text": "Illes is quiet. Rod has not replied. I would call him today. Rusty is fine."}
            ]),
            registry=build_registry(config, vault),
            vault=vault,
            knowledge_loader=KnowledgeLoader(vault, config.vault, config.knowledge),
            gate=SocketGate(lambda payload: None),
            audit=AuditLog(vault, config.vault.log),
            origin="browser",
        ),
        send=sent.append,
        speaker=Mouth(),
    )
    session.listener = FakeListener()
    session.window = build_window(config, check_microphone=lambda: True)
    session.window.requires_visible = False
    session.window.woke()
    return session, sent


def opened(sent) -> list[dict]:
    return [event for event in sent if event.get("kind") == "window" and event.get("open")]


async def test_a_multi_chunk_reply_opens_exactly_one_window(talking):
    """The failure the anchor exists for, through the real bridge.

    The reply is three sentences, so three `speech` events go out and the
    browser's queue drains between them. Every drain arrives as a `spoken`
    message while the turn is still running. One window.
    """
    session, sent = talking

    await session._run("where are we on Illes")

    chunks = [event for event in sent if event.get("kind") == "speech"]
    assert len(chunks) >= 3, "a single chunk reply would prove nothing here"

    # The browser plays each chunk and reports its queue drained, in order.
    for chunk in chunks:
        await session.handle(json.dumps({"type": "spoken", "index": chunk["index"]}))

    assert len(opened(sent)) == 1
    assert session.window.open
    assert session.listener.hotword.listened == [session.window.seconds]


async def test_draining_between_chunks_before_the_turn_ends_opens_nothing(talking):
    """The same events, interleaved the way they really arrive."""
    session, sent = talking

    await session.handle(json.dumps({"type": "spoken", "index": 0}))
    await session.handle(json.dumps({"type": "spoken", "index": 1}))
    assert opened(sent) == []

    await session._run("where are we on Illes")
    chunks = [event for event in sent if event.get("kind") == "speech"]
    for chunk in chunks:
        await session.handle(json.dumps({"type": "spoken", "index": chunk["index"]}))

    assert len(opened(sent)) == 1


async def test_a_spoken_yes_inside_the_window_is_not_consent(talking, config):
    """The one the operator named, and the reason the card is a hard close.

    A follow-up arrives with no wake phrase in front of it and reads exactly
    like continuation, which is what makes "yes" tempting to treat as an
    answer. It is not. The card holds, the inbox gets the notice, and approval
    still needs the keyboard.
    """
    session, sent = talking

    # Get a window genuinely open first, so what the card closes is real.
    await session._run("where are we on Illes")
    for chunk in [event for event in sent if event.get("kind") == "speech"]:
        await session.handle(json.dumps({"type": "spoken", "index": chunk["index"]}))
    assert session.window.open, "the rest of this test would prove nothing"

    from ranger.gate import ConfirmationRequest

    async def ask_and_hold():
        return await session.agent.gate.ask(
            ConfirmationRequest(
                action="Permanently remove from memory: morning meetings",
                tool="forget",
                payload={"fact": "morning meetings"},
                origin="browser",
                token="toolu_1",
            )
        )

    asking = asyncio.create_task(ask_and_hold())
    await asyncio.sleep(0)

    # The card opening closes the window and spends the budget.
    assert not session.window.open
    assert session.window.spent

    # Now the operator says "yes" out loud. It reaches the bridge as a turn,
    # which is the only thing a transcript can ever be.
    await session.handle(json.dumps({"type": "turn", "text": "yes"}))
    await asyncio.sleep(0)

    assert not asking.done(), "the gate answered without a click"

    # And a decision carrying the token is still what settles it.
    session.agent.gate.decide("toolu_1", True)
    decision = await asyncio.wait_for(asking, timeout=2)
    assert decision.approved

    asking = asyncio.create_task(ask_and_hold())
    await asyncio.sleep(0)
    assert not asking.done()
    session.agent.gate.decide("toolu_1", False)
    await asyncio.wait_for(asking, timeout=2)


async def test_a_card_closes_an_open_window(talking):
    """Not because a spoken yes could reach the gate -- it cannot -- but
    because an open microphone next to a decision is the wrong shape."""
    session, sent = talking

    await session._run("where are we on Illes")
    for chunk in [event for event in sent if event.get("kind") == "speech"]:
        await session.handle(json.dumps({"type": "spoken", "index": chunk["index"]}))
    assert session.window.open

    session.watch({"kind": "confirm_open", "token": "toolu_1", "action": "x", "tool": "forget"})

    assert not session.window.open
    assert session.window.spent


async def test_the_card_gate_is_watched_however_the_session_was_built(config):
    # config is the fixture; Agent below only needs to carry a gate.
    """The hook is in Session, not in build_session, so it cannot be lost by a
    caller that assembles its own."""
    from ranger.bridge import Session
    from ranger.gate import SocketGate

    class Agent:
        gate = SocketGate(lambda payload: None)

    session = Session(agent=Agent(), send=lambda payload: None)

    assert session.agent.gate.emit == session.watch


async def test_typing_closes_the_window(talking):
    session, sent = talking

    await session._run("where are we on Illes")
    for chunk in [event for event in sent if event.get("kind") == "speech"]:
        await session.handle(json.dumps({"type": "spoken", "index": chunk["index"]}))
    assert session.window.open

    await session.handle(json.dumps({"type": "turn", "text": "what about Rusty's"}))

    assert not session.window.open
    assert session.window.spent


async def test_the_page_going_hidden_closes_the_window(talking):
    session, sent = talking
    session.window.requires_visible = True

    await session._run("where are we on Illes")
    for chunk in [event for event in sent if event.get("kind") == "speech"]:
        await session.handle(json.dumps({"type": "spoken", "index": chunk["index"]}))
    assert session.window.open

    await session.handle(json.dumps({"type": "visible", "visible": False}))

    assert not session.window.open


async def test_a_follow_up_keeps_its_first_word(talking):
    """No phrase came before it, so there is nothing to strip. Stripping the
    phrase off a follow-up eats a real word out of the front of the request."""
    session, sent = talking
    captured: list[tuple] = []

    async def record(utterance, strip=""):
        captured.append((utterance, strip))

    session._run_audio = record

    # It starts a task, so the loop has to turn before the call is recorded.
    session._heard_hands_free(b"\x00" * 3200, follow_up=True)
    await asyncio.sleep(0)
    assert captured[-1][1] == ""

    session._heard_hands_free(b"\x00" * 3200, follow_up=False)
    await asyncio.sleep(0)
    assert captured[-1][1] == session.agent.config.wake.phrase


async def test_a_follow_up_does_not_refill_the_budget(talking):
    """Only the phrase does. Otherwise the cap counts to three forever."""
    session, sent = talking
    async def nothing(utterance, strip=""):
        return None

    session._run_audio = nothing
    session.window.used = 2

    session._heard_hands_free(b"\x00" * 3200, follow_up=True)
    assert session.window.used == 2

    session._heard_hands_free(b"\x00" * 3200, follow_up=False)
    assert session.window.used == 0


async def test_every_window_open_and_close_reaches_the_audit_log(talking, config):
    session, sent = talking

    await session._run("where are we on Illes")
    for chunk in [event for event in sent if event.get("kind") == "speech"]:
        await session.handle(json.dumps({"type": "spoken", "index": chunk["index"]}))
    session._close_window(Why.TIMER, "nothing was said")

    from ranger.audit import AuditLog
    from ranger.vault import Vault

    written = AuditLog(Vault(config.vault), config.vault.log).read()

    assert "window opened" in written
    assert "window closed timer" in written
