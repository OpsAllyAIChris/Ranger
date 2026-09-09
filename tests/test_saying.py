"""Numbers as words, for the ear only.

"$292,187" sent to a speech model as digits is a guess it has to make, and on a
long figure it slurs the middle. Sent as words there is nothing left to guess.

**The screen must not change.** Digits in the window, words in the ear. The
transform lives in `ElevenLabsSpeaker.stream`, which is the one place in the
project where text becomes audio, and the last test in this file is the one
that matters most: nothing else may import it. Spelled-out numbers in a filed
note would be the exact opposite of what the figure audit exists for.
"""

from __future__ import annotations

import pytest

from ranger.saying import digits, for_speech, identifier, integer


@pytest.mark.parametrize(
    "value,words",
    [
        (0, "zero"), (7, "seven"), (13, "thirteen"), (21, "twenty-one"),
        (100, "one hundred"), (105, "one hundred five"),
        (1_000, "one thousand"), (1_250, "one thousand two hundred fifty"),
        (292_187, "two hundred ninety-two thousand one hundred eighty-seven"),
        (1_000_000, "one million"),
        (-40, "minus forty"),
    ],
)
def test_whole_numbers(value: int, words: str):
    assert integer(value) == words


def test_tens_are_hyphenated():
    """One word to a speech model. "twenty two" invites a pause in the middle
    of a number, which is the slurring this is here to fix."""
    assert integer(92) == "ninety-two"


# -- the shapes the operator listed ----------------------------------------


@pytest.mark.parametrize(
    "written,spoken",
    [
        # Currency, which is the case that started this.
        ("$292,187", "two hundred ninety-two thousand one hundred eighty-seven dollars"),
        ("$12.50", "twelve dollars fifty cents"),
        ("$1", "one dollar"),
        ("$4.05", "four dollars five cents"),
        ("£1,000", "one thousand pounds"),
        ("€250", "two hundred fifty euros"),
        # A round amount does not gain "zero cents".
        ("$300.00", "three hundred dollars"),
        # Percentages, whole and not.
        ("40%", "forty percent"),
        ("12.5%", "twelve point five percent"),
        # Dates and times.
        ("2026-09-09", "September ninth, twenty twenty-six"),
        ("2026-08", "August twenty twenty-six"),
        ("14:30", "fourteen thirty"),
        ("9:05", "nine oh five"),
        # A reference, not a quantity.
        ("07700900123", "zero seven seven zero zero nine zero zero one two three"),
        ("100482913", "one zero zero four eight two nine one three"),
        # A decimal that is not money reads digit by digit after the point,
        # because "point five one" and "point fifty-one" are different numbers
        # to a listener and only one of them is what was written.
        ("3.14", "three point one four"),
    ],
)
def test_what_gets_said(written: str, spoken: str):
    assert for_speech(written) == spoken


# -- mixed number and jargon, which is where it gets fiddly ----------------


def test_jargon_beside_a_number_is_left_alone():
    """**The number is spelled out; the word is not touched.**

    Guessing that MOQ should be "M O Q" is the same class of mistake as
    guessing at the digits, and it is one this cannot make from three letters.
    """
    assert for_speech("3,000 MOQ") == "three thousand MOQ"


def test_a_ratio_is_said_the_way_it_is_said_out_loud():
    """60/40 terms is "sixty forty terms" in the room."""
    assert for_speech("60/40 terms") == "sixty forty terms"


def test_an_identifier_is_read_out_rather_than_counted():
    """SKU-4471 is not four thousand four hundred seventy-one. A token with
    letters and digits in it is a reference, and a person reads one digit by
    digit."""
    assert for_speech("SKU-4471") == "SKU four four seven one"
    assert for_speech("order PO-99120") == "order PO nine nine one two zero"


def test_a_letter_and_a_digit_do_not_run_together():
    """Q3 came out as "Qthree" before the parts were spaced."""
    assert for_speech("Q3 revenue") == "Q three revenue"


def test_a_number_beside_a_word_is_still_a_number():
    """"Tier 1" is a quantity with a word in front of it, not an identifier.
    The space is what tells them apart."""
    assert for_speech("Tier 1") == "Tier one"


def test_a_whole_sentence():
    said = for_speech("GP for August was $292,187, up 12.5% on 3,000 MOQ.")

    assert said == (
        "GP for August was two hundred ninety-two thousand one hundred "
        "eighty-seven dollars, up twelve point five percent on three thousand MOQ."
    )


def test_words_with_no_numbers_are_returned_unchanged():
    """It has to be quiet about everything it is not for."""
    line = "Rod has not replied and the quote is still open."

    assert for_speech(line) == line


# -- where it runs, and where it must never run ----------------------------


def test_only_the_speech_path_imports_it():
    """**The failure this test exists for is spelled-out numbers in a filed
    note**, which is the exact opposite of what the figure audit wants.

    The transform belongs to `ElevenLabsSpeaker.stream`, the one place text
    becomes audio. Anything else importing it is a route from this module to
    something that gets kept, and that is worth failing over rather than
    reviewing for.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "ranger"
    importers = []
    for source in sorted(root.glob("**/*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("saying"):
                names = [source.name]
            elif isinstance(node, ast.Import):
                names = [source.name for alias in node.names
                         if alias.name.endswith("saying")]
            importers.extend(names)

    assert sorted(set(importers)) == ["tts.py"], (
        "only the speech path may spell numbers out; everything else keeps "
        "the digits the model wrote"
    )
