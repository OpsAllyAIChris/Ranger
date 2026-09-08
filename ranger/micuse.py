"""Who else is using the microphone.

The mitigation for the case the operator actually cares about: a wake word
firing during a customer call and transcribing the customer. Hands free never
arms while another application has the microphone open, and disarms if one
takes it while armed.

Windows records this per application under

    HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager
        \\ConsentStore\\microphone

with a `NonPackaged` subkey for ordinary desktop programs, whose paths are
mangled with `#` in place of the separators. Each application has
`LastUsedTimeStart` and `LastUsedTimeStop`; **a stop time of zero means it is
using the microphone right now.** This is the same source the Windows 11
taskbar indicator reads, so what Ranger believes and what the operator can see
in their own taskbar cannot disagree.

The rule is strict, by the operator's decision: **any other consumer wins.**
Most of their meetings are browser calls, and a browser holding the microphone
is indistinguishable from a browser holding it for a Teams tab. Lenient would
leave uncovered exactly the case the check exists for.

Reading is injected so the whole thing is testable somewhere that has no
registry at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Iterable

CONSENT_STORE = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager"
    r"\ConsentStore\microphone"
)

#: Ranger's own capture, which obviously does not count as somebody else.
OURS = ("python.exe", "pythonw.exe", "ranger.exe")


@dataclass(frozen=True)
class Consumer:
    """One application, and whether it has the microphone open right now."""

    name: str
    packaged: bool
    in_use: bool

    @property
    def is_ours(self) -> bool:
        return self.name.lower() in OURS


def unmangle(key: str) -> str:
    """`C:#Program Files#Google#Chrome#chrome.exe` back to a program name.

    Only the executable name is kept. The full path is the operator's software
    inventory and it has no business being in a log or on a screen.
    """
    return key.replace("#", "\\").rsplit("\\", 1)[-1]


def on_windows() -> bool:
    return os.name == "nt"


def _read_registry() -> Iterable[tuple[str, bool, int]]:
    """(name, packaged, stop_time) for every application in the consent store."""
    import winreg

    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            store = winreg.OpenKey(root, CONSENT_STORE)
        except OSError:
            continue
        with store:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(store, index)
                except OSError:
                    break
                index += 1
                if name.lower() == "nonpackaged":
                    yield from _read_group(store, name, packaged=False)
                else:
                    stop = _stop_time(store, name)
                    if stop is not None:
                        yield (name, True, stop)


def _read_group(store, group: str, *, packaged: bool):
    import winreg

    try:
        parent = winreg.OpenKey(store, group)
    except OSError:
        return
    with parent:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(parent, index)
            except OSError:
                return
            index += 1
            stop = _stop_time(parent, name)
            if stop is not None:
                yield (unmangle(name), packaged, stop)


def _stop_time(parent, name: str) -> int | None:
    import winreg

    try:
        with winreg.OpenKey(parent, name) as key:
            value, _ = winreg.QueryValueEx(key, "LastUsedTimeStop")
            return int(value)
    except OSError:
        return None


def consumers(read: Callable[[], Iterable[tuple[str, bool, int]]] | None = None) -> list[Consumer]:
    """Every application the consent store knows about, and its current state."""
    reader = read or (_read_registry if on_windows() else lambda: ())
    found: list[Consumer] = []
    for name, packaged, stop in reader():
        found.append(Consumer(name=name, packaged=packaged, in_use=stop == 0))
    return found


def others_using(
    read: Callable[[], Iterable[tuple[str, bool, int]]] | None = None,
    *,
    ignore: tuple[str, ...] = (),
) -> list[str]:
    """Names of applications holding the microphone, excluding Ranger's own.

    Sorted and deduplicated, because this ends up in a banner and in the audit
    log and both should read the same way twice.
    """
    skip = {name.lower() for name in (*OURS, *ignore)}
    return sorted(
        {c.name for c in consumers(read) if c.in_use and c.name.lower() not in skip}
    )


@dataclass(frozen=True)
class Verdict:
    """Whether hands free may be armed, and what to say if not."""

    allowed: bool
    blockers: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        if self.allowed:
            return "nothing else is using the microphone"
        return "the microphone is in use by " + ", ".join(self.blockers)


def may_arm(
    read: Callable[[], Iterable[tuple[str, bool, int]]] | None = None,
    *,
    ignore: tuple[str, ...] = (),
    require_windows: bool = True,
) -> Verdict:
    """The check, in the form the caller wants it.

    On anything that is not Windows there is no consent store to read, so this
    reports allowed and says so. That is honest rather than convenient: the
    operator's machine is Windows, and pretending to have checked on a platform
    where nothing was checked would be worse than saying nothing was.
    """
    if require_windows and not on_windows() and read is None:
        return Verdict(True)
    blockers = others_using(read, ignore=ignore)
    return Verdict(not blockers, tuple(blockers))
