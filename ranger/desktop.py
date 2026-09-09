"""Making Jarvis something you click rather than something you type.

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
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from .naming import ASSISTANT
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
        # Its own profile keeps Jarvis's window out of the operator's work
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
#: Showing a generated document where it lives, so the operator can drag it
#: into an email. Three outcomes, and "unsupported" is one of them rather than
#: a silent no-op: this is the half of "get the file out" that only exists on a
#: desktop, and the download link is the half that always works.
REVEALED = "revealed"
REVEAL_UNSUPPORTED = "unsupported"
REVEAL_FAILED = "failed"


def reveal_file(path: Path) -> str:
    """Open the file manager with this file selected. Never opens the file.

    Deliberately not `os.startfile` and not `xdg-open` on the file itself:
    opening a generated document means launching Word, which is the operator's
    decision to make by double-clicking, not something a button in a browser
    does to them. This shows them where it is.

    `explorer /select` is documented to return a non-zero exit code on success,
    so its return code is not checked -- checking it would report a failure
    every time it worked.
    """
    target = Path(path)
    if not target.is_file():
        return REVEAL_FAILED
    try:
        if on_windows():
            subprocess.Popen(["explorer", f"/select,{target}"])
            return REVEALED
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(target)])
            return REVEALED
        opener = shutil.which("xdg-open")
        if not opener:
            return REVEAL_UNSUPPORTED
        # The folder, not the file: xdg-open on a .docx would launch whatever
        # is registered for it, which is the thing this must not do.
        subprocess.Popen([opener, str(target.parent)])
        return REVEALED
    except Exception:
        return REVEAL_FAILED


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


def focus_window(title_starts_with: str | None = None, *, topmost: bool = False) -> FocusResult:
    """Bring Jarvis's window forward, and say which of three things happened.

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
    without foreground rights, and it puts Jarvis over a screen share, so it is
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


MINIMISED = "minimised"
ALREADY = "already_minimised"


def minimise_window(title_starts_with: str | None = None) -> FocusResult:
    """Put Jarvis's window away. The other direction, and the easy one.

    `window.blur()` from the page does nothing in Chrome's app mode -- confirmed
    on the operator's machine over several attempts -- so the spoken dismissal
    goes through the same HWND surfacing already resolves.

    **`ShowWindow(SW_MINIMIZE)` is not foreground-gated.** That is the whole
    asymmetry of this feature: Jarvis can reliably put its own window away and
    cannot reliably bring it back. Nothing here touches the hotword or the
    socket -- minimising is a thing that happens to a window, and the microphone
    stays exactly as armed as it was.
    """
    if not on_windows():
        return FocusResult(NOT_FOUND, "not Windows")

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        handle = _find_window(user32, ctypes, wintypes, title_starts_with)
        if handle is None:
            return FocusResult(NOT_FOUND, "no window with that title")

        if user32.IsIconic(handle):
            return FocusResult(ALREADY, "it was already minimised")

        SW_MINIMIZE = 6
        user32.ShowWindow(handle, SW_MINIMIZE)
        # Asked again rather than assumed. The log has to say whether it
        # actually happened, not that it was attempted -- the same rule that
        # made focus_window stop reporting unconditional success.
        if user32.IsIconic(handle):
            return FocusResult(MINIMISED, "minimised")
        return FocusResult(FAILED, "ShowWindow was called and the window is still up")
    except Exception as exc:
        return FocusResult(FAILED, f"{type(exc).__name__}: {exc}")


def find_report(title_starts_with: str | None = None, url: str = "") -> dict[str, Any]:
    """What window would be surfaced, **and how sure that is**.

    `ranger doctor` prints this. It used to print `ok` and the title, and the
    title was the only reason the operator noticed it had matched Obsidian. So
    the report now carries the evidence, and a match that is not confident is
    not reported as ok. A green check on the wrong window is worse than no
    check.

    Three things come back:

    - `found` and `confidence`: what would be acted on, and whether the process
      behind it was identified as this interface.
    - `rejected`: windows that carry the name and were refused, with the reason.
      **The Obsidian window appears here**, which is where it should have been
      all along rather than in the ok line.
    - `titles`: every visible window, when nothing matched at all, because the
      useful question then is "what is it called now".
    """
    names = (title_starts_with,) if title_starts_with else window_names()
    report: dict[str, Any] = {
        "windows": False, "found": False, "title": "", "looking_for": list(names),
        "titles": [], "detail": "", "confidence": "", "why": "", "image": "",
        "rejected": [],
    }
    if not on_windows():
        report["detail"] = "not Windows, so there is no window to find"
        return report

    report["windows"] = True
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        accepted, rejected = survey_windows(user32, ctypes, wintypes, names, url)
        report["rejected"] = [
            {"title": m.title, "image": image_name(m.image), "why": m.why} for m in rejected
        ]
        if accepted:
            best = accepted[0]
            report.update(
                found=True,
                title=best.title,
                confidence=best.confidence,
                why=best.why,
                image=image_name(best.image),
                detail=f"{best.title!r}: {best.why}",
            )
            return report

        report["titles"] = [title for _, title, _ in visible_windows(user32, ctypes, wintypes)]
        report["detail"] = (
            f"no window is the interface. Looking for one titled exactly "
            f"{names[0]!r} belonging to a browser; "
            f"{len(report['titles'])} windows are open"
        )
    except Exception as exc:
        report["detail"] = f"{type(exc).__name__}: {exc}"
    return report


def window_state(title_starts_with: str | None = None) -> dict[str, Any]:
    """What Windows will say about Jarvis's window, which is less than hoped.

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


# -- which window is actually ours -----------------------------------------
#
# **A window title is cosmetic and cannot be the whole answer.** The first
# version matched `startswith(ASSISTANT)`, and a rename silently disabled
# surfacing. The fix was a substring match over two candidate names, and that
# opened a wider hole: the operator's vault is called `Ranger-Vault`, so
# Obsidian's window is titled
#
#     Graph view - Ranger-Vault - Obsidian 1.13.7
#
# which contains "Ranger". Surfacing would have restored and flashed Obsidian
# on every wake firing, and `ranger doctor` reported it as ok. **A green check
# on the wrong window is worse than no check.**
#
# So identity is now evidence about the process, and the title is one signal
# rather than the signal:
#
#   1. **The command line.** The interface is a browser started with
#      `--app=http://localhost:<port>/`. Nothing else on the machine has that.
#      Read best-effort; when it can be read it settles the question.
#   2. **The executable.** The window's process, through the documented
#      `QueryFullProcessImageNameW`. Obsidian.exe is not a browser, and that
#      one check is what rejects the window above however it is titled.
#   3. **The title**, tightened to what the page actually serves: exactly
#      "Jarvis", or "Jarvis" followed by a browser's own suffix. Not a
#      substring anywhere, and "Ranger" is gone from the candidates -- the
#      vault directory keeps that name, deliberately, so it can never be a
#      thing to match on again.
#
# Everything below the Win32 calls is pure and takes strings, so the decision
# can be tested on a machine with no windows at all.

CONFIRMED = "confirmed"
PROBABLE = "probable"
REJECTED = ""

#: The image name of a browser that can host the interface. The window's
#: process must be one of these: it is the check that rejects another
#: application whose title happens to carry the name.
BROWSER_IMAGES = frozenset({
    "chrome.exe", "msedge.exe", "chromium.exe", "brave.exe", "firefox.exe",
    "chrome", "msedge", "chromium", "chromium-browser", "google-chrome", "firefox",
})

#: What a browser appends to a page title in its ordinary windows. App mode
#: appends nothing, which is why an exact title is the strong case.
BROWSER_SUFFIXES = (
    "google chrome", "chromium", "microsoft edge", "mozilla firefox", "brave",
)

#: The separators browsers use between a page title and their own name.
SEPARATORS = (" - ", " \u2013 ", " \u2014 ", " | ")

#: How the interface is launched, and the only string on the machine that
#: belongs to it alone.
APP_MARKER = "--app="


def window_names() -> tuple[str, ...]:
    """The names a window may carry. **One name, and it is not "Ranger".**

    The old name was kept as a candidate on the reasoning that a Chrome window
    open across the rename would still say it. That was true and it was not
    worth what it cost: the vault directory is `Ranger-Vault` and is staying
    that way, so every Obsidian window on the machine carries the word. A
    window that still says the old name is fixed by reloading the page, which
    is a smaller problem than surfacing the wrong application.
    """
    from .naming import ASSISTANT

    return (ASSISTANT,)


def image_name(path: str) -> str:
    """The executable's own name, from a path in **either** convention.

    `Path(...).name` is not this: on Linux a backslash is an ordinary
    character, so `Path("C:\\chrome.exe").name` is the whole string. These are
    always Windows paths and the tests run on both platforms, so a helper that
    splits on both separators is the only version that means the same thing
    everywhere. Found by a test failing on Linux for the right reason.
    """
    return re.split(r"[\\/]", str(path or "").strip())[-1].casefold()


def title_shape(title: str, names: tuple[str, ...] = ()) -> str:
    """How the title matches, if it does at all: "exact", "suffix", or "".

    Exact is what Chrome's app mode produces: the window title is the page's
    `<title>` and nothing else. Suffix is an ordinary browser window, where the
    browser appends its own name -- a weaker signal, because it is also what a
    tab showing any page called "Jarvis" would look like.
    """
    flat = " ".join(str(title or "").split())
    lowered = flat.casefold()
    for name in (names or window_names()):
        wanted = name.casefold()
        if not wanted:
            continue
        if lowered == wanted:
            return "exact"
        for separator in SEPARATORS:
            head, found, tail = lowered.partition(separator.casefold())
            if found and head == wanted and any(
                tail.startswith(suffix) for suffix in BROWSER_SUFFIXES
            ):
                return "suffix"
    return ""


def identify(
    title: str,
    *,
    image: str = "",
    command_line: str = "",
    names: tuple[str, ...] = (),
    url: str = "",
) -> tuple[str, str]:
    """(confidence, why) for one window. Pure, and the whole decision.

    Ordered by how much each signal is worth. The command line settles it. The
    executable can only reject. The title alone is never enough to confirm and
    never enough, on its own, to act on the wrong application.
    """
    shape = title_shape(title, names)
    exe = image_name(image)
    line = str(command_line or "")

    if not shape:
        return REJECTED, "the title is not the one the interface serves"
    if exe and exe not in BROWSER_IMAGES:
        # The Obsidian case. It does not matter what the title says.
        return REJECTED, f"it belongs to {exe}, which is not a browser"

    if line:
        if APP_MARKER in line and (not url or url.casefold() in line.casefold()):
            return CONFIRMED, f"started with {APP_MARKER}, and the title is {title!r}"
        if APP_MARKER in line:
            return PROBABLE, (
                f"started with {APP_MARKER} but not at {url}; it may be a second window"
            )
        return PROBABLE, (
            f"its command line does not name this interface; matched on the title "
            f"{title!r}" + (f" and on {exe}" if exe else "")
        )

    if shape == "exact" and exe:
        return CONFIRMED, f"the title is exactly {title!r} and it belongs to {exe}"
    if shape == "exact":
        return PROBABLE, (
            f"the title is exactly {title!r}; the process could not be identified"
        )
    return PROBABLE, (
        f"the title is {title!r}, which is a browser window rather than the app window"
    )


def matches(title: str, names: tuple[str, ...] = ()) -> bool:
    """Could this title be the interface? **Title evidence only.**

    Kept because the served `<title>` and the matcher must not drift apart, and
    that test asks this question. It is deliberately not enough on its own:
    everything that acts on a window goes through `identify`.
    """
    return bool(title_shape(title, names))


def process_image(pid: int) -> str:
    """The executable behind a window, or "" when it cannot be read.

    `QueryFullProcessImageNameW` with `PROCESS_QUERY_LIMITED_INFORMATION`,
    which is documented, cheap, and granted for a process the operator owns.
    """
    if not on_windows() or not pid:
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)
            ):
                return ""
            return buffer.value
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


def process_command_line(pid: int) -> str:
    """The command line of a process, or "" when it cannot be read.

    **Best effort, and the failure is a shrug rather than an exception.** There
    is no documented Win32 call for another process's command line, so this
    reads it out of the process environment block, which means fixed structure
    offsets: `PEB.ProcessParameters` at 0x20 and
    `RTL_USER_PROCESS_PARAMETERS.CommandLine` at 0x70, both stable for 64-bit
    Windows 10 and 11. A 32-bit Python cannot read a 64-bit process this way
    and does not try.

    The result is checked for being a command line rather than trusted, so a
    wrong offset degrades to "could not read" instead of to a confident answer
    about noise. Everything above treats "" as "no evidence" and falls back to
    the executable and the title, which is why this being unavailable weakens
    the answer without breaking it.

    None of this can be exercised from a sandbox. `identify` is where the
    decision lives and it takes the string.
    """
    if not on_windows() or not pid or sys.maxsize <= 2 ** 32:
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        PROCESS_VM_READ = 0x0010

        class PROCESS_BASIC_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("Reserved1", ctypes.c_void_p),
                ("PebBaseAddress", ctypes.c_void_p),
                ("Reserved2", ctypes.c_void_p * 2),
                ("UniqueProcessId", ctypes.c_void_p),
                ("Reserved3", ctypes.c_void_p),
            ]

        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [
                ("Length", ctypes.c_ushort),
                ("MaximumLength", ctypes.c_ushort),
                ("Buffer", ctypes.c_void_p),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, int(pid)
        )
        if not handle:
            return ""
        try:
            info = PROCESS_BASIC_INFORMATION()
            written = ctypes.c_ulong(0)
            if ntdll.NtQueryInformationProcess(
                handle, 0, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(written)
            ) != 0 or not info.PebBaseAddress:
                return ""

            def read(address, size):
                buffer = ctypes.create_string_buffer(size)
                read_bytes = ctypes.c_size_t(0)
                if not kernel32.ReadProcessMemory(
                    handle, ctypes.c_void_p(address), buffer,
                    ctypes.c_size_t(size), ctypes.byref(read_bytes),
                ):
                    return None
                return buffer.raw[: read_bytes.value]

            pointer = read(info.PebBaseAddress + 0x20, 8)
            if not pointer or len(pointer) < 8:
                return ""
            parameters = int.from_bytes(pointer, "little")
            raw = read(parameters + 0x70, ctypes.sizeof(UNICODE_STRING))
            if not raw or len(raw) < ctypes.sizeof(UNICODE_STRING):
                return ""
            unicode_string = UNICODE_STRING.from_buffer_copy(raw)
            if not unicode_string.Buffer or not 0 < unicode_string.Length <= 32768:
                return ""
            text = read(unicode_string.Buffer, unicode_string.Length)
            if not text:
                return ""
            line = text.decode("utf-16-le", errors="replace").strip("\x00")
            # Checked, not trusted. A command line has a program in it.
            if len(line) < 3 or "\x00" in line or line.count("\ufffd") > 4:
                return ""
            return line
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


def visible_windows(user32, ctypes, wintypes) -> list[tuple[int, str, int]]:
    """Every visible top level window with a title, and the process behind it.

    `(handle, title, pid)`. The pid is what turned identity from a string
    comparison into a question about a process.

    A minimised window is still `IsWindowVisible`: that flag is about WS_VISIBLE
    rather than about being on screen, which is the same distinction that makes
    the occlusion question hard.
    """
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found: list[tuple[int, str, int]] = []

    def visit(handle, _param):
        if not user32.IsWindowVisible(handle):
            return True
        length = user32.GetWindowTextLengthW(handle)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        pid = wintypes.DWORD(0)
        try:
            user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        except Exception:
            pid = wintypes.DWORD(0)
        found.append((handle, buffer.value, int(pid.value)))
        return True

    user32.EnumWindows(enum_proc(visit), 0)
    return found


@dataclass(frozen=True)
class WindowMatch:
    """One candidate, with the evidence that made it one."""

    handle: int
    title: str
    pid: int
    image: str
    confidence: str
    why: str

    @property
    def confirmed(self) -> bool:
        return self.confidence == CONFIRMED

    def describe(self) -> str:
        return f"{self.title!r} ({image_name(self.image) or 'unknown process'}): {self.why}"


def survey_windows(
    user32, ctypes, wintypes, names: tuple[str, ...] = (), url: str = ""
) -> tuple[list[WindowMatch], list[WindowMatch]]:
    """(what could be the interface, what nearly was).

    The second list is the point of this function. The Obsidian window was a
    near miss under the old rule and a silent one; anything carrying the name
    and rejected is worth showing the operator, because that is where the next
    wrong match will come from.
    """
    names = names or window_names()
    accepted: list[WindowMatch] = []
    rejected: list[WindowMatch] = []
    for handle, title, pid in visible_windows(user32, ctypes, wintypes):
        shape = title_shape(title, names)
        carries = any(name.casefold() in title.casefold() for name in names)
        if not shape and not carries:
            continue
        image = process_image(pid)
        confidence, why = identify(
            title,
            image=image,
            command_line=process_command_line(pid) if shape else "",
            names=names,
            url=url,
        )
        match = WindowMatch(handle, title, pid, image, confidence, why)
        (accepted if confidence else rejected).append(match)
    accepted.sort(key=lambda m: 0 if m.confirmed else 1)
    return accepted, rejected


def _find_window(user32, ctypes, wintypes, title_starts_with: str | None = None):
    """The interface's window, or None. **Evidence, not a title comparison.**

    A confirmed match wins; a probable one is used when there is no confirmed
    one, because a browser window that is genuinely the interface is still the
    interface. A rejected window is never touched, whatever it is called --
    which is the whole of the Obsidian fix.

    `title_starts_with` is an override for the tests that ask for a title
    nothing can have. When it is None the real candidate list is used.
    """
    names = (title_starts_with,) if title_starts_with else window_names()
    accepted, _ = survey_windows(user32, ctypes, wintypes, names)
    return accepted[0].handle if accepted else None


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
    description: str = "Jarvis"

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
