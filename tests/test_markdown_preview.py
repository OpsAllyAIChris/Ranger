"""Reading a draft without opening Obsidian, and getting it into Outlook.

A draft is an email on its way somewhere. The panel could preview a generated
.pdf and not the markdown draft sitting next to it, and the way to read one was
to leave the window entirely.

The copy side is the part with a sharp edge. **Plain text is the default
because a draft with `**` and `#` in it, pasted into an email, is worse than
having no button at all** -- nobody notices their own asterisks until the
customer has them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ranger.preview import KINDS, front_matter, preview, previewable, strip_markup

DRAFT = """---
created: 2026-09-08
title: Petmate RFQ follow up
account: Petmate
status: draft, not sent
---

# Petmate RFQ follow up

Hi Dave,

Following up on the **RFQ** we talked about. The `SKU-4471` line is the one
with the *longest* lead time, so I wanted to flag it before you commit to a
delivery date.

- 6 week lead on the 4471
- everything else ships in 10 days
- see [the quote](https://example.com/q/88)

| Item | Qty | Price |
| --- | --- | --- |
| SKU-4471 | 40 | 12.50 |

Best,
Chris
"""


@pytest.fixture
def draft(tmp_path: Path) -> Path:
    path = tmp_path / "2026-09-08-petmate-rfq-follow-up.md"
    path.write_text(DRAFT, encoding="utf-8")
    return path


def test_a_markdown_draft_is_previewable(draft: Path):
    assert "md" in KINDS
    assert previewable(draft)


def test_the_front_matter_is_provenance_and_not_content(draft: Path):
    """**"status: draft, not sent" must never be copied into an email.**

    So it is split off and reported separately, where the interface draws it as
    a line above the draft rather than as part of it.
    """
    rendered = preview(draft, root=draft.parent)

    assert rendered.front["status"] == "draft, not sent"
    assert rendered.front["created"] == "2026-09-08"
    assert "status:" not in rendered.as_plain()
    assert "---" not in rendered.as_plain()


def test_the_plain_text_has_no_markdown_in_it(draft: Path):
    """What lands in Outlook. Every one of these was in the draft."""
    plain = preview(draft, root=draft.parent).as_plain()

    for markup in ("**", "`", "# ", "|", "](http"):
        assert markup not in plain, f"{markup!r} survived into the plain text"
    assert "RFQ" in plain and "SKU-4471" in plain
    assert "https://example.com/q/88" in plain, "a link keeps where it went"


def test_a_wrapped_paragraph_is_one_line_and_a_sign_off_is_not(draft: Path):
    """**Both halves, and they pull against each other.**

    A draft hard-wrapped at eighty characters must not paste into an email with
    a break every eighty characters. But `Best,` and `Chris` are two lines on
    purpose and joining them is just as wrong. The length of the line before
    the break decides.
    """
    plain = preview(draft, root=draft.parent).as_plain()

    assert "The SKU-4471 line is the one with the longest lead time" in plain, (
        "the wrapped sentence came back together"
    )
    assert "Best,\nChris" in plain, "and the sign-off did not"


def test_bullets_stay_together_and_paragraphs_do_not(draft: Path):
    plain = preview(draft, root=draft.parent).as_plain()

    assert "• 6 week lead on the 4471\n• everything else" in plain, (
        "a list reads as one thing"
    )
    assert "Hi Dave,\n\nFollowing up" in plain, "paragraphs keep their gap"


def test_a_table_keeps_its_tabs_so_it_pastes_into_cells(draft: Path):
    plain = preview(draft, root=draft.parent).as_plain()

    assert "Item\tQty\tPrice" in plain
    assert "SKU-4471\t40\t12.50" in plain


def test_the_source_is_the_file_as_written(draft: Path):
    """The second button. Reconstructing markdown from the parsed blocks would
    hand back an approximation of the operator's own file."""
    rendered = preview(draft, root=draft.parent)

    assert rendered.source == DRAFT


def test_as_text_still_puts_the_markdown_back(draft: Path):
    """`as_text` is for a model, which reads structure better with the markers
    on. `as_plain` is for an email, which does not. They are different jobs and
    this is what stops one being quietly used for the other."""
    rendered = preview(draft, root=draft.parent)

    assert "# Petmate RFQ follow up" in rendered.as_text()
    assert "- 6 week lead" in rendered.as_text()
    assert "#" not in rendered.as_plain()


@pytest.mark.parametrize(
    "text,expected",
    [
        ("**bold**", "bold"),
        ("*italic*", "italic"),
        ("***both***", "both"),
        ("`code`", "code"),
        ("~~gone~~", "gone"),
        ("[quote](http://x/y)", "quote (http://x/y)"),
        ("![alt](img.png)", "alt"),
        # Left alone on purpose. An underscore inside a word is a filename or a
        # column header far more often than it is emphasis, and a mangled
        # account name in front of a customer is worse than a stray character.
        ("GP_TOTAL_2026", "GP_TOTAL_2026"),
        ("a * b", "a * b"),
        ("2 * 3 * 4", "2 * 3 * 4"),
    ],
)
def test_what_markup_comes_off_and_what_stays(text: str, expected: str):
    assert strip_markup(text) == expected


def test_a_draft_with_no_front_matter_still_reads(tmp_path: Path):
    path = tmp_path / "scratch.md"
    path.write_text("Just a note.\n\nAnd another line.\n", encoding="utf-8")

    rendered = preview(path, root=tmp_path)

    assert rendered.front == {}
    assert rendered.as_plain().startswith("Just a note.")


def test_front_matter_that_is_not_closed_is_not_front_matter():
    """Three dashes at the top of a note is a horizontal rule until there is a
    second three dashes. Eating the rest of the file would lose the draft."""
    fields, body = front_matter("---\nnot closed\n\nreal content\n")

    assert fields == {}
    assert "real content" in body


def test_a_fenced_block_keeps_its_characters(tmp_path: Path):
    """Inside a fence the text is literal. Stripping markup there would edit
    the thing being quoted."""
    path = tmp_path / "code.md"
    path.write_text("Look:\n\n```\nrate = a ** b\n```\n\nDone.\n", encoding="utf-8")

    rendered = preview(path, root=tmp_path)

    assert "rate = a ** b" in rendered.as_plain()


def test_a_long_draft_says_it_was_truncated(tmp_path: Path):
    path = tmp_path / "long.md"
    path.write_text("\n\n".join(f"Paragraph {n}." for n in range(400)), encoding="utf-8")

    rendered = preview(path, root=tmp_path, max_blocks=20)

    assert rendered.truncated
    assert rendered.total_blocks == 400
    assert len(rendered.blocks) == 20


def test_an_unreadable_encoding_is_read_rather_than_refused(tmp_path: Path):
    """Written on Windows by something other than Jarvis. A draft that cannot
    be opened is worse than one shown with a wrong dash."""
    path = tmp_path / "cp1252.md"
    # The bytes, not a string encoded to them: U+2013 is what cp1252 0x96
    # decodes to, and encoding it back is what a test writes when it means
    # "these bytes". An en dash written by Word is the everyday case.
    path.write_bytes(b"Price is 12.50 \x96 firm.\n")

    rendered = preview(path, root=tmp_path)

    assert not rendered.error
    assert "Price is 12.50" in rendered.as_plain()
