"""What the operator themselves said, and the figures in it.

**The accountant rule needs evidence, not intention.** Jarvis may enter a gross
profit figure the operator states, and may never derive one -- not from a
spreadsheet, not from a column, not inferred from other months. A tool
description asking the model not to derive a number is a request. This is the
check: the figure has to appear in the operator's own words, and Python decides
whether it does.

The words come through a `ContextVar` set by `core.turn`, for the same reason
the headless depth guard uses one: asyncio copies the current context into a
task when the task is created, so anything spawned during a turn carries it. A
module-level variable would be shared between concurrent turns, and a parameter
threaded through the registry would have to be threaded through every future
caller as well.

What is stored is the operator's side of the conversation, not just the latest
line. "Add a GP entry" and "292,187" are routinely two turns, and a figure the
operator said a minute ago is still a figure the operator said.

**This is not the safety.** The gate is: the card shows the figure and the
period and the operator approves it at the keyboard. This stops a figure nobody
said from reaching the card at all.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation

#: The operator's own turns, most recent last.
_said: ContextVar[tuple[str, ...]] = ContextVar("ranger_operator_words", default=())


def remember(words: "list[str] | tuple[str, ...]"):
    """Set the operator's words for this turn. Returns the reset token."""
    return _said.set(tuple(str(word) for word in words if str(word).strip()))


def forget(token) -> None:
    _said.reset(token)


def said() -> tuple[str, ...]:
    return _said.get()


# -- figures ----------------------------------------------------------------
#
# Compared as numbers, never as strings. "$292,187.00" and "292187" are the
# same figure, and a string comparison would refuse the entry and leave the
# operator repeating themselves at a machine that heard them correctly.

_NUMERIC = re.compile(r"(?<![\d.])\d[\d,]*(?:\.\d+)?(?![\d])")

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALES = {"hundred": 100, "thousand": 1_000, "million": 1_000_000,
           "billion": 1_000_000_000}


def _spoken(text: str) -> set[Decimal]:
    """Numbers written as words. Transcription produces them often enough.

    Whisper writes "292,187" most of the time and "two hundred and ninety two
    thousand one hundred and eighty seven" some of the time, and which one it
    picks is not something the operator can control or should have to think
    about.
    """
    found: set[Decimal] = set()
    total = current = Decimal(0)
    running = False

    def flush() -> None:
        nonlocal total, current, running
        if running:
            found.add(total + current)
        total = current = Decimal(0)
        running = False

    # Digits count as words here. "292 thousand" is half spoken and half
    # written, and it is what transcription produces most often of all --
    # reading it as the two separate numbers 292 and 1000 is how a figure
    # nobody said appears in a list of figures the operator said.
    for token in re.findall(r"\d[\d,]*(?:\.\d+)?|[a-z]+",
                            text.lower().replace("-", " ")):
        if token[0].isdigit():
            try:
                value = Decimal(token.replace(",", ""))
            except InvalidOperation:
                flush()
                continue
            if running and current:
                # Two numbers running together are two numbers, not a sum.
                flush()
            current += value
            running = True
        elif token in _UNITS:
            current += _UNITS[token]
            running = True
        elif token in _TENS:
            current += _TENS[token]
            running = True
        elif token == "hundred":
            current = (current or Decimal(1)) * 100
            running = True
        elif token in _SCALES:
            total += (current or Decimal(1)) * _SCALES[token]
            current = Decimal(0)
            running = True
        elif token == "and" and running:
            continue
        else:
            flush()
    flush()
    found.discard(Decimal(0))
    return found


def figures_in(text: str) -> set[Decimal]:
    """Every number in this text, as numbers.

    Both spellings, because a figure is a figure however it was transcribed.
    """
    found: set[Decimal] = set()
    for token in _NUMERIC.findall(text or ""):
        try:
            found.add(Decimal(token.replace(",", "")))
        except InvalidOperation:
            continue
    found |= _spoken(text or "")
    return found


def stated(amount: Decimal, words: "tuple[str, ...] | None" = None) -> bool:
    """Did the operator say this figure?

    Numeric equality against everything they said this conversation. A rounded
    version does not count: "about 292 thousand" is 292000, which is not
    292,187, and entering the first when they said the second is exactly the
    invention this is here to stop.
    """
    spoken = words if words is not None else said()
    for line in spoken:
        if any(figure == amount for figure in figures_in(line)):
            return True
    return False
