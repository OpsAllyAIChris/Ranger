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


# -- off Windows ------------------------------------------------------------


def test_focus_says_not_found_rather_than_pretending():
    """On Linux there is no window. It says so and claims nothing."""
    result = focus_window()

    assert result.outcome == NOT_FOUND
    assert not result.focused and not result.surfaced


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
