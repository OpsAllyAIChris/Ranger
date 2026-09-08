"""Line endings, asserted at the byte level, on every platform.

The account write path's whole design is byte identity: the bytes above the
marker must be the same bytes after a write as before it. That promise is
platform-specific in a way nothing in the suite noticed, because the suite ran
on Linux and Linux translates nothing.

`Path.write_text` opens in text mode. On Windows that turns every `\\n` into
`\\r\\n`, so a file that already used `\\r\\n` comes back as `\\r\\r\\n`, and
`read_text` with universal newlines turns that into `\\n\\n`. The bytes above
the marker then differ from the bytes that were read, the guard refuses, and it
refuses for the wrong reason: the file was fine and the writer corrupted it.

**These tests write the fixture bytes by hand and assert on bytes.** They do not
use `write_text` to build a CRLF file, because that would be the same
translation deciding both what the test writes and what it expects. The real
vault came out of four separate CRM exports and nothing guarantees they agree
on endings, so a lone `\\r` and a mixed file are here too.
"""

from __future__ import annotations

from datetime import date

import pytest

from ranger.marker import BLOCK, count, entry, migrate
from ranger.vault import Vault

BODY = "# Illes Foods\n\n- **Tier:** 1\n\n## Activity\n\n### 2026-09-04 | Call | Rod\nnotes\n"

#: Built by replacing bytes, never by writing text, so the fixture is exactly
#: what it says it is on every platform.
ENDINGS = {
    "lf": b"\n",
    "crlf": b"\r\n",
    "cr": b"\r",
}


def body_bytes(style: str) -> bytes:
    return BODY.encode("utf-8").replace(b"\n", ENDINGS[style])


def marked_bytes(style: str) -> bytes:
    return body_bytes(style) + ENDINGS[style] + BLOCK.encode("utf-8").replace(
        b"\n", ENDINGS[style]
    )


MIXED = (
    b"# Illes Foods\r\n\r\n- **Tier:** 1\n\n## Activity\r\n\r\n"
    b"### 2026-09-04 | Call | Rod\rnotes\n"
)


@pytest.fixture
def vault(config):
    return Vault(config.vault)


def account(config, name: str, data: bytes):
    path = config.vault.accounts / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def bytes_above(path) -> bytes:
    """The half the promise is about, read as bytes and split as bytes."""
    from ranger.marker import TOKEN

    data = path.read_bytes()
    return data[: data.index(TOKEN.encode("utf-8"))]


# -- the append path --------------------------------------------------------


@pytest.mark.parametrize("style", ["lf", "crlf", "cr"])
def test_appending_leaves_the_export_bytes_untouched(vault, config, style):
    """The promise, checked as bytes rather than as text.

    On Linux this passed for all three before the fix, because nothing
    translated. On Windows only `lf` could ever pass.
    """
    path = account(config, f"Illes {style}", marked_bytes(style))
    was = bytes_above(path)

    vault.append_below_marker(path, entry("Rod wants pricing", "call", date(2026, 9, 8)))

    assert bytes_above(path) == was


def test_appending_to_a_mixed_ending_note_leaves_the_export_bytes_untouched(vault, config):
    """The real vault came out of four CRM exports and nothing guarantees they
    agree. A file with all three endings must still round-trip exactly."""
    path = account(config, "Illes mixed", MIXED + b"\n" + BLOCK.encode("utf-8"))
    was = bytes_above(path)

    vault.append_below_marker(path, entry("something", "call", date(2026, 9, 8)))

    assert bytes_above(path) == was


@pytest.mark.parametrize("style", ["lf", "crlf", "cr"])
def test_appending_does_not_rewrite_endings_anywhere_in_the_file(vault, config, style):
    """Not only above the marker. Rewriting the whole file's endings is a
    modification even when the text is unchanged, and it would show up as a
    whole-file diff in every snapshot."""
    path = account(config, f"Rusty {style}", marked_bytes(style))
    before = path.read_bytes()

    vault.append_below_marker(path, entry("x", "call", date(2026, 9, 8)))

    after = path.read_bytes()
    assert after.startswith(before.rstrip(b"\r\n")), "the existing bytes moved"


@pytest.mark.parametrize("style", ["lf", "crlf"])
def test_what_is_appended_matches_the_file_it_is_appended_to(vault, config, style):
    """A CRLF note that grows LF-only lines is a file with mixed endings that
    Ranger created. Harmless to render, ugly in a diff, and avoidable."""
    path = account(config, f"Gabriel {style}", marked_bytes(style))

    vault.append_below_marker(path, entry("x", "call", date(2026, 9, 8)))

    tail = path.read_bytes().split(b"-->")[-1]
    if style == "crlf":
        assert b"\r\n" in tail
        assert tail.replace(b"\r\n", b"") .count(b"\n") == 0, "an LF crept into a CRLF note"
    else:
        assert b"\r" not in tail


@pytest.mark.parametrize("style", ["lf", "crlf", "cr"])
def test_the_appended_entry_can_be_read_back(vault, config, style):
    """Byte identity is not worth much if the note stops parsing."""
    from ranger.accounts import parse_note

    path = account(config, f"Petmate {style}", marked_bytes(style))
    vault.append_below_marker(path, entry("Rod wants pricing", "call", date(2026, 9, 8)))

    parsed = parse_note(path.read_text(encoding="utf-8"), "Petmate", path)

    assert date(2026, 9, 8) in [a.date for a in parsed.activities]


# -- the migration ----------------------------------------------------------


@pytest.mark.parametrize("style", ["lf", "crlf", "cr"])
def test_the_migration_appends_and_modifies_nothing(config, style):
    """The migration reads and writes the whole file, so a text-mode write
    would rewrite every line ending in the CRM half -- silently modifying the
    exact thing the marker exists to protect, before the marker even exists.
    """
    path = account(config, f"Illes {style}", body_bytes(style))
    before = path.read_bytes()

    migrate(None, config.vault.accounts)

    after = path.read_bytes()
    assert after.startswith(before), "the migration rewrote what was already there"
    assert count(after.decode("utf-8")) == 1


def test_the_migration_leaves_a_mixed_ending_note_alone(config):
    path = account(config, "Illes mixed", MIXED)
    before = path.read_bytes()

    migrate(None, config.vault.accounts)

    assert path.read_bytes().startswith(before)


@pytest.mark.parametrize("style", ["lf", "crlf", "cr"])
def test_a_second_migration_of_a_crlf_note_changes_nothing(config, style):
    path = account(config, f"Rusty {style}", body_bytes(style))
    migrate(None, config.vault.accounts)
    after_first = path.read_bytes()

    migrate(None, config.vault.accounts)

    assert path.read_bytes() == after_first


# -- the guard cannot be handed a decoded string ---------------------------


def test_the_digest_refuses_text():
    """The bug in one line: a digest taken over a decoded string is a digest of
    something that has already been through a translating reader. Making it
    bytes-only means this class cannot come back quietly."""
    from ranger.marker import digest

    assert digest(b"abc")

    with pytest.raises(TypeError):
        digest("abc")
