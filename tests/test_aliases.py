"""Accounts the CRM exports under two names, folded back into one.

Six real pairs in the operator's export split one account's history in half:
BWI Companies against BWI Company, TST Impreso against TST Impresso, Vytalogy
against Vitalogy. The rule the whole module exists under is that **Ranger
proposes and the operator approves**. A wrong merge is wrong in both accounts
forever and shows up as an error in neither, so the tests that matter most here
are the ones about what is *not* suggested and what is *not* silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from ranger.aliases import (
    Alias,
    AliasFile,
    Aliases,
    fold,
    parse,
    render,
    suggest,
)


# -- reading the file -------------------------------------------------------


def test_a_pair_is_read():
    aliases = parse("- BWI Company -> BWI Companies\n")

    assert aliases.pairs == (Alias("BWI Company", "BWI Companies"),)
    assert aliases.canonical_for("BWI Company") == "BWI Companies"


def test_a_name_with_no_alias_comes_back_unchanged():
    aliases = parse("- BWI Company -> BWI Companies\n")

    assert aliases.canonical_for("Illes Foods") == "Illes Foods"


def test_matching_ignores_case_and_surrounding_space():
    aliases = parse("-   bwi company   ->   BWI Companies  \n")

    assert aliases.canonical_for("  BWI COMPANY  ") == "BWI Companies"


def test_an_arrow_typed_as_an_arrow_works():
    """The file is hand edited, and an editor may well substitute one."""
    aliases = parse("- TST Impreso → TST Impresso\n")

    assert aliases.canonical_for("TST Impreso") == "TST Impresso"


def test_an_indented_example_is_not_a_pair():
    """This was a real bug and it hid itself.

    The parser stripped each line before looking for a list item, so the
    indented example in the file's own header parsed as a real alias. Every
    file `ranger alias add` wrote carried a phantom BWI Company -> BWI
    Companies, which is one of the operator's genuine pairs, so it looked
    correct — and `ranger alias remove BWI Company` reported success and
    changed nothing, because the header put it straight back.
    """
    aliases = parse(
        "# Aliases\n"
        "\n"
        "The left name is folded into the right one. For example:\n"
        "\n"
        "    - BWI Company -> BWI Companies\n"
        "\n"
        "- Vytalogy -> Vitalogy\n"
    )

    assert [(a.variant, a.canonical) for a in aliases.pairs] == [("Vytalogy", "Vitalogy")]


def test_the_written_header_does_not_smuggle_in_an_alias():
    """The header is prose, and prose must parse to nothing."""
    from ranger.aliases import HEADER

    assert parse(HEADER + "- Vytalogy -> Vitalogy\n").pairs == (
        Alias("Vytalogy", "Vitalogy"),
    )


def test_a_note_after_a_hash_is_not_part_of_the_name():
    aliases = parse("- Vytalogy -> Vitalogy  # the CRM spells it both ways\n")

    assert aliases.canonical_for("Vytalogy") == "Vitalogy"


def test_a_line_mapping_a_name_to_itself_is_ignored():
    """Harmless in itself, but it would make variants_of report a name as its
    own variant and double it wherever the halves are added together."""
    assert parse("- Illes Foods -> Illes Foods\n").empty


def test_a_line_that_is_not_a_pair_is_skipped_rather_than_raising():
    aliases = parse("- just a bullet point\n- BWI Company -> BWI Companies\n")

    assert len(aliases.pairs) == 1


def test_a_canonical_name_that_is_not_an_account_is_called_out(capsys):
    """The failure this exists to prevent is silent.

    After a CRM refresh a canonical name can stop existing. Dropping the alias
    quietly splits the account back into two, and the only symptom is that
    answers get worse.
    """
    aliases = parse(
        "- BWI Company -> BWI Companies\n",
        known=["Illes Foods", "Vitalogy"],
    )

    assert aliases.empty
    assert len(aliases.stale) == 1
    assert "BWI Companies" in aliases.stale[0]


def test_nothing_is_stale_when_the_account_list_is_not_known():
    """Callers that have no list must not have every alias reported stale."""
    aliases = parse("- BWI Company -> BWI Companies\n")

    assert aliases.stale == ()
    assert not aliases.empty


def test_variants_of_finds_every_name_folded_into_one():
    aliases = parse(
        "- BWI Company -> BWI Companies\n- B.W.I. Companies -> BWI Companies\n"
    )

    assert set(aliases.variants_of("BWI Companies")) == {"BWI Company", "B.W.I. Companies"}


def test_what_is_written_can_be_read_back():
    pairs = [Alias("BWI Company", "BWI Companies"), Alias("Vytalogy", "Vitalogy")]

    assert parse(render(pairs)).pairs == tuple(sorted(pairs, key=lambda a: a.variant))


# -- folding the scans together ---------------------------------------------


@dataclass(frozen=True)
class Scan:
    """Stands in for accounts.NoteScan. Only the fields fold touches."""

    name: str
    path: Path = Path("x.md")
    status: str = ""
    last_activity: date | None = None
    activity_count: int = 0
    tier: str = ""
    opportunity_stages: tuple[str, ...] = ()


def test_folding_adds_the_halves_together():
    scans = [
        Scan("BWI Companies", last_activity=date(2026, 1, 10), activity_count=4),
        Scan("BWI Company", last_activity=date(2026, 3, 2), activity_count=3),
    ]

    folded = fold(scans, parse("- BWI Company -> BWI Companies\n"))

    assert len(folded) == 1
    assert folded[0].name == "BWI Companies"
    assert folded[0].activity_count == 7


def test_the_newest_activity_wins():
    """This is the whole point for the quiet check. An account worked last week
    under one name is not quiet because the other name has been silent a year.
    """
    scans = [
        Scan("BWI Companies", last_activity=date(2025, 4, 1), activity_count=1),
        Scan("BWI Company", last_activity=date(2026, 3, 2), activity_count=1),
    ]

    folded = fold(scans, parse("- BWI Company -> BWI Companies\n"))

    assert folded[0].last_activity == date(2026, 3, 2)


def test_the_canonical_notes_tier_wins_over_a_variants():
    """The tier ranks the morning brief. Taking it from whichever half was
    scanned first would make the brief's order depend on directory order.
    """
    scans = [
        Scan("BWI Company", tier="3"),
        Scan("BWI Companies", tier="1"),
    ]

    folded = fold(scans, parse("- BWI Company -> BWI Companies\n"))

    assert folded[0].tier == "1"


def test_a_variant_with_no_tier_borrows_the_others():
    scans = [Scan("BWI Companies", tier=""), Scan("BWI Company", tier="2")]

    folded = fold(scans, parse("- BWI Company -> BWI Companies\n"))

    assert folded[0].tier == "2"


def test_opportunities_from_both_halves_survive():
    scans = [
        Scan("BWI Companies", opportunity_stages=("Proposal",)),
        Scan("BWI Company", opportunity_stages=("Closed Won",)),
    ]

    folded = fold(scans, parse("- BWI Company -> BWI Companies\n"))

    assert set(folded[0].opportunity_stages) == {"Proposal", "Closed Won"}


def test_accounts_with_no_alias_pass_through_untouched():
    scans = [Scan("Illes Foods", activity_count=2), Scan("Gabriel Ranch Beef")]

    folded = fold(scans, parse("- BWI Company -> BWI Companies\n"))

    assert [scan.name for scan in folded] == ["Illes Foods", "Gabriel Ranch Beef"]


def test_an_empty_map_changes_nothing_at_all():
    scans = [Scan("Illes Foods"), Scan("BWI Companies")]

    assert fold(scans, Aliases()) is scans


def test_a_variant_whose_canonical_note_is_gone_keeps_itself():
    """Folding into a note that does not exist would delete the account from
    every list it appears in, which is worse than not folding.
    """
    scans = [Scan("BWI Company", activity_count=3)]

    folded = fold(scans, parse("- BWI Company -> BWI Companies\n"))

    assert [scan.name for scan in folded] == ["BWI Company"]
    assert folded[0].activity_count == 3


def test_three_names_fold_into_one():
    scans = [
        Scan("BWI Companies", activity_count=1),
        Scan("BWI Company", activity_count=2),
        Scan("B.W.I. Companies", activity_count=4),
    ]

    folded = fold(
        scans,
        parse("- BWI Company -> BWI Companies\n- B.W.I. Companies -> BWI Companies\n"),
    )

    assert len(folded) == 1
    assert folded[0].activity_count == 7


def test_folding_does_not_double_an_account_counted_twice():
    """The canonical scan is the seed for the merged record. Adding it again as
    a member would double its activity, and the number is what the brief ranks
    on, so it would silently reorder the morning list.
    """
    scans = [Scan("BWI Companies", activity_count=5), Scan("BWI Company", activity_count=1)]

    folded = fold(scans, parse("- BWI Company -> BWI Companies\n"))

    assert folded[0].activity_count == 6


# -- suggesting, and refusing to suggest ------------------------------------


def _pairs(names, **kwargs):
    return {(s.left, s.right) for s in suggest(names, **kwargs)}


def test_it_finds_every_real_pair():
    """The six the operator confirmed, in one list with plenty of distractors."""
    names = [
        "BWI Companies", "BWI Company",
        "TST Impreso", "TST Impresso",
        "Vytalogy", "Vitalogy",
        "Gabriel Ranch Beef", "Gabriel Beef Ranch",
        "Reily Foods Company", "Reily Foods",
        "Del Monte Foods Inc", "Del Monte Foods",
        "Illes Foods", "Rusty's BBQ", "Pegasus Logistics",
    ]

    found = _pairs(names)

    assert ("BWI Companies", "BWI Company") in found
    assert ("TST Impreso", "TST Impresso") in found
    assert ("Vitalogy", "Vytalogy") in found
    assert ("Gabriel Beef Ranch", "Gabriel Ranch Beef") in found
    assert ("Reily Foods", "Reily Foods Company") in found
    assert ("Del Monte Foods", "Del Monte Foods Inc") in found


def test_a_company_suffix_is_not_identity():
    assert _pairs(["Reily Foods", "Reily Foods Company"]) == {
        ("Reily Foods", "Reily Foods Company")
    }


def test_reordered_words_are_caught():
    """String similarity misses these badly: the same words in a different
    order score low as strings, which is why word-order is its own signal.
    """
    assert _pairs(["Gabriel Ranch Beef", "Gabriel Beef Ranch"])


def test_one_letter_apart_is_caught():
    assert _pairs(["TST Impreso", "TST Impresso"])
    assert _pairs(["Vytalogy", "Vitalogy"])


def test_two_real_companies_sharing_a_word_are_not_paired():
    """The operator named this one. Pegasus Logistics and Pegasus Packaging are
    two customers, and merging them would put one company's pricing in the
    other's history.
    """
    assert _pairs(["Pegasus Logistics", "Pegasus Packaging"]) == set()


def test_unrelated_accounts_are_not_paired():
    names = ["Illes Foods", "Rusty's BBQ", "Gabriel Ranch Beef", "Vitalogy"]

    assert _pairs(names) == set()


def test_a_name_that_is_nothing_but_suffixes_pairs_with_nobody():
    """'The Company' shapes down to an empty string, and every empty shape
    would otherwise match every other empty shape.
    """
    assert _pairs(["The Company", "Inc", "Illes Foods"]) == set()


def test_the_strongest_signal_is_reported_first():
    names = ["Reily Foods", "Reily Foods Company", "TST Impreso", "TST Impresso"]

    scores = [s.score for s in suggest(names)]

    assert scores == sorted(scores, reverse=True)
    assert suggest(names)[0].score == 1.0


def test_each_pair_says_why():
    reasons = {s.why for s in suggest(["Reily Foods", "Reily Foods Company"])}

    assert reasons == {"the same once Inc and Co are removed"}


def test_a_pair_is_suggested_once_not_twice():
    assert len(suggest(["BWI Company", "BWI Companies"])) == 1


def test_the_same_name_twice_is_not_a_pair():
    assert suggest(["Illes Foods", "Illes Foods"]) == []


def test_raising_the_cutoff_drops_the_spelling_signal_only():
    """The suffix and word-order signals are exact and must not be tunable
    away: a cutoff that silenced them would hide the pairs the operator has
    already confirmed are real.
    """
    names = ["TST Impreso", "TST Impresso", "Reily Foods", "Reily Foods Company"]

    strict = _pairs(names, cutoff=0.99)

    assert ("TST Impreso", "TST Impresso") not in strict
    assert ("Reily Foods", "Reily Foods Company") in strict


# -- the file on disk -------------------------------------------------------


@pytest.fixture
def store(vault, config):
    return AliasFile(vault, config.vault.ranger)


def test_a_missing_file_reads_as_no_aliases(store):
    """Not an error. Most vaults will never have one."""
    assert store.load().empty


def test_adding_a_pair_writes_a_file_that_reads_back(store):
    assert store.add("BWI Company", "BWI Companies") is True

    assert store.load().canonical_for("BWI Company") == "BWI Companies"


def test_the_written_file_explains_itself(store):
    """It is hand edited, and the reader will not have this conversation."""
    store.add("BWI Company", "BWI Companies")

    text = store.path.read_text(encoding="utf-8")

    assert "build_vault.py" in text
    assert "- BWI Company -> BWI Companies" in text


def test_a_variant_cannot_be_mapped_to_two_places(store):
    """Silently rewriting the first mapping would move an account's history
    without saying so.
    """
    store.add("BWI Company", "BWI Companies")

    assert store.add("BWI Company", "Illes Foods") is False
    assert store.load().canonical_for("BWI Company") == "BWI Companies"


def test_removing_a_pair_leaves_the_others(store):
    store.add("BWI Company", "BWI Companies")
    store.add("Vytalogy", "Vitalogy")

    assert store.remove("BWI Company") is True

    remaining = store.load()
    assert remaining.canonical_for("BWI Company") == "BWI Company"
    assert remaining.canonical_for("Vytalogy") == "Vitalogy"


def test_removing_something_that_is_not_there_says_so(store):
    assert store.remove("Illes Foods") is False


def test_adding_and_removing_leaves_the_file_readable(store):
    store.add("BWI Company", "BWI Companies")
    store.remove("BWI Company")

    assert store.load().empty
    assert "# Aliases" in store.path.read_text(encoding="utf-8")
