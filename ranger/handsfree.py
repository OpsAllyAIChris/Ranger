"""The microphone loop behind hands free.

Python owns the microphone while hands free is armed, by the operator's
decision. The alternative, letting the browser keep it, would have meant that
"Chrome is using the microphone" could not be told apart from "Chrome is using
the microphone because Ranger asked it to" — and most of their meetings are
browser calls, so the lenient version leaves uncovered exactly the case the
check exists for.

The cost of that decision, stated where it is made: while armed, the level the
orb follows for *input* is a number reported over the socket rather than one
measured in the browser. Playback amplitude is unchanged and still measured
where the sound comes out, which is the half that was actually judged.

Nothing here talks to a model or to a transcriber. It reads frames, hands them
to `Hotword`, and reports what comes back.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any, Callable

from .wake import FRAME_SAMPLES, SAMPLE_RATE, Fire, Hotword, State

#: Bytes per 80ms frame of 16 bit mono.
FRAME_BYTES = FRAME_SAMPLES * 2


@dataclass
class Listener:
    """Runs the hotword against a real microphone on a daemon thread.

    A thread rather than the event loop because the capture call blocks, and a
    daemon because a blocked read must never be the reason ctrl-c hangs. The
    same lesson as the websocket reader.
    """

    hotword: Hotword
    #: Yields 80ms frames of 16 bit mono at 16kHz. Injected so the whole loop
    #: can be driven from a file, or from nothing at all, in a test.
    frames: Callable[[], Any]
    on_utterance: Callable[[bytes], None]
    on_fire: Callable[[Fire], None]
    on_state: Callable[[State], None] | None = None
    on_level: Callable[[float], None] | None = None
    check_seconds: float = 5.0

    _thread: threading.Thread | None = None
    _stop: threading.Event | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ranger-hotword")
        self._thread.start()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        from .wake import rms

        last_state = self.hotword.state
        try:
            for frame in self.frames():
                if self._stop is not None and self._stop.is_set():
                    break
                if not self.hotword.armed:
                    break

                utterance, fire = self.hotword.feed(frame)

                if self.on_level is not None:
                    # Scaled so ordinary speech uses most of the range, the
                    # same shape as the browser's own meter.
                    self.on_level(min(1.0, rms(frame) / 6000.0))

                if self.hotword.state is not last_state:
                    last_state = self.hotword.state
                    if self.on_state is not None:
                        self.on_state(last_state)

                if fire is not None:
                    self.on_fire(fire)
                if utterance:
                    self.on_utterance(utterance)
        except Exception as exc:  # a dead microphone must not be a dead server
            self.hotword.disarm(f"the microphone stopped: {exc}")
            if self.on_state is not None:
                self.on_state(State.OFF)
        finally:
            self._thread = None


def microphone_frames(device: Any = None, stop: threading.Event | None = None):
    """80ms frames of 16 bit mono at 16kHz, from the real microphone.

    Separated from everything else so that the one part which cannot be tested
    anywhere without a microphone is also the one part that contains no rules.
    """
    import queue

    import sounddevice

    frames: "queue.Queue[bytes]" = queue.Queue(maxsize=64)

    def callback(indata, count, timing, status):  # noqa: ARG001
        try:
            frames.put_nowait(bytes(indata))
        except queue.Full:
            # Dropping a frame is better than growing without bound behind a
            # consumer that has stalled. The hotword tolerates a gap.
            pass

    stream = sounddevice.RawInputStream(
        samplerate=SAMPLE_RATE,
        blocksize=FRAME_SAMPLES,
        dtype="int16",
        channels=1,
        device=device,
        callback=callback,
    )
    with stream:
        while stop is None or not stop.is_set():
            try:
                yield frames.get(timeout=0.5)
            except Exception:
                continue
