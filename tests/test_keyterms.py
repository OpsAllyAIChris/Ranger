"""Building the hint list from the vault."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ranger.accounts import NoteScan
from ranger.keyterms import derive_keyterms, distinctive_tokens, score_account, tokens_of

TODAY = date(2026, 9, 7)


def scan(name, days_ago=10, count=5, status="Active"):
    when = None if days_ago is None else date.fromordinal(TODAY.toordinal() - days_ago)
    return NoteScan(name, Path("x"), status, when, count)


# -- which words are worth a slot ------------------------------------------


def test_generic_business_words_are_dropped():
    chosen = distinctive_tokens(["Illes Foods", "Texoma Packaging", "Amcor Flexibles Inc"])
    assert chosen["Illes Foods"] == ["Illes"]
    assert chosen["Texoma Packaging"] == ["Texoma"]
    assert "Inc" not in chosen["Amcor Flexibles Inc"]


def test_a_word_in_many_names_disqualifies_itself():
    """Document frequency does this without anyone maintaining a list."""
    names = ["Acme Converting", "Baker Converting", "Cole Converting"]
    for name, chosen in distinctive_tokens(names).items():
        assert "Converting" not in chosen


def test_short_tokens_are_dropped():
    assert distinctive_tokens(["Wexxar Bel"])["Wexxar Bel"] == ["Wexxar"]


def test_a_name_of_only_common_words_falls_back_to_the_whole_name():
    names = ["Pegasus Packaging", "Pegasus Logistics"]
    chosen = distinctive_tokens(names)
    assert chosen["Pegasus Packaging"] == ["Pegasus Packaging"]
    assert chosen["Pegasus Logistics"] == ["Pegasus Logistics"]


def test_tokens_of_handles_punctuation():
    assert tokens_of("O'Brien & Sons, Inc.") == ["O'Brien", "Sons", "Inc"]


# -- ranking ---------------------------------------------------------------


def test_recent_activity_outranks_old_activity():
    recent, _ = score_account(scan("A", days_ago=3), TODAY)
    stale, _ = score_account(scan("B", days_ago=300), TODAY)
    assert recent > stale


def test_volume_breaks_a_tie_on_recency():
    busy, _ = score_account(scan("A", days_ago=10, count=40), TODAY)
    quiet, _ = score_account(scan("B", days_ago=10, count=1), TODAY)
    assert busy > quiet


def test_an_account_with_no_activity_ranks_last():
    none, reason = score_account(scan("A", days_ago=None, count=0), TODAY)
    ancient, _ = score_account(scan("B", days_ago=2000), TODAY)
    assert none < ancient
    assert "no activity" in reason


# -- the plan --------------------------------------------------------------


def test_config_terms_always_survive_the_cut():
    scans = [scan(f"Company{i} Holdings", days_ago=1, count=50) for i in range(50)]
    plan = derive_keyterms(scans, ("corrugated", "case erector"), cap=5, as_of=TODAY)
    assert plan.terms[:2] == ("corrugated", "case erector")
    assert len(plan.terms) == 5


def test_the_cap_keeps_the_most_recently_worked():
    scans = [
        scan("Wexxar Bel", days_ago=2, count=30),
        scan("Illes Foods", days_ago=5, count=20),
        scan("Texoma Packaging", days_ago=200, count=2),
        scan("Northwind Provisions", days_ago=None, count=0),
    ]
    plan = derive_keyterms(scans, (), cap=2, as_of=TODAY)
    assert plan.terms == ("Wexxar", "Illes")
    assert plan.over_cap
    assert {c.term for c in plan.cut} == {"Texoma", "Northwind", "Provisions"}


def test_nothing_is_cut_when_it_all_fits():
    plan = derive_keyterms([scan("Illes Foods")], ("corrugated",), cap=100, as_of=TODAY)
    assert not plan.over_cap and plan.cut == ()


def test_unconfirmed_notes_do_not_take_slots():
    scans = [scan("Illes Foods"), scan("Cedar Ridge Dairy", status="UNCONFIRMED")]
    plan = derive_keyterms(scans, (), cap=10, as_of=TODAY)
    assert "Cedar" not in plan.terms
    assert plan.skipped_unconfirmed == 1
    assert plan.accounts_considered == 1


def test_duplicates_are_not_hinted_twice():
    scans = [scan("Wexxar Bel"), scan("Wexxar Packaging")]
    plan = derive_keyterms(scans, ("Wexxar",), cap=10, as_of=TODAY)
    assert [t.lower() for t in plan.terms].count("wexxar") == 1


def test_a_cap_of_zero_sends_nothing():
    plan = derive_keyterms([scan("Illes Foods")], ("corrugated",), cap=0, as_of=TODAY)
    assert plan.terms == ()


def test_an_empty_vault_still_returns_the_config_terms():
    plan = derive_keyterms([], ("corrugated",), cap=10, as_of=TODAY)
    assert plan.terms == ("corrugated",)
    assert plan.accounts_considered == 0


def test_the_real_fixture_vault_produces_sensible_hints(config, vault_root):
    import shutil

    from ranger.accounts import scan_all
    from ranger.vault import Vault

    shutil.rmtree(vault_root / "Accounts")
    shutil.copytree(Path(__file__).parent / "fixtures" / "vault" / "Accounts", vault_root / "Accounts")

    scans, errors = scan_all(Vault(config.vault), config.vault.accounts, ("_vault-build-report.md",))
    assert errors == []
    plan = derive_keyterms(scans, config.stt.keyterms, cap=100, as_of=TODAY)

    assert plan.terms[: len(config.stt.keyterms)] == config.stt.keyterms
    assert "Illes" in plan.terms              # deduped into the config terms
    assert "Cedar" not in plan.terms          # UNCONFIRMED, skipped

    # Derived terms come back in recency order. Pegasus Packaging was worked 10
    # days ago, Rusty Supply Co 180, and Northwind has never been touched.
    derived = [c for c in plan.kept if c.source != "ranger.toml"]
    assert [c.term for c in derived[:3]] == [
        "Pegasus Packaging",
        "Pegasus Logistics",
        "Rusty",
    ]
    assert derived[-1].term in ("Northwind", "Provisions")
    assert [c.score for c in derived] == sorted((c.score for c in derived), reverse=True)
