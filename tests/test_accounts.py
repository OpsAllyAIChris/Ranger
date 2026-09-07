"""Parsing, resolving and reporting on account notes.

Every fixture under tests/fixtures/vault matches the operator's format
contract. The fixtures are fictional; the operator's real notes hold customer
email, pricing and confidential material and are never committed here.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from ranger.accounts import (
    parse_note,
    quiet_report,
    render_digest,
    resolve_account,
    scan_note,
)

FIXTURES = Path(__file__).parent / "fixtures" / "vault"
ACCOUNTS = FIXTURES / "Accounts"
TODAY = date(2026, 9, 7)


def read(name: str) -> str:
    return (ACCOUNTS / f"{name}.md").read_text(encoding="utf-8")


def note(name: str):
    return parse_note(read(name), name, ACCOUNTS / f"{name}.md")


# -- the format contract ---------------------------------------------------


def test_fixtures_exist():
    assert len(list(ACCOUNTS.glob("*.md"))) == 6


def test_metadata_block_is_read():
    meta = note("Illes Foods").metadata
    assert meta["Status"] == "Active"
    assert meta["Tier"] == "A"
    assert meta["Annual packaging spend"] == "$1.2M"
    assert meta["Decision structure"] == "Rod signs, Marcy specifies"


def test_optional_sections_are_read():
    sections = note("Illes Foods").sections
    assert set(sections) >= {"Pain points", "Target solution", "Notes", "Contacts", "Opportunities", "Activity"}
    assert "retort line" in sections["Pain points"]
    assert "Rod Illes, President" in sections["Contacts"]
    assert "### Retort film conversion" in sections["Opportunities"]


def test_contact_bullets_are_not_mistaken_for_metadata():
    """Metadata is only the block before the first ## heading."""
    meta = note("Illes Foods").metadata
    assert "Owner" not in meta          # that one lives under Opportunities
    assert "Competitor" not in meta


def test_activity_headings_are_parsed():
    activities = note("Illes Foods").activities
    assert len(activities) == 6
    assert activities[0].date == date(2026, 9, 4)
    assert activities[0].kind == "Call"
    assert activities[0].contact == "Rod Illes"
    assert "confirmed volumes" in activities[0].body


def test_activity_without_a_contact():
    last = note("Illes Foods").activities[-1]
    assert last.date == date(2026, 6, 18)
    assert last.kind == "Call"
    assert last.contact is None


def test_activities_are_newest_first_regardless_of_file_order():
    scrambled = """## Activity
### 2026-02-04 | Email
older
### 2026-09-04 | Call | Rod
newest
### 2026-06-18 | Meeting
middle
"""
    dates = [a.date for a in parse_note(scrambled, "x").activities]
    assert dates == sorted(dates, reverse=True)


def test_trailing_pipe_and_padding_in_a_heading():
    text = "## Activity\n###   2026-09-04 |  Call  |  \nbody\n"
    activity = parse_note(text, "x").activities[0]
    assert activity.kind == "Call" and activity.contact is None


def test_a_note_with_no_activity_section():
    assert note("Northwind Provisions").activities == ()
    assert note("Northwind Provisions").last_activity is None


def test_unconfirmed_is_visible_on_the_note():
    assert note("Cedar Ridge Dairy").unconfirmed
    assert not note("Illes Foods").unconfirmed


def test_scan_agrees_with_the_full_parse():
    """The quiet check uses the cheap scan. It must not disagree."""
    for path in ACCOUNTS.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        full = parse_note(text, path.stem, path)
        cheap = scan_note(text, path.stem, path)
        assert cheap.last_activity == full.last_activity, path.name
        assert cheap.activity_count == len(full.activities), path.name
        assert cheap.unconfirmed == full.unconfirmed, path.name


def test_a_date_in_a_heading_that_is_not_an_activity_is_ignored():
    """Only ### headings inside ## Activity count."""
    text = """- **Status:** Active
- **Renewal:** 2027-01-01

## Notes
Contract renews 2027-01-01.

## Activity
### 2026-03-11 | Email
only this one counts
"""
    assert parse_note(text, "x").last_activity == date(2026, 3, 11)
    assert scan_note(text, "x", Path("x")).last_activity == date(2026, 3, 11)


# -- name resolution -------------------------------------------------------

NAMES = sorted(p.stem for p in ACCOUNTS.glob("*.md"))


@pytest.mark.parametrize(
    "spoken,expected",
    [
        ("Illes", "Illes Foods"),
        ("illes foods", "Illes Foods"),
        ("ILLES", "Illes Foods"),
        ("Rusty", "Rusty Supply Co"),
        ("northwind", "Northwind Provisions"),
    ],
)
def test_a_spoken_fragment_resolves(spoken, expected):
    assert resolve_account(spoken, NAMES).match == expected


def test_a_near_miss_resolves_by_close_match():
    resolution = resolve_account("Northwynd", NAMES)
    assert resolution.match == "Northwind Provisions"
    assert resolution.how == "close"


def test_more_than_one_match_is_never_picked_silently():
    resolution = resolve_account("Pegasus", NAMES)
    assert resolution.match is None
    assert resolution.ambiguous
    assert resolution.candidates == ("Pegasus Logistics", "Pegasus Packaging")


def test_no_match_is_reported_as_no_match():
    assert resolve_account("Wingfield Aerospace", NAMES).how == "none"
    assert resolve_account("   ", NAMES).how == "none"


# -- what went quiet -------------------------------------------------------


def scans():
    return [
        scan_note(p.read_text(encoding="utf-8"), p.stem, p)
        for p in sorted(ACCOUNTS.glob("*.md"))
    ]


def test_quiet_report_splits_the_three_groups():
    report = quiet_report(scans(), threshold_days=21, as_of=TODAY)

    assert [a.name for a in report.lapsed] == ["Rusty Supply Co", "Pegasus Logistics"]
    assert report.lapsed[0].days == 180
    assert report.lapsed[1].days == 97
    assert report.never_touched == ("Northwind Provisions",)
    assert report.active == 2                      # Illes Foods, Pegasus Packaging
    assert report.skipped_unconfirmed == ("Cedar Ridge Dairy",)
    assert report.considered == 5


def test_never_touched_are_not_mixed_into_lapsed():
    """They would sort to the top and drown the genuinely lapsed accounts."""
    report = quiet_report(scans(), threshold_days=21, as_of=TODAY)
    assert "Northwind Provisions" not in [a.name for a in report.lapsed]


def test_unconfirmed_notes_are_skipped_not_reported():
    report = quiet_report(scans(), threshold_days=1, as_of=TODAY)
    everything = [a.name for a in report.lapsed] + list(report.never_touched)
    assert "Cedar Ridge Dairy" not in everything


def test_skip_statuses_is_configurable():
    report = quiet_report(scans(), threshold_days=21, as_of=TODAY, skip_statuses=())
    assert "Cedar Ridge Dairy" in [a.name for a in report.lapsed]


def test_threshold_moves_the_boundary():
    generous = quiet_report(scans(), threshold_days=365, as_of=TODAY)
    assert generous.lapsed == ()
    assert generous.active == 4

    strict = quiet_report(scans(), threshold_days=1, as_of=TODAY)
    assert len(strict.lapsed) == 4


def test_lapsed_is_sorted_longest_first():
    report = quiet_report(scans(), threshold_days=1, as_of=TODAY)
    days = [a.days for a in report.lapsed]
    assert days == sorted(days, reverse=True)


# -- the digest is bounded -------------------------------------------------


def test_digest_carries_the_useful_parts():
    digest = render_digest(note("Illes Foods"))
    assert "Illes Foods" in digest
    assert "**Tier:** A" in digest
    assert "retort line" in digest
    assert "2026-09-04 | Call | Rod Illes" in digest
    assert "6 logged activities, 2026-06-18 to 2026-09-04" in digest


def test_digest_shows_only_the_recent_activities():
    digest = render_digest(note("Illes Foods"), activities=2)
    assert "2026-09-04" in digest
    assert "2026-02-04" not in digest
    assert "4 older activities not shown" in digest


def test_digest_of_a_huge_note_stays_bounded(tmp_path):
    """Real notes reach 25,000 characters. A digest must not."""
    base = read("Illes Foods")
    filler = "\n".join(
        f"### 2025-{m:02d}-{d:02d} | Email | Someone\n" + ("padding text " * 90)
        for m in range(1, 13)
        for d in (1, 11, 21)
    )
    huge = base + "\n" + filler
    assert len(huge) > 25_000

    parsed = parse_note(huge, "Illes Foods")
    digest = render_digest(parsed, max_chars=4000)

    assert len(digest) <= 4000
    assert len(parsed.activities) == 42
    assert "2026-09-04" in digest          # newest still present


def test_digest_never_reports_zero_activity_as_quiet_language():
    digest = render_digest(note("Northwind Provisions"))
    assert "No activity has ever been logged" in digest
