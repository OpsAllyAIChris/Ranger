"""Tier 5: the morning brief.

Built against the shape of the operator's real problem: 58 accounts, 41 past a
21 day threshold, topped by one at 215 days. A boolean threshold produced most
of the book sorted by how thoroughly each account had been abandoned, which is
the list least likely to be acted on.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ranger.accounts import NoteScan, scan_note
from ranger.brief import (
    BriefStore,
    build,
    read_dormant,
    read_seen,
    render_dormant,
    render_seen,
)
from ranger.config import AccountsConfig, BriefConfig

TODAY = date(2026, 9, 7)


def scan(name, *, days=None, activities=5, tier="", stages=(), status=""):
    """One account note's worth of facts, without writing a note."""
    last = None if days is None else date.fromordinal(TODAY.toordinal() - days)
    return NoteScan(
        name=name,
        path=Path(f"{name}.md"),
        status=status,
        last_activity=last,
        activity_count=activities,
        tier=tier,
        opportunity_stages=tuple(stages),
    )


@pytest.fixture
def accounts():
    return AccountsConfig(
        quiet_after_days=21,
        skip_statuses=("UNCONFIRMED",),
        tier_order=("Tier 1", "Tier 2", "Tier 3"),
        open_stages=("Qualifying", "Proposal"),
    )


@pytest.fixture
def brief_config():
    return BriefConfig()


#: An entry for an account no test scans, so `seen` is non-empty and the brief
#: is not treated as a first run. Without it every test would exercise the
#: first-run path, where nothing can have just crossed.
HISTORY = {"An Account From Last Week": date(2026, 8, 30)}


def make(scans, accounts, brief_config, *, seen=None, dormant=None, as_of=TODAY, fresh=False):
    """Build a brief. `fresh=True` means no history at all, the first ever run."""
    if seen is None:
        seen = {} if fresh else dict(HISTORY)
    return build(
        scans,
        accounts=accounts,
        brief=brief_config,
        as_of=as_of,
        seen=seen,
        dormant=dormant,
    )


# -- the size of the thing --------------------------------------------------


def test_the_brief_has_a_size_not_a_threshold(accounts, brief_config):
    """41 of 58 past the threshold must not become 41 lines."""
    scans = [scan(f"Account {i:02d}", days=30 + i, activities=10) for i in range(41)]
    scans += [scan(f"Fine {i}", days=3) for i in range(17)]

    result, _ = make(scans, accounts, brief_config)
    lines = len(result.slipping) + len(result.deals) + (1 if result.decision else 0)
    assert lines <= brief_config.lines
    assert result.withheld > 30
    assert "Ask for the full list" in result.render()


def test_the_deepest_lapse_is_not_the_headline(accounts, brief_config):
    """Longest first is sorting by regret. 215 days is a decision already made."""
    scans = [
        scan("Gabriel Ranch Beef", days=215, activities=2),
        scan("Illes Foods", days=25, activities=35),
    ]
    result, _ = make(scans, accounts, brief_config)

    assert [line.account for line in result.slipping] == ["Illes Foods"]
    assert result.decision is not None
    assert result.decision.account == "Gabriel Ranch Beef"


# -- the ranking ------------------------------------------------------------


def test_a_relationship_that_lapsed_beats_one_that_never_started(accounts, brief_config):
    """Activity count is the cheapest proxy for whether there was anything there."""
    scans = [
        scan("Two Touches Ever", days=30, activities=2),
        scan("Worked Hard For Years", days=30, activities=40),
    ]
    result, _ = make(scans, accounts, brief_config)
    assert [line.account for line in result.slipping] == [
        "Worked Hard For Years",
        "Two Touches Ever",
    ]


def test_tier_outranks_activity_count(accounts, brief_config):
    scans = [
        scan("Small But Busy", days=30, activities=40, tier="Tier 3"),
        scan("Big And Quiet", days=30, activities=6, tier="Tier 1"),
    ]
    result, _ = make(scans, accounts, brief_config)
    assert result.slipping[0].account == "Big And Quiet"


def test_an_unknown_tier_sorts_last_and_never_first(accounts, brief_config):
    scans = [
        scan("Mystery", days=30, activities=99, tier="Platinum Elite"),
        scan("Known", days=30, activities=1, tier="Tier 3"),
    ]
    result, _ = make(scans, accounts, brief_config)
    assert result.slipping[0].account == "Known"


def test_with_no_tier_order_configured_activity_count_leads(brief_config):
    """It degrades to something sensible before the survey has been run."""
    plain = AccountsConfig(quiet_after_days=21)
    scans = [
        scan("Thin", days=30, activities=2, tier="Tier 1"),
        scan("Thick", days=30, activities=30, tier="Tier 3"),
    ]
    result, _ = build(
        scans, accounts=plain, brief=brief_config, as_of=TODAY, seen=dict(HISTORY)
    )
    assert result.slipping[0].account == "Thick"


# -- reporting a change, not a state ---------------------------------------


def test_only_accounts_that_just_crossed_are_slipping(accounts, brief_config):
    """Quiet 25, then 26, then 27 days is the same fact three times."""
    scans = [scan("Told You Already", days=40), scan("New Today", days=22)]
    seen = {"Told You Already": date(2026, 8, 20)}

    result, _ = make(scans, accounts, brief_config, seen=seen)
    assert [line.account for line in result.slipping] == ["New Today"]


def test_crossing_is_recorded_so_tomorrow_knows(accounts, brief_config):
    scans = [scan("New Today", days=22)]
    result, seen = make(scans, accounts, brief_config)
    assert [line.account for line in result.slipping] == ["New Today"]
    assert seen["New Today"] == TODAY

    again, _ = make(scans, accounts, brief_config, seen=seen)
    assert again.slipping == ()


def test_an_account_that_was_worked_again_can_slip_afresh(accounts, brief_config):
    """Otherwise a save is punished: rescue it once and it never resurfaces."""
    scans = [scan("Rescued", days=2)]
    _, seen = make(scans, accounts, brief_config, seen={"Rescued": date(2026, 8, 1)})
    assert "Rescued" not in seen


# -- deals ------------------------------------------------------------------


def test_an_open_deal_going_quiet_gets_its_own_bucket(accounts, brief_config):
    scans = [
        scan("Just A Relationship", days=40, activities=30),
        scan("Deal Stalling", days=40, activities=8, stages=("Proposal",)),
    ]
    seen = {"Just A Relationship": date(2026, 8, 1), "Deal Stalling": date(2026, 8, 1)}
    result, _ = make(scans, accounts, brief_config, seen=seen)

    assert [line.account for line in result.deals] == ["Deal Stalling"]
    assert "deal open" in result.render()


def test_a_closed_deal_is_not_a_deal(accounts, brief_config):
    """The whole point of accounts.open_stages, if closed rows stay in the export."""
    scans = [scan("Won It Already", days=40, stages=("Closed Won",))]
    seen = {"Won It Already": date(2026, 8, 1)}
    result, _ = make(scans, accounts, brief_config, seen=seen)
    assert result.deals == ()


def test_with_no_open_stages_configured_every_opportunity_counts(brief_config):
    plain = AccountsConfig(quiet_after_days=21)
    scans = [scan("Has Something", days=40, stages=("Closed Lost",))]
    result, _ = build(
        scans,
        accounts=plain,
        brief=brief_config,
        as_of=TODAY,
        seen={"Has Something": date(2026, 8, 1)},
    )
    assert [line.account for line in result.deals] == ["Has Something"]


def test_an_account_is_never_in_two_buckets(accounts, brief_config):
    scans = [scan("Both", days=22, stages=("Proposal",))]
    result, _ = make(scans, accounts, brief_config)
    assert len(result.slipping) + len(result.deals) == 1


# -- the decision that drains the backlog ----------------------------------


def test_cold_accounts_never_appear_in_the_daily_buckets(accounts, brief_config):
    scans = [scan("Ancient", days=215, activities=30, tier="Tier 1", stages=("Proposal",))]
    result, _ = make(scans, accounts, brief_config)
    assert result.slipping == () and result.deals == ()
    assert result.decision is not None and result.decision.account == "Ancient"


def test_the_decision_rotates_so_the_backlog_drains(accounts, brief_config):
    scans = [scan(f"Cold {i:02d}", days=200 + i) for i in range(10)]
    asked = set()
    for offset in range(10):
        as_of = date.fromordinal(TODAY.toordinal() + offset)
        result, _ = make(scans, accounts, brief_config, as_of=as_of)
        asked.add(result.decision.account)
    assert len(asked) == 10, "every cold account should come up within its own length"


def test_the_same_day_asks_about_the_same_account(accounts, brief_config):
    """Running the check twice must not look like two different answers."""
    scans = [scan(f"Cold {i}", days=200 + i) for i in range(6)]
    first, _ = make(scans, accounts, brief_config)
    second, _ = make(scans, accounts, brief_config)
    assert first.decision.account == second.decision.account


def test_the_decision_line_is_always_left_room(accounts, brief_config):
    """It is the only thing that shrinks the list, so it never gets trimmed."""
    scans = [scan(f"Warm {i}", days=30 + i, activities=20, stages=("Proposal",)) for i in range(9)]
    scans += [scan("Ancient", days=300)]
    result, _ = make(scans, accounts, brief_config)

    assert result.decision is not None
    total = len(result.slipping) + len(result.deals) + 1
    assert total <= brief_config.lines


def test_a_dormant_account_is_gone_from_the_brief_entirely(accounts, brief_config):
    scans = [scan("Gabriel Ranch Beef", days=215), scan("Live One", days=25)]
    result, _ = make(scans, accounts, brief_config, dormant={"gabriel ranch beef"})

    rendered = result.render()
    assert "Gabriel Ranch Beef" not in rendered
    assert result.dormant == 1
    assert "1 dormant by your decision" in rendered


# -- the groups that were already right ------------------------------------


def test_never_touched_stays_separate(accounts, brief_config):
    scans = [scan("Never Worked", days=None), scan("Lapsed", days=30)]
    result, _ = make(scans, accounts, brief_config)
    assert result.never_touched == ("Never Worked",)
    assert all(line.account != "Never Worked" for line in result.slipping)


def test_unconfirmed_notes_are_still_set_aside(accounts, brief_config):
    scans = [scan("Cedar Ridge Dairy", days=40, status="UNCONFIRMED")]
    result, _ = make(scans, accounts, brief_config)
    assert result.skipped == 1
    assert "Cedar Ridge Dairy" not in result.render()


def test_a_quiet_morning_says_so_rather_than_printing_nothing(accounts, brief_config):
    result, _ = make([scan("Fine", days=2)], accounts, brief_config)
    assert "Nothing new is slipping" in result.render()


# -- the two small files ----------------------------------------------------


def test_the_seen_file_round_trips():
    seen = {"Illes Foods": date(2026, 9, 1), "Wexxar": date(2026, 8, 14)}
    assert read_seen(render_seen(seen)) == seen


def test_the_seen_file_survives_a_hand_edit():
    text = render_seen({"Illes Foods": date(2026, 9, 1)}) + "- not a date | Nonsense\n- \n"
    assert read_seen(text) == {"Illes Foods": date(2026, 9, 1)}


def test_the_dormant_file_forgives_a_comment_and_ignores_the_prose():
    text = render_dormant(["Gabriel Ranch Beef"]) + "- Old Mill Co  # lost to a competitor\n"
    names = read_dormant(text)
    assert "gabriel ranch beef" in names
    assert "old mill co" in names


def test_dormant_matching_is_case_insensitive(accounts, brief_config):
    scans = [scan("Gabriel Ranch Beef", days=215)]
    result, _ = make(scans, accounts, brief_config, dormant=read_dormant("- gabriel RANCH beef\n"))
    assert result.dormant == 1


def test_the_store_writes_inside_rangers_own_folder(config, vault, brief_config):
    store = BriefStore(vault, config.vault.ranger, brief_config)
    store.write_seen({"Illes Foods": date(2026, 9, 1)})
    assert store.seen() == {"Illes Foods": date(2026, 9, 1)}
    assert store.seen_path.is_relative_to(config.vault.ranger)

    assert store.sleep("Gabriel Ranch Beef") is True
    assert store.sleep("Gabriel Ranch Beef") is False
    assert "gabriel ranch beef" in store.dormant()
    assert store.wake("Gabriel Ranch Beef") is True
    assert store.dormant() == set()


def test_a_missing_file_reads_as_empty_rather_than_failing(config, vault, brief_config):
    store = BriefStore(vault, config.vault.ranger, brief_config)
    assert store.seen() == {}
    assert store.dormant() == set()


# -- the tier and stage vocabulary, read from a real note -------------------


def test_a_note_gives_up_its_tier_and_its_opportunity_stages():
    note = """\
- **Status:** ACTIVE
- **Tier:** Tier 1
- **Industry:** Food

## Opportunities
### Case erector replacement
- **Stage:** Proposal
- **Owner:** Chris

### Tape award
- **Stage:** Closed Lost

## Activity
### 2026-09-01 | Call | Rod
"""
    result = scan_note(note, "Illes Foods", Path("Illes Foods.md"))
    assert result.tier == "Tier 1"
    assert result.opportunity_stages == ("Proposal", "Closed Lost")
    assert result.opportunities == 2


def test_an_opportunity_owner_is_not_mistaken_for_an_account_tier():
    note = """\
- **Tier:** Tier 2

## Opportunities
### Something
- **Tier:** ignore me
"""
    assert scan_note(note, "X", Path("X.md")).tier == "Tier 2"


def test_a_note_with_no_opportunities_section_has_none():
    assert scan_note("- **Tier:** Tier 3\n", "X", Path("X.md")).opportunity_stages == ()


def test_the_first_brief_does_not_claim_forty_accounts_just_crossed(accounts, brief_config):
    """No history means no history, not a page of false alarms."""
    scans = [scan(f"Old {i}", days=100 + i) for i in range(6)]
    scans += [scan("Recent", days=25)]

    result, seen = make(scans, accounts, brief_config, fresh=True)
    assert result.slipping == ()
    assert result.first_run
    assert "first brief" in result.render()
    assert len(seen) == 7, "but everything quiet is recorded, so tomorrow is honest"

    tomorrow = date.fromordinal(TODAY.toordinal() + 1)
    scans.append(scan("Crossed Overnight", days=22))
    later, _ = make(scans, accounts, brief_config, seen=seen, as_of=tomorrow)
    assert [line.account for line in later.slipping] == ["Crossed Overnight"]
    assert not later.first_run


# -- finding where a stage lives -------------------------------------------


def test_an_opportunity_gives_up_its_meta_line_and_its_fields():
    """The operator's export has 45 opportunities and no Stage line at all.

    The survey has to report the shape that is really there, because the
    export's schema is not written down anywhere this repository can see.
    """
    from ranger.accounts import opportunity_shapes

    note = """\
## Opportunities
### Case erector replacement
Open | $118,225.18 | 2026-Q4
- **Owner:** Chris
- **Product:** WF20H

### Tape award
Lost | $9,400.00 | 2025-Q3
- **Owner:** Chris

## Activity
### 2026-09-01 | Call | Rod
"""
    shapes = opportunity_shapes(note)
    assert len(shapes) == 2
    meta, fields = shapes[0]
    assert meta == ("Open", "$118,225.18", "2026-Q4")
    assert ("Owner", "Chris") in fields
    assert ("Product", "WF20H") in fields
    assert shapes[1][0][0] == "Lost"


def test_an_opportunity_with_no_meta_line_still_reports_its_fields():
    from ranger.accounts import opportunity_shapes

    shapes = opportunity_shapes("## Opportunities\n### Plain\n- **Owner:** Chris\n")
    assert shapes == [((), (("Owner", "Chris"),))]


@pytest.mark.parametrize(
    "value, hidden",
    [
        ("$118,225.18", True),
        ("118,225.18", True),
        ("9400.00", True),
        ("£12,000", True),
        ("2026-Q4", False),
        ("Open", False),
        ("Closed Won", False),
        ("WF20H", False),
    ],
)
def test_the_survey_never_prints_an_amount(value, hidden):
    """Pricing is the one thing that must not reach a terminal that gets
    screenshotted, and cardinality alone would not catch a column where every
    deal carries the same figure."""
    from ranger.cli import _LOOKS_LIKE_MONEY

    assert bool(_LOOKS_LIKE_MONEY.search(value)) is hidden
