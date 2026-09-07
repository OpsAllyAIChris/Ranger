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


@dataclass(frozen=True)
class FocusResult:
    focused: bool
    detail: str


def focus_window(title_starts_with: str = "Ranger") -> FocusResult:
    """Bring Ranger's window to the front, if it can be found.

    Best effort by construction. Windows will refuse SetForegroundWindow to a
    process that does not own the foreground, in which case the window flashes
    in the taskbar instead, which is the usual behaviour for an app being
    reopened and is good enough. Any failure returns rather than raises: the
    caller opens a new window instead, and a second window is a much smaller
    problem than a shortcut that errors.
    """
    if not on_windows():
        return FocusResult(False, "not Windows")

    try:
        import ctypes
        from ctypes import wintypes
    except Exception as exc:  # pragma: no cover - ctypes is always there
        return FocusResult(False, f"no ctypes: {exc}")

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        enum_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )
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
        if not found:
            return FocusResult(False, "no window with that title")

        handle = found[0]
        SW_RESTORE = 9
        if user32.IsIconic(handle):
            user32.ShowWindow(handle, SW_RESTORE)
        user32.SetForegroundWindow(handle)
        return FocusResult(True, "brought the existing window forward")
    except Exception as exc:
        return FocusResult(False, f"{type(exc).__name__}: {exc}")


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
