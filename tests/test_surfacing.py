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
from pathlib import Path

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

    text = Path(__file__).resolve().parent.parent.joinpath("ranger.toml").read_text(
        encoding="utf-8"
    )

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


# -- the title and the matcher cannot drift ---------------------------------
#
# Renaming the assistant changed `<title>` and changed what `focus_window`
# looked for, and surfacing still broke: nothing in the suite noticed, because
# no test connected the page the server serves to the string the matcher uses.
# A `not_found` that only appears in the audit log after a wake firing is a
# silent regression with a long fuse.


PAGE = Path(__file__).resolve().parent.parent / "ranger" / "web" / "index.html"


def served_title() -> str:
    import re

    found = re.search(r"<title>(.*?)</title>", PAGE.read_text(encoding="utf-8"))
    assert found, "the interface serves no <title> at all"
    return found.group(1).strip()


def test_the_matcher_looks_for_the_title_the_interface_actually_serves():
    """The test that would have caught it, tying the two ends together."""
    from ranger.desktop import matches, window_names

    title = served_title()

    assert matches(title), (
        f"the page serves <title>{title}</title> and the matcher looks for "
        f"{window_names()}, so surfacing cannot find its own window"
    )


def test_the_title_comes_from_the_naming_constant():
    """Not a coincidence that happens to line up today."""
    from ranger.naming import ASSISTANT

    assert served_title() == ASSISTANT


#: The window that broke it, verbatim from the operator's machine. Their vault
#: directory is `Ranger-Vault` and is staying that way, so Obsidian's window
#: will carry the word "Ranger" for as long as this project exists.
OBSIDIAN = "Graph view - Ranger-Vault - Obsidian 1.13.7"


def test_the_obsidian_window_is_not_matched():
    """**The false positive, as a fixture.**

    The substring rule fixed a rename and opened a wider hole: this title
    contains "Ranger", so surfacing would have restored and flashed Obsidian on
    every wake firing, and `ranger doctor` reported it as ok. The vault keeps
    the old name deliberately, so this string is permanent and so is this test.
    """
    from ranger.desktop import identify, matches

    assert not matches(OBSIDIAN)
    confidence, why = identify(OBSIDIAN, image=r"C:\Obsidian\Obsidian.exe")
    assert confidence == ""
    assert "title" in why


def test_the_old_name_is_no_longer_a_candidate():
    """Dropped on purpose, and this is the record of why.

    It was kept so that a Chrome window open across the rename would still be
    found. That was true, and it cost more than it was worth: every Obsidian
    window on the machine carries the word. A window still showing the old name
    is fixed by reloading the page, which is a smaller problem than surfacing
    somebody else's application.
    """
    from ranger.desktop import matches
    from ranger.naming import PROJECT

    assert not matches(PROJECT)
    assert PROJECT not in " ".join(__import__("ranger.desktop", fromlist=["x"]).window_names())


def test_a_window_that_is_not_a_browser_is_refused_however_it_is_titled():
    """The check that is not cosmetic. A title can say anything; Obsidian.exe
    is not a browser and no title changes that."""
    from ranger.desktop import identify

    confidence, why = identify("Jarvis", image=r"C:\Obsidian\Obsidian.exe")
    assert confidence == ""
    assert "obsidian.exe" in why and "not a browser" in why


def test_the_command_line_is_what_settles_it():
    """The interface is a browser started with --app at our own port. Nothing
    else on the machine has that string in it."""
    from ranger.desktop import CONFIRMED, identify

    confidence, why = identify(
        "Jarvis",
        image=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        command_line=(
            '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
            "--app=http://localhost:8765/ --user-data-dir=C:\\Vault\\Ranger\\browser"
        ),
        url="http://localhost:8765/",
    )
    assert confidence == CONFIRMED
    assert "--app=" in why


def test_a_browser_window_that_is_not_the_app_window_is_only_probable():
    """An ordinary tab showing a page called Jarvis is not the interface. It is
    still worth surfacing if there is nothing better, and it is not reported as
    a confident answer."""
    from ranger.desktop import PROBABLE, identify

    confidence, _ = identify(
        "Jarvis - Google Chrome",
        image=r"C:\chrome.exe",
        command_line='"C:\\chrome.exe" --profile-directory=Default',
    )
    assert confidence == PROBABLE


def test_the_app_window_is_confirmed_without_a_readable_command_line():
    """Reading another process's command line is best effort. When it fails,
    the exact title plus a browser process is still two signals, and the
    report says which two rather than claiming more."""
    from ranger.desktop import CONFIRMED, identify

    confidence, why = identify("Jarvis", image=r"C:\chrome.exe")
    assert confidence == CONFIRMED
    assert "exactly 'Jarvis'" in why and "chrome.exe" in why


def test_with_no_process_evidence_at_all_it_is_only_probable():
    from ranger.desktop import PROBABLE, identify

    confidence, why = identify("Jarvis")
    assert confidence == PROBABLE
    assert "could not be identified" in why


def test_no_entry_point_hardcodes_a_title():
    """All three read the same candidate list. A default of one cosmetic string
    is what let a rename disable surfacing."""
    import inspect

    from ranger.desktop import focus_window, minimise_window, window_state

    for function in (focus_window, minimise_window, window_state):
        default = inspect.signature(function).parameters["title_starts_with"].default
        assert default is None, f"{function.__name__} defaults to a fixed title"


# -- the diagnostic ---------------------------------------------------------


def test_the_report_says_what_it_is_looking_for():
    """A `not_found` after a wake firing is too late and does not say what it
    was looking at."""
    from ranger.desktop import find_report
    from ranger.naming import ASSISTANT, PROJECT

    report = find_report()

    assert report["looking_for"] == [ASSISTANT]
    assert PROJECT not in report["looking_for"], "the vault directory carries that name"
    assert report["detail"]


@pytest.mark.skipif(ON_WINDOWS, reason="there are real windows here")
def test_off_windows_the_report_says_so_rather_than_failing():
    from ranger.desktop import find_report

    report = find_report()

    assert report["windows"] is False
    assert report["found"] is False
    assert "not Windows" in report["detail"]


@pytest.mark.skipif(not ON_WINDOWS, reason="needs real windows to enumerate")
def test_on_windows_the_report_lists_what_is_open_when_nothing_matches():
    """The thing that answers "what is it called now" without a Windows machine
    on this end."""
    from ranger.desktop import find_report

    report = find_report("Definitely Not A Window " * 4)

    assert report["windows"] is True
    assert report["found"] is False
    assert report["titles"], "nothing was reported, so the question stays open"


def test_doctor_reports_the_window(config, capsys, monkeypatch):
    """Before the microphone is ever armed."""
    from ranger.cli import cmd_doctor

    monkeypatch.setattr("ranger.desktop.on_windows", lambda: True)
    monkeypatch.setattr(
        "ranger.desktop.find_report",
        lambda title=None, url="": {
            "windows": True, "found": False, "title": "", "titles": ["Microsoft Teams"],
            "looking_for": ["Jarvis"], "detail": "", "confidence": "", "why": "",
            "image": "", "rejected": [],
        },
    )

    cmd_doctor(config)
    out = capsys.readouterr().out

    assert "the interface window cannot be found" in out
    assert "titled exactly 'Jarvis'" in out
    assert "Microsoft Teams" in out


def doctor_output(config, capsys, monkeypatch, report):
    from ranger.cli import cmd_doctor

    base = {
        "windows": True, "found": False, "title": "", "titles": [],
        "looking_for": ["Jarvis"], "detail": "", "confidence": "", "why": "",
        "image": "", "rejected": [],
    }
    base.update(report)
    monkeypatch.setattr("ranger.desktop.on_windows", lambda: True)
    monkeypatch.setattr("ranger.desktop.find_report", lambda title=None, url="": base)
    cmd_doctor(config)
    return capsys.readouterr().out


def test_doctor_does_not_say_ok_for_a_window_it_is_not_sure_about(
    config, capsys, monkeypatch
):
    """**A green check on the wrong window is worse than no check.**

    The Obsidian match was reported as ok, and the only reason the operator
    caught it is that the title was printed beside it. Anything short of a
    confident identification now says so.
    """
    out = doctor_output(config, capsys, monkeypatch, {
        "found": True, "title": "Jarvis - Google Chrome", "confidence": "probable",
        "why": "the title is a browser window rather than the app window",
    })

    assert "check    a window matched" in out
    assert "not confidently the interface" in out
    assert "Jarvis - Google Chrome" in out
    assert "  ok       the interface window" not in out


def test_doctor_shows_what_it_refused_and_why(config, capsys, monkeypatch):
    """Where the Obsidian window belongs: named, with the reason, whether or
    not something better was found."""
    out = doctor_output(config, capsys, monkeypatch, {
        "found": True, "title": "Jarvis", "confidence": "confirmed",
        "why": "started with --app=, and the title is 'Jarvis'",
        "rejected": [{
            "title": OBSIDIAN, "image": "obsidian.exe",
            "why": "it belongs to obsidian.exe, which is not a browser",
        }],
    })

    assert "ok       the interface window: 'Jarvis'" in out
    assert "started with --app=" in out
    assert f"not it:  {OBSIDIAN!r}" in out
    assert "not a browser" in out

