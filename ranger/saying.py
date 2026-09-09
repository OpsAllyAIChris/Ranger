"""Numbers as words, for the ear only.

**"$292,187" is not text, it is a guess.** Sent to a speech model as digits it
has to decide whether that is a price, a part number or a year, and on a long
figure it slurs the middle. Sent as "two hundred ninety-two thousand one
hundred eighty-seven dollars" there is nothing left to guess at.

## Where this runs, and where it must never run

`ElevenLabsSpeaker.stream` and nowhere else. That is the one place in the
project where text becomes audio, so a transform applied there cannot reach
anything that is kept:

- The window shows the model's own words. Untouched.
- A filed note, a draft, a document, a GP entry: all written from the model's
  words or from Python's figures. Untouched.
- The figure audit compares the reply's digits against what Python computed. It
  runs on the written reply, and would find nothing to compare if it ran on
  this.

Digits on the screen, words in the ear. A test asserts that nothing outside
`tts.py` imports this module, because the failure -- spelled-out numbers in a
filed note -- is the exact opposite of what the figure audit exists for.

## What it will not touch

Words. `3,000 MOQ` becomes `three thousand MOQ`, because MOQ is not a number
and guessing that it should be "M O Q" is the same class of mistake as guessing
at the digits. Jargon is left exactly as written and read however the voice
reads it.
"""

from __future__ import annotations

import re
from typing import Any

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve "
         "thirteen fourteen fifteen sixteen seventeen eighteen nineteen").split()
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety")
_SCALES = ((1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand"))

_MONTHS = ("January February March April May June July August September "
           "October November December").split()

#: How a currency symbol is said, singular and plural.
_CURRENCY = {
    "$": ("dollar", "cent"),
    "£": ("pound", "pence"),
    "€": ("euro", "cent"),
}


def _under_hundred(value: int) -> str:
    if value < 20:
        return _ONES[value]
    tens, ones = divmod(value, 10)
    # Hyphenated, because "twenty-two" is one word to a speech model and
    # "twenty two" invites a pause in the middle of a number.
    return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")


def integer(value: int) -> str:
    """A whole number, spoken.

    No "and": "one hundred five", not "one hundred and five". Both are read
    correctly; the shorter one has fewer places to put a pause.
    """
    if value < 0:
        return "minus " + integer(-value)
    if value < 100:
        return _under_hundred(value)
    parts: list[str] = []
    for size, name in _SCALES:
        if value >= size:
            count, value = divmod(value, size)
            parts.append(f"{integer(count)} {name}")
    if value >= 100:
        hundreds, value = divmod(value, 100)
        parts.append(f"{_ONES[hundreds]} hundred")
    if value:
        parts.append(_under_hundred(value))
    return " ".join(parts)


def digits(text: str) -> str:
    """One digit at a time, the way a person reads a reference out.

    For anything that is an identifier rather than a quantity. Nobody says a
    phone number is nine hundred thousand and something.
    """
    said = []
    for char in text:
        if char.isdigit():
            said.append(_ONES[int(char)])
        elif char in "+":
            said.append("plus")
    return " ".join(said)


def identifier(token: str) -> str:
    """A reference with letters and digits in it, read out.

    Letter runs kept, digit runs read one at a time, separators dropped and the
    parts spaced. "Q3" said as "Qthree" is what happens without the spacing,
    and "SKU-4471" as a quantity is what happens without the rule at all.
    """
    parts = re.findall(r"[A-Za-z]+|\d+", token)
    return " ".join(
        digits(part) if part[0].isdigit() else part for part in parts
    )


def _decimal(whole: str, fraction: str) -> str:
    """A decimal that is not money. "twelve point five", digit by digit after
    the point, because "point five one" and "point fifty-one" are different
    numbers to a listener and only one of them is what was written."""
    said = integer(int(whole or 0))
    if fraction:
        said += " point " + " ".join(_ONES[int(d)] for d in fraction)
    return said


def _money(symbol: str, whole: str, fraction: str) -> str:
    unit, sub = _CURRENCY.get(symbol, ("dollar", "cent"))
    amount = int(whole.replace(",", "") or 0)
    said = f"{integer(amount)} {unit}" + ("" if amount == 1 else "s")
    if fraction and int(fraction.ljust(2, '0')[:2]):
        cents = int(fraction.ljust(2, "0")[:2])
        said += f" {integer(cents)} {sub}" + ("" if cents == 1 else "s")
    return said


def _year(value: int) -> str:
    """1999 is "nineteen ninety-nine", 2005 is "two thousand five"."""
    if 1100 <= value <= 1999 or 2010 <= value <= 2099:
        high, low = divmod(value, 100)
        return f"{integer(high)} {_under_hundred(low) if low else 'hundred'}"
    return integer(value)


# The order below is the whole design. Each pattern consumes the text it
# matched, so the specific shapes have to run before the general ones: an ISO
# date read as three separate numbers, or a time read as a ratio, is a
# regression that only shows up in the ear.

def _time(match: re.Match) -> str:
    hour, minute = int(match.group(1)), match.group(2)
    return f"{integer(hour)} {'hundred hours' if minute == '00' else _spoken_minute(minute)}"


def _spoken_minute(minute: str) -> str:
    value = int(minute)
    # "oh five", not "five", which would be heard as the hour repeating.
    return f"oh {_ONES[value]}" if value < 10 else _under_hundred(value)


def _iso_date(match: re.Match) -> str:
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return match.group(0)
    return f"{_MONTHS[month - 1]} {_ordinal(day)}, {_year(year)}"


def _iso_month(match: re.Match) -> str:
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return match.group(0)
    return f"{_MONTHS[month - 1]} {_year(year)}"


#: A number with thousands separators, or without. **A comma only counts when
#: three digits follow it.** Written as `\d[\d,]*` it swallowed the comma after
#: "$292,187," in a sentence, and the punctuation went with it.
_GROUPED = r"\d{1,3}(?:,\d{3})+|\d+"

_ORDINALS = {1: "first", 2: "second", 3: "third", 5: "fifth", 8: "eighth",
             9: "ninth", 12: "twelfth"}


def _ordinal(day: int) -> str:
    if day in _ORDINALS:
        return _ORDINALS[day]
    if day < 20:
        return _ONES[day] + "th"
    tens, ones = divmod(day, 10)
    if ones == 0:
        return _TENS[tens][:-1] + "ieth"
    return f"{_TENS[tens]}-{_ORDINALS.get(ones, _ONES[ones] + 'th')}"


_RULES: "tuple[tuple[re.Pattern, Any], ...]" = (
    # 1. Times, before anything can read the colon as something else.
    (re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?\b"), _time),
    # 2. Full ISO dates, then bare year-months. Both before plain numbers,
    #    which would otherwise say "twenty twenty-six dash nine dash nine".
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), _iso_date),
    (re.compile(r"\b(\d{4})-(\d{2})\b"), _iso_month),
    # 3. Anything with a letter stuck to it is an identifier, not a quantity.
    #    SKU-4471 is read out digit by digit, which is how a person reads it.
    #    A token that starts with a letter and has a digit in it somewhere.
    #    The first version used two lookaheads and missed SKU-4471, because
    #    the hyphen broke the "letters then a digit" run and the number fell
    #    through to the quantity rule as four thousand four hundred seventy-one.
    (re.compile(r"\b[A-Za-z][A-Za-z_-]*\d[\w-]*\b"), lambda m: identifier(m.group(0))),
    # 4. Money. The symbol decides the words, so it has to be read with the
    #    number rather than left behind as a symbol the voice has to name.
    (re.compile(rf"([$£€])\s?({_GROUPED})(?:\.(\d{{1,2}}))?"),
     lambda m: _money(m.group(1), m.group(2), m.group(3) or "")),
    # 5. Percentages.
    (re.compile(rf"\b({_GROUPED})(?:\.(\d+))?\s?%"),
     lambda m: _decimal(m.group(1).replace(",", ""), m.group(2) or "") + " percent"),
    # 6. A ratio between two numbers. "60/40 terms" is said "sixty forty
    #    terms", which is what it is called out loud.
    (re.compile(rf"\b({_GROUPED})\s?/\s?({_GROUPED})\b"),
     lambda m: f"{integer(int(m.group(1).replace(',', '')))} "
               f"{integer(int(m.group(2).replace(',', '')))}"),
    # 7. A run of seven or more digits with no separators is a reference, not a
    #    quantity: an account number, an order number, a phone number. Read out.
    (re.compile(r"\b\+?\d{7,}\b"), lambda m: digits(m.group(0))),
    # 8. Everything else that is a number, with or without separators.
    (re.compile(rf"\b({_GROUPED})(?:\.(\d+))?\b"),
     lambda m: _decimal(m.group(1).replace(",", ""), m.group(2) or "")),
)


def for_speech(text: str) -> str:
    """The reply, with every number turned into words. **Speech only.**"""
    out = text
    for pattern, replace in _RULES:
        out = pattern.sub(replace, out)
    return out
