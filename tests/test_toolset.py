"""The three Tier 2 tools, end to end against the fixture vault."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from ranger.toolset import build_registry
from ranger.vault import Vault

FIXTURES = Path(__file__).parent / "fixtures" / "vault"
TODAY = date(2026, 9, 7)


@pytest.fixture
def seeded(vault_root, config):
    """The fixture vault copied into a temp vault, so tests can write to it."""
    for name in ("Accounts", "Knowledge"):
        shutil.rmtree(vault_root / name, ignore_errors=True)
        shutil.copytree(FIXTURES / name, vault_root / name)
    shutil.copy(FIXTURES / "_vault-build-report.md", vault_root / "_vault-build-report.md")
    return config


@pytest.fixture
def registry(seeded):
    return build_registry(seeded, Vault(seeded.vault), today=lambda: TODAY)


async def run(registry, name, **payload):
    return await registry.run(name, payload)


def test_the_registry_holds_exactly_the_intended_tools(registry):
    """Tier 2's three, Tier 4's two, and the account write path's one. A
    seventh is a scope decision."""
    assert registry.names() == [
        "account_recall",
        "draft_and_hold",
        "file_to_account",
        "forget",
        "remember",
        "what_went_quiet",
    ]


def test_every_tool_has_a_description_a_model_can_act_on(registry):
    for tool in registry:
        assert len(tool.description) > 80
        assert tool.input_schema["type"] == "object"


def test_only_forget_needs_the_confirmation_gate(registry):
    """Nothing here sends or spends. forget rewrites a file, which does not
    happen without the operator's yes.

    file_to_account writes and deliberately does not gate: filing into an
    account that already exists happens often enough that a card would become a
    reflex within a week, and a card clicked without reading manufactures a
    record of review that did not happen. What makes it safe instead is that it
    can only add, only below the marker, and only to a note that already exists.
    """
    assert [t.name for t in registry if t.confirm] == ["forget"]
    assert sorted(t.name for t in registry if t.writes) == [
        "draft_and_hold",
        "file_to_account",
        "forget",
        "remember",
    ]


# -- account recall --------------------------------------------------------


async def test_recall_answers_from_a_spoken_fragment(registry):
    result = await run(registry, "account_recall", account="Illes")
    assert result.ok
    assert "Illes Foods" in result.content
    assert "retort line" in result.content
    assert "2026-09-04 | Call | Rod Illes" in result.content


async def test_recall_returns_a_digest_not_the_note(registry, seeded):
    note_size = (seeded.vault.accounts / "Illes Foods.md").stat().st_size
    result = await run(registry, "account_recall", account="Illes")
    assert "digest of a" in result.content
    # The oldest activity heading is trimmed. The date itself still appears in
    # the "6 logged activities, <first> to <last>" line, which is the point of
    # that line, so this asserts on the heading rather than the date.
    assert "### 2026-06-18" not in result.content
    assert "### 2026-09-04" in result.content
    assert len(result.content) < note_size + 1200   # fencing and the note about size


async def test_recall_fences_the_note_as_data(registry):
    """A note holds pasted customer email. It is data, never an instruction."""
    result = await run(registry, "account_recall", account="Illes")
    assert "<untrusted_content" in result.content
    assert "Accounts/Illes Foods.md" in result.content


async def test_recall_asks_rather_than_guessing(registry):
    result = await run(registry, "account_recall", account="Pegasus")
    assert result.ok
    assert "Pegasus Logistics" in result.content and "Pegasus Packaging" in result.content
    assert "do not pick one yourself" in result.content
    assert "needs the operator" in result.summary


async def test_recall_on_an_unknown_account(registry):
    result = await run(registry, "account_recall", account="Wingfield Aerospace")
    assert "No account note matches" in result.content
    assert result.summary == "no match"


async def test_recall_detail_shows_more_history(registry):
    brief = await run(registry, "account_recall", account="Illes")
    detail = await run(registry, "account_recall", account="Illes", detail=True)

    # Counted, not measured. With only six fixture activities, detail adds one
    # entry and drops the "older activities not shown" trailer, so it can come
    # out marginally shorter while still holding strictly more history.
    assert detail.content.count("### 2026-") > brief.content.count("### 2026-")
    assert "### 2026-06-18" in detail.content
    assert "older activities not shown" not in detail.content


async def test_recall_never_reads_the_build_report(registry):
    result = await run(registry, "account_recall", account="vault-build-report")
    assert result.summary == "no match"


async def test_recall_with_no_account_named(registry):
    result = await run(registry, "account_recall", account="  ")
    assert not result.ok


# -- what went quiet -------------------------------------------------------


async def test_quiet_groups_lapsed_and_never_touched_separately(registry):
    """full=true is the whole list, longest first. The default is the brief."""
    result = await run(registry, "what_went_quiet", full=True)
    assert result.ok

    lapsed_at = result.content.index("Gone quiet")
    never_at = result.content.index("Never had any activity")
    assert lapsed_at < never_at

    lapsed_block = result.content[lapsed_at:never_at]
    assert "Rusty Supply Co: 180 days" in lapsed_block
    assert "Pegasus Logistics: 97 days" in lapsed_block
    assert "Northwind Provisions" not in lapsed_block
    assert "Northwind Provisions" in result.content[never_at:]


async def test_quiet_excludes_the_build_report(registry):
    result = await run(registry, "what_went_quiet", full=True)
    assert "vault-build-report" not in result.content
    assert "5 accounts checked" in result.content


async def test_quiet_sets_unconfirmed_notes_aside(registry):
    result = await run(registry, "what_went_quiet", days=1, full=True)
    assert "Cedar Ridge Dairy" not in result.content
    assert "1 unconfirmed notes were left out" in result.content


async def test_quiet_threshold_can_be_overridden(registry):
    result = await run(registry, "what_went_quiet", days=365, full=True)
    assert "Nothing has gone quiet" in result.content
    assert "Northwind Provisions" in result.content   # still never touched


async def test_quiet_ignores_a_nonsense_threshold(registry):
    result = await run(registry, "what_went_quiet", days="soon")
    assert result.ok and "more than 21 days" in result.content


# -- draft and hold --------------------------------------------------------


async def test_draft_is_written_into_the_drafts_folder(registry, seeded):
    result = await run(
        registry,
        "draft_and_hold",
        title="Follow up to Rusty about the SupplyBox timeline",
        body="Rusty,\n\nChecking in on the timeline. Are you still aiming for October?\n\nChris",
        account="Rusty",
        recipient="Rusty Nakamura",
    )
    assert result.ok
    written = list(seeded.vault.drafts.glob("*.md"))
    assert len(written) == 1

    text = written[0].read_text(encoding="utf-8")
    assert "status: draft, not sent" in text
    assert "account: Rusty Supply Co" in text        # resolved from the fragment
    assert "to: Rusty Nakamura" in text
    assert "Checking in on the timeline" in text
    assert "not been sent" in result.content


async def test_draft_refuses_an_em_dash(registry, seeded):
    result = await run(
        registry,
        "draft_and_hold",
        title="Follow up",
        body="Rusty — quick one about the timeline.",
    )
    assert not result.ok
    assert "em dash" in result.content
    assert list(seeded.vault.drafts.glob("*.md")) == []


async def test_draft_refuses_an_en_dash(registry, seeded):
    result = await run(registry, "draft_and_hold", title="Follow up", body="Two – three weeks.")
    assert not result.ok and "en dash" in result.content


async def test_hyphens_are_left_alone(registry, seeded):
    """follow-up and mid-market are ordinary words."""
    result = await run(
        registry,
        "draft_and_hold",
        title="Follow-up for a mid-market account",
        body="Quick follow-up on the mid-market pricing.",
    )
    assert result.ok


async def test_a_second_draft_never_overwrites_the_first(registry, seeded):
    for _ in range(3):
        await run(registry, "draft_and_hold", title="Same subject", body="Body text.")
    written = sorted(p.name for p in seeded.vault.drafts.glob("*.md"))
    assert len(written) == 3
    assert len(set(written)) == 3


async def test_draft_cannot_escape_the_drafts_folder(registry, seeded):
    result = await run(
        registry,
        "draft_and_hold",
        title="../../Accounts/Illes Foods",
        body="Trying to clobber an account note.",
    )
    # The slug strips the traversal, so this lands in drafts like anything else.
    assert result.ok
    assert (seeded.vault.accounts / "Illes Foods.md").read_text(encoding="utf-8").startswith(
        "- **Status:** Active"
    )
    assert all(p.parent == seeded.vault.drafts for p in seeded.vault.drafts.glob("*.md"))


async def test_draft_needs_a_title_and_a_body(registry):
    assert not (await run(registry, "draft_and_hold", title="", body="x")).ok
    assert not (await run(registry, "draft_and_hold", title="x", body="")).ok
