"""Chopping a streaming reply into speakable sentences.

The model streams and ElevenLabs streams, so the first sentence can be spoken
while the rest is still being written. That is most of the perceived latency:
waiting for a whole reply before saying any of it adds a second or more to
every turn for no reason.

Splitting has to be conservative in one direction only. Speaking half a
sentence sounds broken; waiting one clause too long merely sounds unhurried.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: A full stop that ends one of these is not the end of a sentence.
ABBREVIATIONS = frozenset(
    """
    mr mrs ms dr prof sr jr st co inc ltd llc corp dept est approx
    vs etc eg ie no vol fig al am pm
    jan feb mar apr jun jul aug sep sept oct nov dec
    """.split()
)

_ENDING = re.compile(r"([.!?])([\s\"')\]]|$)")
_TRAILING_TOKEN = re.compile(r"([A-Za-z]+)\.?$")


def _is_real_ending(text: str, index: int) -> bool:
    """Is the punctuation at index actually the end of a sentence."""
    if text[index] != ".":
        return True  # ! and ? are unambiguous enough

    head = text[:index]
    match = _TRAILING_TOKEN.search(head)
    if match:
        word = match.group(1).lower()
        if word in ABBREVIATIONS:
            return False
        # A single letter before a dot is an initial: "J. Smith".
        if len(word) == 1:
            return False
    # A digit either side is a decimal or a version number.
    if head[-1:].isdigit() and text[index + 1 : index + 2].isdigit():
        return False
    return True


@dataclass
class SentenceStream:
    """Feed it deltas, take whole sentences out.

    min_chars stops a stray "Yes." going out as its own request, which costs a
    round trip and sounds clipped. The first sentence gets a lower bar because
    it is the one the operator is waiting on.
    """

    min_chars: int = 24
    first_min_chars: int = 12
    _buffer: str = ""
    _emitted: int = 0

    def feed(self, text: str) -> list[str]:
        self._buffer += text
        return self._drain()

    def _threshold(self) -> int:
        return self.first_min_chars if self._emitted == 0 else self.min_chars

    def _drain(self) -> list[str]:
        out: list[str] = []
        while True:
            sentence = self._take_one()
            if sentence is None:
                return out
            out.append(sentence)
            self._emitted += 1

    def _take_one(self) -> str | None:
        for match in _ENDING.finditer(self._buffer):
            index = match.start(1)
            if not _is_real_ending(self._buffer, index):
                continue
            candidate = self._buffer[: index + 1].strip()
            if len(candidate) < self._threshold():
                continue
            self._buffer = self._buffer[index + 1 :].lstrip()
            return candidate

        # A paragraph break is a sentence boundary even without punctuation.
        if "\n\n" in self._buffer:
            head, _, rest = self._buffer.partition("\n\n")
            if len(head.strip()) >= self._threshold():
                self._buffer = rest.lstrip()
                return head.strip()
        return None

    def flush(self) -> str:
        """Whatever is left when the reply ends."""
        remainder = self._buffer.strip()
        self._buffer = ""
        if remainder:
            self._emitted += 1
        return remainder

    def reset(self) -> None:
        self._buffer = ""
        self._emitted = 0
