"""When to start and stop recording.

Push to talk only, no wake word. A terminal can see a key go down with the
standard library but not come back up, so true hold-to-talk needs a global key
hook. pynput provides one; the stdlib toggle is the fallback for when the hook
fights Windows focus or a security tool, and it is a config edit away rather
than a wait.

Both shapes are the same to the caller: block until the operator starts, hand
back an Event that is set when they stop.
"""

from __future__ import annotations

import sys
import threading
from typing import Any, Callable, Protocol, runtime_checkable


class TriggerError(Exception):
    """The trigger could not be set up, said in a sentence."""


@runtime_checkable
class Trigger(Protocol):
    #: What to print so the operator knows how to start.
    hint: str

    def wait_to_start(self) -> bool:
        """Block until recording should start. False means the operator quit."""

    def stop_event(self) -> threading.Event:
        """Set the moment recording should stop."""

    def close(self) -> None: ...


class HoldTrigger:
    """Hold the key to talk, release to send. The default."""

    def __init__(self, key: str = "space") -> None:
        self.key_name = key
        self.hint = f"HOLD {key.upper()} TO TALK"
        self._pressed = threading.Event()
        self._released = threading.Event()
        self._quit = threading.Event()
        self._listener: Any = None
        self._key = None

    def _start_listener(self) -> None:
        if self._listener is not None:
            return
        try:
            from pynput import keyboard
        except ImportError as exc:
            raise TriggerError(
                "pynput is not installed, so hold-to-talk cannot listen for the key. "
                "Run: pip install pynput. Or set voice.trigger = \"toggle\" in ranger.toml "
                "to use the standard-library fallback."
            ) from exc

        self._key = _to_key(keyboard, self.key_name)

        def on_press(key: Any) -> None:
            if _same_key(key, self._key) and not self._pressed.is_set():
                self._released.clear()
                self._pressed.set()
            elif key == keyboard.Key.esc:
                self._quit.set()
                self._pressed.set()

        def on_release(key: Any) -> None:
            if _same_key(key, self._key):
                self._released.set()

        try:
            self._listener = keyboard.Listener(on_press=on_press, on_release=on_release)
            self._listener.start()
        except Exception as exc:
            raise TriggerError(
                f"could not start the keyboard listener ({exc}). Set voice.trigger = "
                '"toggle" in ranger.toml to use the standard-library fallback.'
            ) from exc

    def wait_to_start(self) -> bool:
        self._start_listener()
        self._pressed.clear()
        self._released.clear()
        self._pressed.wait()
        return not self._quit.is_set()

    def stop_event(self) -> threading.Event:
        return self._released

    def close(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None


class ToggleTrigger:
    """Enter to start, Enter again to stop. Standard library only, cannot fail."""

    hint = "PRESS ENTER TO TALK, ENTER AGAIN TO SEND"

    def __init__(self, readline: Callable[[], str] | None = None) -> None:
        self._readline = readline or (lambda: sys.stdin.readline())
        self._stop = threading.Event()

    def wait_to_start(self) -> bool:
        line = self._readline()
        if line == "":  # EOF
            return False
        if line.strip().lower() in {"q", "quit", "exit"}:
            return False
        self._stop = threading.Event()
        return True

    def stop_event(self) -> threading.Event:
        stop = self._stop

        def wait_for_enter() -> None:
            self._readline()
            stop.set()

        threading.Thread(target=wait_for_enter, daemon=True).start()
        return stop

    def close(self) -> None:
        self._stop.set()


class FixedTrigger:
    """Record for a set number of seconds. No key hook at all.

    This is what makes 'ranger audio check' work before pynput is proven: a
    failure here is the microphone, never the keyboard.
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.hint = f"recording for {seconds:g} seconds"
        self._armed = False
        self._timer: threading.Timer | None = None

    def wait_to_start(self) -> bool:
        if self._armed:
            return False
        self._armed = True
        return True

    def stop_event(self) -> threading.Event:
        stop = threading.Event()
        self._timer = threading.Timer(self.seconds, stop.set)
        self._timer.daemon = True
        self._timer.start()
        return stop

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


def _to_key(keyboard: Any, name: str) -> Any:
    named = getattr(keyboard.Key, name.strip().lower(), None)
    if named is not None:
        return named
    cleaned = name.strip()
    if len(cleaned) == 1:
        return keyboard.KeyCode.from_char(cleaned.lower())
    raise TriggerError(
        f"voice.key = {name!r} is not a key pynput knows. Use a single character, or one of "
        "space, ctrl, alt, shift, cmd, tab, f1 to f12."
    )


def _same_key(pressed: Any, wanted: Any) -> bool:
    if pressed == wanted:
        return True
    char = getattr(pressed, "char", None)
    wanted_char = getattr(wanted, "char", None)
    return char is not None and wanted_char is not None and char.lower() == wanted_char.lower()


def build_trigger(kind: str, key: str, seconds: float | None = None) -> Trigger:
    if seconds is not None:
        return FixedTrigger(seconds)
    kind = (kind or "hold").strip().lower()
    if kind == "hold":
        return HoldTrigger(key)
    if kind == "toggle":
        return ToggleTrigger()
    raise TriggerError(f'voice.trigger must be "hold" or "toggle", got {kind!r}')
