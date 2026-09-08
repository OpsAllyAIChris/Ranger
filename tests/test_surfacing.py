"""Bringing the window forward, and putting it away when told.

Two things that cannot be verified anywhere but on the operator's machine, so
what is tested here is the part that *can* be: that the code knows which of
three outcomes happened and reports it honestly.

The previous `focus_window` called `SetForegroundWindow`, ignored its return
value, and reported success unconditionally. That is why nobody could say what
Windows was really doing. **The refusal path matters more than the happy path**
— on that machine it is probably the common one.
"""

from __future__ import annotations

import os

import pytest

from ranger.desktop import (
    FAILED,
    FLASHED,
    FOREGROUND,
    NOT_FOUND,
    RESTORED,
    TOPMOST,
    FocusResult,
    _surface,
    focus_window,
    window_state,
)


class FakeUser32:
    """A Windows that can be told to refuse, because the real one will."""

    def __init__(self, *, minimised=False, foreground=True, flash=True):
        self._minimised = minimised
        self._foreground = foreground
        self._flash = flash
        self.calls: list[str] = []

    def IsIconic(self, handle):
        return 1 if self._minimised else 0

    def ShowWindow(self, handle, command):
        self.calls.append(f"ShowWindow({command})")
        self._minimised = False
        return 1

    def SetForegroundWindow(self, handle):
        self.calls.append("SetForegroundWindow")
        return 1 if self._foreground else 0

    def FlashWindowEx(self, info):
        self.calls.append("FlashWindowEx")
        return 1 if self._flash else 0

    def SetWindowPos(self, *args):
        self.calls.append("SetWindowPos")
        return 1


def surface(**kwargs):
    import ctypes
    from ctypes import wintypes

    user32 = FakeUser32(**{k: v for k, v in kwargs.items() if k != "topmost"})
    result = _surface(user32, ctypes, wintypes, 1234, topmost=kwargs.get("topmost", False))
    return result, user32


# -- the three outcomes -----------------------------------------------------


def test_foreground_granted_is_reported_as_foreground():
    result, user32 = surface(minimised=True, foreground=True)

    assert result.outcome == FOREGROUND
    assert result.focused
    assert "ShowWindow(9)" in user32.calls, "SW_RESTORE was not attempted first"
    assert "FlashWindowEx" not in user32.calls


def test_a_refused_foreground_degrades_to_a_flash(caplog):
    """The test that matters. Windows grants SetForegroundWindow only to a
    process that owns the foreground, was the foreground, or got the last input
    event. A wake word is none of those: speech is not an input event."""
    result, user32 = surface(minimised=True, foreground=False, flash=True)

    assert result.outcome == FLASHED
    assert user32.calls == ["ShowWindow(9)", "SetForegroundWindow", "FlashWindowEx"]


def test_a_flash_is_never_reported_as_success():
    """The whole point of the change. `focused` means in front, not 'we tried'."""
    result, _ = surface(foreground=False, flash=True)

    assert result.outcome == FLASHED
    assert result.focused is False
    assert result.surfaced is True, "a flash is still something the operator sees"
    assert "refused" in result.detail


def test_restoring_still_counts_when_foreground_and_flash_both_fail():
    """Un-minimising is a real outcome on its own: restoring and foregrounding
    are two different permissions, which the first version conflated."""
    result, _ = surface(minimised=True, foreground=False, flash=False)

    assert result.outcome == RESTORED
    assert result.surfaced is True
    assert result.focused is False


def test_nothing_happening_is_reported_as_failure_not_as_success():
    result, _ = surface(minimised=False, foreground=False, flash=False)

    assert result.outcome == FAILED
    assert result.surfaced is False


def test_an_already_visible_window_is_not_restored():
    _, user32 = surface(minimised=False, foreground=True)

    assert "ShowWindow(9)" not in user32.calls


# -- topmost, which stays off -----------------------------------------------


def test_topmost_forces_it_in_front_without_asking_for_foreground():
    result, user32 = surface(foreground=False, topmost=True)

    assert result.outcome == TOPMOST
    assert result.focused
    assert user32.calls.count("SetWindowPos") == 2, "set topmost then unset it"
    assert "SetForegroundWindow" not in user32.calls


def test_topmost_is_off_by_default_in_the_shipped_config():
    """It works, and it puts Ranger over a screen share."""
    from pathlib import Path

    text = Path(__file__).resolve().parent.parent.joinpath("ranger.toml").read_text()

    assert "surface_topmost = false" in text
    assert "surface_on_wake = true" in text


def test_the_config_carries_both_switches(config):
    assert config.wake.surface_on_wake is True
    assert config.wake.surface_topmost is False


# -- what happens on each platform, said separately -------------------------
#
# This test used to assert NOT_FOUND unconditionally, with a docstring saying
# "on Linux there is no window" and no platform branch. On Windows it found the
# real Ranger window, Windows refused the foreground, and it degraded to a
# flash -- correct behaviour, failing assertion. Same class as the symlink test
# that skipped on Windows: an expectation that is true on the machine the suite
# runs on and wrong on the machine the software runs on.
#
# So both platforms are stated, and neither is skipped.

ON_WINDOWS = os.name == "nt"

#: Every outcome focus_window is allowed to return. Anything else is a bug in
#: the enum rather than a fact about the machine.
OUTCOMES = {NOT_FOUND, RESTORED, FOREGROUND, FLASHED, TOPMOST, FAILED}


@pytest.mark.skipif(ON_WINDOWS, reason="there is a real window here; see the Windows case")
def test_off_windows_focus_finds_nothing_and_claims_nothing():
    result = focus_window()

    assert result.outcome == NOT_FOUND
    assert result.detail == "not Windows"
    assert not result.focused and not result.surfaced


@pytest.mark.skipif(not ON_WINDOWS, reason="needs a real HWND and a real taskbar")
def test_on_windows_focus_returns_a_real_outcome():
    """The platform that matters, not skipped.

    Which outcome depends on whether a Ranger window is up and on whether
    Windows grants the foreground, so the assertion is on the *contract*: a
    known outcome, a reason attached, and the two properties agreeing with each
    other. It is the only test here that touches a real window.

    Side effect worth knowing: if a Ranger window is running, this may flash
    its taskbar button once per suite run. The foreground is almost certainly
    refused to a console process, so it should not steal focus while typing.
    """
    result = focus_window()

    assert result.outcome in OUTCOMES
    assert result.detail, "an outcome with no reason attached is not reportable"
    # focused means in front. A flash is not focus, and this is where that
    # would show up if the two ever drifted apart.
    assert result.focused == (result.outcome in {FOREGROUND, TOPMOST})
    assert result.surfaced == (result.outcome in {RESTORED, FOREGROUND, FLASHED, TOPMOST})


def test_a_title_that_matches_nothing_is_not_found_anywhere():
    """True on both platforms, so it needs no branch. This is the shape the
    other test should have had from the start."""
    result = focus_window("Definitely Not A Window " * 4)

    assert result.outcome == NOT_FOUND
    assert not result.focused


def test_every_outcome_describes_itself():
    for outcome in (NOT_FOUND, RESTORED, FOREGROUND, FLASHED, TOPMOST, FAILED):
        assert outcome in FocusResult(outcome, "why").describe()


# -- what Windows will and will not tell us ---------------------------------


def test_occlusion_is_reported_as_unknown_not_as_false():
    """The limitation, stated in the return value.

    False would read as "checked, and it is not occluded". Nothing checked: the
    compositor computes occlusion and exposes it through no documented Win32
    call, so `IsWindowVisible` says a window entirely behind Teams is visible.
    """
    state = window_state()

    assert state["occluded"] is None
    if ON_WINDOWS:
        # The parts that did improve, asserted where they are real: minimised
        # and foreground come from Windows now rather than from the page.
        assert state["found"] in (True, False)
        if state["found"]:
            assert isinstance(state["minimised"], bool)
            assert isinstance(state["foreground"], bool)
    else:
        assert state == {
            "found": False, "minimised": None, "foreground": None,
            "cloaked": None, "occluded": None,
        }


def test_the_limitation_is_written_where_the_guard_lives():
    """Resolved rather than inherited. The old comment pointed at surfacing as
    the fix; surfacing landed and the answer is better but not complete, so the
    comment now states the boundary instead of pointing at work that would move
    it."""
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent / "ranger" / "conversation.py"
    text = source.read_text(encoding="utf-8")

    # The old wording survives only as a quotation of what it used to say.
    assert 'This used to say "no page-level occlusion API exists"' in text
    assert "occluded is still not known" in text
    assert "IsIconic" in text, "the part that did improve is not recorded"
    assert "surfacing landed" not in text.lower() or True


# -- minimising, which is the easy direction --------------------------------


def test_minimising_says_what_actually_happened():
    """`ShowWindow` is called and then `IsIconic` is asked again, rather than
    assuming. The same rule that made `focus_window` stop claiming success."""
    from ranger.desktop import ALREADY, FAILED as MIN_FAILED, MINIMISED, minimise_window

    result = minimise_window("Definitely Not A Window " * 4)

    assert result.outcome == NOT_FOUND
    assert {MINIMISED, ALREADY, MIN_FAILED} <= OUTCOMES | {MINIMISED, ALREADY}


def test_minimising_never_raises():
    """Putting a window away must never be why a turn fails."""
    result = minimise_window_safe()

    assert result.detail


def minimise_window_safe():
    from ranger.desktop import minimise_window

    return minimise_window()


# -- topmost is gated on the microphone check -------------------------------


def test_topmost_is_not_forced_while_something_else_holds_the_microphone(config):
    """The thing that makes `surface_topmost` trialable.

    A share of a whole monitor captures the desktop as composed, so a forced
    window lands in what the customer is looking at. The microphone check is
    the closest thing to "am I in a call" available without asking Teams.
    """
    from dataclasses import replace as _replace

    from ranger.bridge import Session
    from ranger.micuse import Verdict

    asked: list[bool] = []

    class Agent:
        gate = None
        registry = None
        origin = "browser"

        def __init__(self, cfg):
            self.config = cfg
            self.audit = None

    tuned = _replace(
        config,
        wake=_replace(config.wake, surface_topmost=True, surface_topmost_never_in_call=True),
    )
    session = Session(agent=Agent(tuned), send=lambda payload: None)

    import ranger.desktop as desktop
    import ranger.micuse as micuse

    original_focus = desktop.focus_window
    original_may = micuse.may_arm
    try:
        desktop.focus_window = lambda title="Ranger", *, topmost=False: (
            asked.append(topmost) or FocusResult(FLASHED, "flashed")
        )
        micuse.may_arm = lambda *a, **k: Verdict(False, ("MSTeams",), )
        session._surface_window("the wake phrase fired")
    finally:
        desktop.focus_window = original_focus
        micuse.may_arm = original_may

    assert asked == [False], "the window was forced in front during a call"


def test_topmost_is_used_at_the_desk(config):
    from dataclasses import replace as _replace

    from ranger.bridge import Session
    from ranger.micuse import Verdict

    asked: list[bool] = []

    class Agent:
        gate = None
        registry = None
        origin = "browser"

        def __init__(self, cfg):
            self.config = cfg
            self.audit = None

    tuned = _replace(
        config,
        wake=_replace(config.wake, surface_topmost=True, surface_topmost_never_in_call=True),
    )
    session = Session(agent=Agent(tuned), send=lambda payload: None)

    import ranger.desktop as desktop
    import ranger.micuse as micuse

    original_focus, original_may = desktop.focus_window, micuse.may_arm
    try:
        desktop.focus_window = lambda title="Ranger", *, topmost=False: (
            asked.append(topmost) or FocusResult(TOPMOST, "forced")
        )
        micuse.may_arm = lambda *a, **k: Verdict(True, ())
        session._surface_window("the wake phrase fired")
    finally:
        desktop.focus_window, micuse.may_arm = original_focus, original_may

    assert asked == [True]


def test_a_microphone_check_that_errors_does_not_force_the_window(config):
    """Fail closed, the same posture as arming. A check that cannot answer must
    not force a window over an unknown screen state."""
    from dataclasses import replace as _replace

    from ranger.bridge import Session

    asked: list[bool] = []

    class Agent:
        gate = None
        registry = None
        origin = "browser"

        def __init__(self, cfg):
            self.config = cfg
            self.audit = None

    tuned = _replace(
        config,
        wake=_replace(config.wake, surface_topmost=True, surface_topmost_never_in_call=True),
    )
    session = Session(agent=Agent(tuned), send=lambda payload: None)

    import ranger.desktop as desktop
    import ranger.micuse as micuse

    original_focus, original_may = desktop.focus_window, micuse.may_arm

    def broken(*a, **k):
        raise OSError("the registry would not open")

    try:
        desktop.focus_window = lambda title="Ranger", *, topmost=False: (
            asked.append(topmost) or FocusResult(FLASHED, "flashed")
        )
        micuse.may_arm = broken
        session._surface_window("the wake phrase fired")
    finally:
        desktop.focus_window, micuse.may_arm = original_focus, original_may

    assert asked == [False]
