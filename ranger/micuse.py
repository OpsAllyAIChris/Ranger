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


class Unreadable(Exception):
    """The consent store could not be read, so nothing is known.

    Deliberately not the same as "nothing is using the microphone". This check
    is the whole mitigation for a wake word firing during a customer call, and
    a check that cannot run has found nothing rather than found nobody.
    """


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
    """(name, packaged, stop_time) for every application in the consent store.

    Raises `Unreadable` if neither hive could be opened. An empty store is a
    real answer; a store that will not open is not.
    """
    try:
        import winreg
    except ImportError as exc:  # not Windows, or a stripped build
        raise Unreadable(f"winreg is not available: {exc}") from exc

    opened = 0
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            store = winreg.OpenKey(root, CONSENT_STORE)
        except OSError:
            continue
        opened += 1
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

    if not opened:
        raise Unreadable(
            f"neither HKCU nor HKLM has {CONSENT_STORE}, so which applications "
            "are using the microphone cannot be known"
        )



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
    """Every application the consent store knows about, and its current state.

    Raises `Unreadable` rather than returning an empty list when there is no
    way to find out. On anything that is not Windows there is no store at all,
    which is also not knowing.
    """
    if read is None and not on_windows():
        raise Unreadable(
            "the microphone check reads a Windows registry key, and this is not Windows"
        )
    reader = read or _read_registry
    found: list[Consumer] = []
    for name, packaged, stop in reader():
        found.append(Consumer(name=name, packaged=packaged, in_use=stop == 0))

    if not found:
        # Zero applications is not "nobody is using the microphone". On a real
        # machine the consent store always has entries, so an empty result is
        # far likelier to mean the enumeration is looking in the wrong place,
        # and the symptom of that would be a check that silently always says
        # yes. Enforced here rather than in the registry reader so it holds for
        # every reader, including the ones in tests.
        raise Unreadable(
            "the consent store listed no applications at all, which means the "
            "check is not reading what it thinks it is"
        )
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
    #: The check could not run. Refused for a different reason from "somebody
    #: else has the microphone", and worth saying differently.
    unreadable: str = ""

    @property
    def reason(self) -> str:
        if self.unreadable:
            return (
                f"the microphone check could not run ({self.unreadable}), and hands free "
                "does not arm without it"
            )
        if self.allowed:
            return "nothing else is using the microphone"
        return "the microphone is in use by " + ", ".join(self.blockers)


def describe(read: Callable[[], Iterable[tuple[str, bool, int]]] | None = None) -> list[str]:
    """What the consent store actually says, for the operator to check.

    The whole check is unverifiable from anywhere without a Windows registry,
    so this exists to make it verifiable in one command on the machine that
    has one: open Teams, run `ranger mic`, and see whether Ranger sees what the
    taskbar sees. A check nobody can confirm is a check nobody should trust.
    """
    try:
        found = consumers(read)
    except Unreadable as exc:
        return [f"the microphone check could not run: {exc}"]

    if not found:
        return ["the consent store listed no applications at all"]

    lines = [f"{len(found)} applications have asked for the microphone at some point"]
    live = [c for c in found if c.in_use]
    if live:
        lines.append("")
        lines.append("using it right now:")
        lines += [
            f"  {c.name}{'   (Ranger itself)' if c.is_ours else ''}"
            for c in sorted(live, key=lambda c: c.name.lower())
        ]
    else:
        lines.append("none of them is using it right now")
    return lines


def may_arm(
    read: Callable[[], Iterable[tuple[str, bool, int]]] | None = None,
    *,
    ignore: tuple[str, ...] = (),
) -> Verdict:
    """The check, and it fails closed.

    A check that cannot run has found nothing, not found nobody. This is the
    entire mitigation for a wake word firing during a customer call and
    transcribing the customer, so being unable to perform it refuses rather
    than allows. The operator's words: they would rather it never work than
    work while they are on a call.

    That means hands free does not arm on anything that is not Windows, since
    there is no consent store to read. Correct rather than convenient.
    """
    try:
        blockers = others_using(read, ignore=ignore)
    except Unreadable as exc:
        return Verdict(False, (), unreadable=str(exc))
    return Verdict(not blockers, tuple(blockers))
