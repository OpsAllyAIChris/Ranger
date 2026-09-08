"""Making Ranger something you click rather than something you type.

Everything Windows-specific about the taskbar shortcut lives here: creating the
`.lnk`, opening the interface in its own window, and bringing that window
forward when it already exists. Each is best effort and each falls back to
something that works, because none of it is load bearing: the worst outcome of
every failure in this file is that the operator gets an ordinary browser tab.

Nothing here is verifiable from a sandbox. It is separated for that reason as
much as any other, so the parts that can be tested are not tangled up with the
parts that cannot.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any
from pathlib import Path

#: Chrome and Edge both take --app, which opens a window with no address bar,
#: no tab strip and its own taskbar button. That last part is what makes a
#: pinned shortcut feel like an application rather than a bookmark.
APP_FLAG = "--app="

WINDOWS_BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def on_windows() -> bool:
    return os.name == "nt"


def find_browser() -> Path | None:
    """A Chromium that understands --app, or None to fall back to webbrowser."""
    for name in ("chrome", "msedge", "chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(name)
        if found:
            return Path(found)
    for candidate in WINDOWS_BROWSERS:
        path = Path(candidate)
        if path.exists():
            return path
    return None


def open_window(url: str, *, profile_dir: Path | None = None) -> str:
    """Open the interface. Returns how it was opened, for the operator to read."""
    browser = find_browser()
    if browser is None:
        import webbrowser

        webbrowser.open(url)
        return "in your default browser, as an ordinary tab"

    arguments = [str(browser), f"{APP_FLAG}{url}"]
    if profile_dir is not None:
        # Its own profile keeps Ranger's window out of the operator's work
        # browser: no shared session, no restored tabs, and closing their
        # browser does not close this.
        arguments.append(f"--user-data-dir={profile_dir}")
    try:
        _spawn(arguments)
    except OSError:
        import webbrowser

        webbrowser.open(url)
        return "in your default browser, as an ordinary tab"
    return f"in its own window, through {browser.name}"


def _spawn(arguments: list[str]) -> subprocess.Popen:
    """Start something and stop caring about it. No console, no waiting."""
    flags = 0
    if on_windows():
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    return subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )


def start_server(config_path: Path, log_path: Path) -> subprocess.Popen:
    """Start `ranger ui` as a detached process with no console window.

    pythonw rather than ranger.exe: this one has to be windowless, because the
    whole point is that clicking an icon does not open a terminal. The log is
    what replaces the terminal, and it is named in the reply every time.
    """
    launcher = Path(sys.executable)
    windowless = launcher.with_name("pythonw.exe")
    if windowless.exists():
        launcher = windowless

    return _spawn(
        [
            str(launcher),
            "-m",
            "ranger",
            "-c",
            str(config_path),
            "ui",
            "--log",
            str(log_path),
        ]
    )


# -- bringing an existing window forward ------------------------------------


#: What actually happened, in the order they are attempted. The whole point of
#: this enum is that the code knows which one it got: the previous version
#: called SetForegroundWindow, ignored its return value, and reported success
#: unconditionally, so nobody could say what Windows was really doing.
NOT_FOUND = "not_found"
RESTORED = "restored"      # SW_RESTORE only. Un-minimised, not necessarily in front
FOREGROUND = "foreground"  # SetForegroundWindow was granted. The good case
FLASHED = "flashed"        # foreground refused, taskbar button flashing instead
TOPMOST = "topmost"        # forced in front, off by default and for a reason
FAILED = "failed"


@dataclass(frozen=True)
class FocusResult:
    """Which of the three outcomes happened, and what to write in the log.

    `focused` is kept for callers that only want a yes or no, and it now means
    what it says: the window is actually in front. A flash is not focus.
    """

    outcome: str
    detail: str

    @property
    def focused(self) -> bool:
        return self.outcome in (FOREGROUND, TOPMOST)

    @property
    def surfaced(self) -> bool:
        """Did the operator get *something*? A flash counts; nothing does not."""
        return self.outcome in (RESTORED, FOREGROUND, FLASHED, TOPMOST)

    def describe(self) -> str:
        return f"{self.outcome}: {self.detail}"


def focus_window(title_starts_with: str = "Ranger", *, topmost: bool = False) -> FocusResult:
    """Bring Ranger's window forward, and say which of three things happened.

    **Restoring and foregrounding are two different permissions**, which the
    first version of this conflated. `ShowWindow(SW_RESTORE)` is not gated by
    the foreground lock and is expected to work. `SetForegroundWindow` is
    expected to be *refused* much of the time: Windows grants it only to a
    process that already owns the foreground, was the foreground, or received
    the last input event. A wake word is none of those -- speech is not an input
    event -- so a firing has less claim than the double-click that used to
    trigger this did.

    When foreground is refused the fallback is `FlashWindowEx`, which is the
    sanctioned way to say "look at me". That is a real, lesser outcome and it is
    reported as `flashed`, never as success. The operator asked for the refusal
    path to matter more than the happy path, and this is why: on their machine
    it is probably the common one, and until it is logged honestly nobody knows.

    `topmost` forces the window in front by toggling `HWND_TOPMOST`. It works
    without foreground rights, and it puts Ranger over a screen share, so it is
    config-only and off by default.
    """
    if not on_windows():
        return FocusResult(NOT_FOUND, "not Windows")

    try:
        import ctypes
        from ctypes import wintypes
    except Exception as exc:  # pragma: no cover - ctypes is always there
        return FocusResult(FAILED, f"no ctypes: {exc}")

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        handle = _find_window(user32, ctypes, wintypes, title_starts_with)
        if handle is None:
            return FocusResult(NOT_FOUND, "no window with that title")
        return _surface(user32, ctypes, wintypes, handle, topmost=topmost)
    except Exception as exc:
        return FocusResult(FAILED, f"{type(exc).__name__}: {exc}")


#: `DWMWA_CLOAKED`. Set when a window is hidden by the compositor -- on another
#: virtual desktop, or a suspended app. Not the same as occluded, and that
#: distinction is the whole of the honest answer below.
DWMWA_CLOAKED = 14


def window_state(title_starts_with: str = "Ranger") -> dict[str, Any]:
    """What Windows will say about Ranger's window, which is less than hoped.

    **There is still no reliable occlusion signal, and this says so rather than
    inventing one.** The comment this replaces said "no page-level occlusion
    API exists" and pointed here, on the reasoning that having an HWND would
    let us ask Windows directly. Having the HWND helps, and it does not close
    the question:

    - `IsIconic` is reliable. Minimised is known exactly, which the browser
      already knew.
    - `IsWindowVisible` is about the WS_VISIBLE style, not about being on
      screen. A window entirely behind Teams is `visible`.
    - `GetForegroundWindow` is reliable, and answers a *different* question:
      being foreground means being on top, but not being foreground does not
      mean being hidden. A window beside the foreground one is perfectly
      readable, and treating that as hidden would refuse to open a conversation
      window most of the time it should.
    - `DwmGetWindowAttribute(DWMWA_CLOAKED)` catches another virtual desktop and
      a suspended app. Not ordinary occlusion.
    - The compositor *does* compute occlusion, and Chrome consumes it
      internally through its own window tracking. It is not exposed by any
      documented Win32 call. Working it out from `EnumWindows` z-order plus
      per-window rectangles is possible and would be a guess with a lot of edge
      cases: transparent windows, multiple monitors, partial overlap.

    So the honest answer is: minimised, cloaked and foreground are known;
    **occluded is not**, and the guard is exactly as strong as before plus
    minimised-known-from-the-OS rather than from the page. The comment at
    `Window.sees` is resolved to say that, rather than left implying a fix that
    never came.
    """
    unknown = {
        "found": False, "minimised": None, "foreground": None,
        "cloaked": None, "occluded": None,
    }
    if not on_windows():
        return unknown

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        handle = _find_window(user32, ctypes, wintypes, title_starts_with)
        if handle is None:
            return unknown

        cloaked = None
        try:
            dwmapi = ctypes.WinDLL("dwmapi")
            value = ctypes.c_int(0)
            if dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(handle), ctypes.c_uint(DWMWA_CLOAKED),
                ctypes.byref(value), ctypes.sizeof(value),
            ) == 0:
                cloaked = bool(value.value)
        except Exception:
            cloaked = None

        return {
            "found": True,
            "minimised": bool(user32.IsIconic(handle)),
            "foreground": user32.GetForegroundWindow() == handle,
            "cloaked": cloaked,
            # Deliberately None, always. Not "False", which would read as
            # "checked and it is not occluded".
            "occluded": None,
        }
    except Exception:
        return unknown


def _find_window(user32, ctypes, wintypes, title_starts_with: str):
    """The first visible top level window whose title starts with the name.

    A minimised window is still `IsWindowVisible`: that flag is about WS_VISIBLE
    rather than about being on screen, which is the same distinction that makes
    the occlusion question hard.
    """
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found: list[int] = []

    def visit(handle, _param):
        if not user32.IsWindowVisible(handle):
            return True
        length = user32.GetWindowTextLengthW(handle)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        if buffer.value.startswith(title_starts_with):
            found.append(handle)
            return False
        return True

    user32.EnumWindows(enum_proc(visit), 0)
    return found[0] if found else None


def _surface(user32, ctypes, wintypes, handle, *, topmost: bool = False) -> FocusResult:
    """Restore, try foreground, flash if refused. In that order."""
    SW_RESTORE = 9
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
    FLASHW_ALL, FLASHW_TIMERNOFG = 0x00000003, 0x0000000C

    was_minimised = bool(user32.IsIconic(handle))
    if was_minimised:
        user32.ShowWindow(handle, SW_RESTORE)

    if topmost:
        # Set topmost then immediately not-topmost: the window comes to the
        # front and does not stay pinned there. No foreground rights needed,
        # and no keyboard focus stolen either.
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
        user32.SetWindowPos(handle, HWND_TOPMOST, 0, 0, 0, 0, flags)
        user32.SetWindowPos(handle, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
        return FocusResult(TOPMOST, "forced in front with HWND_TOPMOST")

    # The return value is the whole point. Ignoring it is what made the old
    # version claim success it had no evidence for.
    if user32.SetForegroundWindow(handle):
        detail = "restored and brought to the front" if was_minimised else "brought to the front"
        return FocusResult(FOREGROUND, detail)

    class FLASHWINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("hwnd", wintypes.HWND),
            ("dwFlags", wintypes.DWORD),
            ("uCount", wintypes.UINT),
            ("dwTimeout", wintypes.DWORD),
        ]

    info = FLASHWINFO(
        ctypes.sizeof(FLASHWINFO), handle, FLASHW_ALL | FLASHW_TIMERNOFG, 0, 0
    )
    flashed = bool(user32.FlashWindowEx(ctypes.byref(info)))
    if flashed:
        detail = (
            "restored, foreground refused by Windows, taskbar button flashing"
            if was_minimised
            else "foreground refused by Windows, taskbar button flashing"
        )
        return FocusResult(FLASHED, detail)
    if was_minimised:
        return FocusResult(RESTORED, "restored, but foreground and flash both refused")
    return FocusResult(FAILED, "foreground refused and the taskbar would not flash")


# -- the shortcut itself ----------------------------------------------------

#: Built by PowerShell's COM object rather than a library. A .lnk is a
#: structured binary format and hand-writing one is a bad trade for something
#: every Windows install can already do.
SHORTCUT_SCRIPT = """\
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut('{path}')
$link.TargetPath = '{target}'
$link.Arguments = '{arguments}'
$link.WorkingDirectory = '{working}'
$link.IconLocation = '{icon}'
$link.Description = '{description}'
$link.WindowStyle = 7
$link.Save()
"""


@dataclass(frozen=True)
class Shortcut:
    path: Path
    target: Path
    arguments: str
    working_directory: Path
    icon: Path
    description: str = "Ranger"

    def script(self) -> str:
        return SHORTCUT_SCRIPT.format(
            path=_ps_quote(self.path),
            target=_ps_quote(self.target),
            arguments=self.arguments.replace("'", "''"),
            working=_ps_quote(self.working_directory),
            icon=_ps_quote(self.icon),
            description=self.description.replace("'", "''"),
        )


def _ps_quote(value: Path | str) -> str:
    """Inside a single-quoted PowerShell string, a quote doubles. Nothing else
    is special, which is why single quotes are used throughout."""
    return str(value).replace("'", "''")


def create_shortcut(shortcut: Shortcut) -> None:
    if not on_windows():
        raise OSError("shortcuts are Windows only")

    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        raise OSError("PowerShell is not on PATH, so the shortcut cannot be created")

    shortcut.path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", shortcut.script()],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise OSError((result.stderr or result.stdout).strip() or "PowerShell refused it")
