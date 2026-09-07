"""Tier 7c: the activity panel.

The panel is a view of the vault, so these tests are mostly about the two
places a browser can reach the disk: what it is shown, and what a click can
change. Dismissing is the only write, and the id it acts on came from outside,
so it goes through the same wall as everything else.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ranger.heartbeat import Inbox, Notice
from ranger.panel import AWAITING_KIND, dismiss, snapshot


@pytest.fixture
def seeded(config, vault):
    inbox = Inbox(vault, config.vault.inbox)
    inbox.write(
        Notice(
            kind="morning",
            title="What went quiet, 7 September",
            body="58 accounts checked, 12 still active.",
            created=datetime(2026, 9, 7, 7, 0),
        )
    )
    inbox.write(
        Notice(
            kind=AWAITING_KIND,
            title="Waiting on you: Permanently remove a fact",
            body="Started from: voice.",
            created=datetime(2026, 9, 7, 7, 41),
        )
    )
    (config.vault.drafts / "2026-09-05-follow-up.md").write_text(
        "---\ncreated: 2026-09-05\ntitle: Follow up with Steffen\nstatus: draft, not sent\n---\n"
        "Checking in on the numbers.\n",
        encoding="utf-8",
    )
    return config, vault


def test_the_panel_shows_the_folders_it_claims_to_show(seeded):
    config, vault = seeded
    view = snapshot(config, vault)

    assert [item["title"] for item in view["inbox"]] == ["What went quiet, 7 September"]
    assert [item["title"] for item in view["drafts"]] == ["Follow up with Steffen"]
    assert len(view["awaiting"]) == 1


def test_held_confirmations_are_their_own_section(seeded):
    """A notice HoldingGate wrote from a voice turn is not ordinary inbox."""
    config, vault = seeded
    view = snapshot(config, vault)
    assert "Waiting on you" in view["awaiting"][0]["title"]
    assert all("Waiting on you" not in item["title"] for item in view["inbox"])


def test_a_dismissed_notice_leaves_the_panel_and_stays_on_disk(seeded):
    """Cleared in the vault, not just on screen. And nothing is deleted."""
    config, vault = seeded
    before = snapshot(config, vault)
    target = before["inbox"][0]["id"]

    assert dismiss(config, vault, target) == "What went quiet, 7 September"

    after = snapshot(config, vault)
    assert after["inbox"] == []

    path = config.vault.root / target
    assert path.is_file(), "dismissing must not delete the note"
    assert "status: dismissed" in path.read_text(encoding="utf-8")


def test_dismissing_the_same_notice_twice_is_not_an_error_the_second_time(seeded):
    config, vault = seeded
    target = snapshot(config, vault)["inbox"][0]["id"]
    assert dismiss(config, vault, target)
    assert dismiss(config, vault, target) is None


@pytest.mark.parametrize(
    "attempt",
    [
        "Ranger/memory/facts.md",
        "Ranger/log/2026-09-07.md",
        "Accounts/Illes Foods.md",
        "../outside.md",
        "Ranger/inbox/../memory/facts.md",
        "/etc/passwd",
        "C:/Windows/System32/drivers/etc/hosts",
        "",
    ],
)
def test_dismiss_refuses_anything_that_is_not_a_notice_in_the_inbox(seeded, attempt):
    """The id came from a browser, so it is treated as a string from outside.

    Amendment D says the wall is a path check in code, not a rule in a prompt.
    This is the point where a string from outside becomes a path.
    """
    config, vault = seeded
    (config.vault.memory / "facts.md").write_text("- 2026-09-01 | keep me\n", encoding="utf-8")

    assert dismiss(config, vault, attempt) is None
    assert "keep me" in (config.vault.memory / "facts.md").read_text(encoding="utf-8")


def test_an_empty_vault_gives_an_empty_panel_rather_than_an_error(config, vault):
    assert snapshot(config, vault) == {"inbox": [], "drafts": [], "awaiting": []}


def test_a_draft_with_no_title_falls_back_to_its_filename(config, vault):
    (config.vault.drafts / "2026-09-05-no-front-matter.md").write_text(
        "just a body\n", encoding="utf-8"
    )
    view = snapshot(config, vault)
    assert view["drafts"][0]["title"] == "2026-09-05-no-front-matter"


def test_previews_are_bounded(config, vault):
    from ranger.panel import PREVIEW_CHARS

    Inbox(vault, config.vault.inbox).write(
        Notice(kind="morning", title="Long", body="word " * 400, created=datetime(2026, 9, 7))
    )
    detail = snapshot(config, vault)["inbox"][0]["detail"]
    assert len(detail) <= PREVIEW_CHARS + 3
