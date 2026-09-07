"""Tier 5: the loop that acts without being spoken to."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest

from ranger.heartbeat import (
    STATUS_DISMISSED,
    Heartbeat,
    Inbox,
    MorningSurface,
    Notice,
    build_checks,
    parse_notice,
)
from ranger.toolset import build_registry
from ranger.vault import Vault

FIXTURES = Path(__file__).parent / "fixtures" / "vault"


@pytest.fixture
def inbox(vault, config):
    return Inbox(vault, config.vault.inbox)


@pytest.fixture
def seeded(config, vault_root):
    for name in ("Accounts", "Knowledge"):
        shutil.rmtree(vault_root / name, ignore_errors=True)
        shutil.copytree(FIXTURES / name, vault_root / name)
    return config


def note(kind="morning", when=None, status="new", title="A notice", body="Something."):
    return Notice(kind=kind, title=title, body=body,
                  created=when or datetime(2026, 9, 7, 7, 0), status=status)


class FakeCheck:
    def __init__(self, name="fake", notice=None, quiet_ok=False, delay=0.0, boom=None):
        self.name = name
        self.runs_in_quiet_hours = quiet_ok
        self.notice = notice
        self.delay = delay
        self.boom = boom
        self.runs = 0
        self.due_always = True

    def status(self, now, inbox):
        from ranger.heartbeat import Dueness

        return Dueness(self.due_always, "due" if self.due_always else "not due yet")

    async def run(self):
        self.runs += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.boom:
            raise RuntimeError(self.boom)
        return self.notice


def beat(config, checks, when=datetime(2026, 9, 7, 9, 0)):
    return Heartbeat(config, Inbox(Vault(config.vault), config.vault.inbox), checks,
                     now=lambda: when)


# -- notices on disk -------------------------------------------------------


def test_a_notice_round_trips(inbox):
    path = inbox.write(note(title="What went quiet", body="Rusty Supply Co: 180 days"))
    back = parse_notice(path.read_text(encoding="utf-8"), path)
    assert back.title == "What went quiet"
    assert "Rusty Supply Co: 180 days" in back.body
    assert back.kind == "morning" and back.status == "new"


def test_notices_are_markdown_a_person_reads_in_obsidian(inbox):
    path = inbox.write(note())
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n") and "# A notice" in text
    assert path.suffix == ".md"
    assert path.name == "2026-09-07 morning.md"


def test_dismissing_clears_it_in_the_vault_not_just_on_screen(inbox):
    inbox.write(note())
    pending = inbox.pending()
    assert len(pending) == 1

    path = inbox.dismiss(pending[0])
    assert "status: dismissed" in path.read_text(encoding="utf-8")
    assert inbox.pending() == []
    # Nothing is deleted: the vault has no delete path and is not getting one.
    assert path.exists()
    assert len(inbox.all()) == 1


def test_a_dismissed_notice_stays_dismissed_across_a_restart(config):
    first = Inbox(Vault(config.vault), config.vault.inbox)
    first.write(note())
    first.dismiss(first.pending()[0])

    second = Inbox(Vault(config.vault), config.vault.inbox)
    assert second.pending() == []


def test_two_notices_of_the_same_kind_on_one_day_do_not_collide(inbox):
    inbox.write(note())
    inbox.write(note(title="Another"))
    assert len(inbox.all()) == 2


def test_a_file_that_is_not_a_notice_is_ignored(inbox, config):
    (config.vault.inbox / "scratch.md").write_text("just some notes", encoding="utf-8")
    inbox.write(note())
    assert len(inbox.all()) == 1


# -- the inbox is the schedule --------------------------------------------


def test_nothing_is_dropped_that_the_operator_did_not_see(inbox):
    """Catch-up-on-return, never deliver-once-and-lose-it."""
    inbox.write(note())
    for _ in range(5):
        assert len(inbox.pending()) == 1     # still there, however often nobody looks


def test_the_morning_check_is_due_after_its_hour(inbox):
    check = MorningSurface(registry=None, hour=7)
    assert not check.due(datetime(2026, 9, 7, 6, 59), inbox)
    assert check.due(datetime(2026, 9, 7, 7, 0), inbox)


def test_a_laptop_asleep_at_seven_catches_up_when_it_wakes(inbox):
    """The case the operator asked about by name."""
    check = MorningSurface(registry=None, hour=7)
    assert check.due(datetime(2026, 9, 7, 14, 30), inbox)


def test_it_does_not_fire_twice_in_a_day(inbox):
    check = MorningSurface(registry=None, hour=7)
    inbox.write(note(kind="morning", when=datetime(2026, 9, 7, 7, 1)))
    assert not check.due(datetime(2026, 9, 7, 9, 0), inbox)
    assert check.due(datetime(2026, 9, 8, 7, 0), inbox)


def test_a_week_away_does_not_replay_six_mornings(inbox):
    """Only today is considered. Old news is not news."""
    check = MorningSurface(registry=None, hour=7)
    assert check.due(datetime(2026, 9, 14, 8, 0), inbox)

    inbox.write(note(kind="morning", when=datetime(2026, 9, 14, 8, 0)))
    assert not check.due(datetime(2026, 9, 14, 9, 0), inbox)


def test_the_schedule_survives_a_restart_without_a_state_file(config):
    """There is no state file to lose: the vault answers the question."""
    check = MorningSurface(registry=None, hour=7)
    first = Inbox(Vault(config.vault), config.vault.inbox)
    first.write(note(kind="morning", when=datetime(2026, 9, 7, 7, 0)))

    fresh = Inbox(Vault(config.vault), config.vault.inbox)
    assert not check.due(datetime(2026, 9, 7, 10, 0), fresh)
    assert not any(p.suffix == ".json" for p in config.vault.ranger.rglob("*"))


# -- the loop --------------------------------------------------------------


async def test_a_due_check_surfaces_a_notice(config):
    check = FakeCheck(notice=note())
    report = await beat(config, [check]).tick()
    assert report.surfaced == ("fake",) and check.runs == 1


async def test_a_check_with_nothing_to_say_surfaces_nothing(config):
    """Quiet by default: most checks produce nothing most days."""
    check = FakeCheck(notice=None)
    report = await beat(config, [check]).tick()
    assert report.ran == ("fake",) and report.surfaced == ()
    assert Inbox(Vault(config.vault), config.vault.inbox).pending() == []


async def test_quiet_hours_hold_a_check_rather_than_dropping_it(config):
    check = FakeCheck(notice=note())
    heart = beat(config, [check], when=datetime(2026, 9, 7, 23, 0))
    report = await heart.tick()

    assert report.skipped_quiet == ("fake",) and check.runs == 0
    # Still due, so it fires once the window ends.
    heart.now = lambda: datetime(2026, 9, 8, 7, 0)
    assert (await heart.tick()).surfaced == ("fake",)


async def test_something_urgent_may_run_in_quiet_hours(config):
    check = FakeCheck(notice=note(), quiet_ok=True)
    report = await beat(config, [check], when=datetime(2026, 9, 7, 23, 0)).tick()
    assert report.surfaced == ("fake",)


async def test_a_slow_check_does_not_stack_up_behind_itself(config):
    check = FakeCheck(notice=note(), delay=0.2)
    heart = beat(config, [check])

    first = asyncio.create_task(heart.tick())
    await asyncio.sleep(0.05)
    second = await heart.tick()
    await first

    assert second.skipped_running == ("fake",)
    assert check.runs == 1


async def test_a_check_that_hangs_times_out_and_leaves_a_note(config):
    """Never block forever waiting on a person who is asleep."""
    tight = replace(config, heartbeat=replace(config.heartbeat, check_timeout_seconds=1))
    check = FakeCheck(notice=note(), delay=30)
    report = await beat(tight, [check]).tick()

    assert report.timed_out == ("fake",)
    pending = Inbox(Vault(config.vault), config.vault.inbox).pending()
    assert len(pending) == 1 and "timed out" in pending[0].title
    assert "Nothing was changed" in pending[0].body


async def test_a_check_that_raises_does_not_stop_the_loop(config):
    boom = FakeCheck(name="boom", boom="everything broke")
    fine = FakeCheck(name="fine", notice=note(kind="fine"))
    report = await beat(config, [boom, fine]).tick()

    assert set(report.ran) == {"boom", "fine"}
    titles = [n.title for n in Inbox(Vault(config.vault), config.vault.inbox).pending()]
    assert any("failed" in t for t in titles)


# -- the morning surface uses the existing tool ---------------------------


async def test_the_morning_surface_calls_what_went_quiet(seeded):
    """No second implementation of that logic, so they cannot disagree."""
    registry = build_registry(seeded, Vault(seeded.vault))
    check = MorningSurface(registry=registry, hour=7)
    notice = await check.run()

    assert notice is not None
    assert "Rusty Supply Co: 180 days" in notice.body
    assert "Never had any activity" in notice.body


async def test_the_morning_surface_reports_a_tool_failure_rather_than_crashing(config):
    class Broken:
        async def run(self, name, payload):
            from ranger.tools import ToolResult

            return ToolResult(False, "the vault could not be read", "failed")

    notice = await MorningSurface(registry=Broken(), hour=7).run()
    assert notice is not None and "could not run" in notice.title


def test_tier_five_registers_exactly_one_check(config):
    checks = build_checks(config, None)
    assert [c.name for c in checks] == ["morning"]
    assert not checks[0].runs_in_quiet_hours
    assert checks[0].hour == config.schedule.morning_hour


# -- every outcome says which one it was -----------------------------------
#
# "nothing due" said the same thing whether a check was not due yet,
# suppressed by quiet hours, or broken. In the tier whose point is acting while
# nobody watches, that is the silence-reads-as-broken failure again.


def test_before_its_hour_says_when_it_is_due(inbox):
    dueness = MorningSurface(registry=None, hour=7).status(datetime(2026, 9, 7, 2, 40), inbox)
    assert not dueness.due
    assert dueness.reason == "not due until 07:00 today"


def test_after_it_has_run_says_so_and_when_it_is_next(inbox):
    inbox.write(note(kind="morning", when=datetime(2026, 9, 7, 7, 1)))
    dueness = MorningSurface(registry=None, hour=7).status(datetime(2026, 9, 7, 9, 0), inbox)
    assert not dueness.due
    assert "already ran today" in dueness.reason and "tomorrow" in dueness.reason


async def test_quiet_hours_says_when_the_window_ends(config):
    check = FakeCheck(notice=note())
    report = await beat(config, [check], when=datetime(2026, 9, 7, 19, 30)).tick()
    outcome = report.outcomes[0]
    assert outcome.state == "held"
    assert "will run after 06:00" in outcome.detail


async def test_a_surfaced_notice_names_the_file(config):
    report = await beat(config, [FakeCheck(notice=note())]).tick()
    assert report.outcomes[0].detail == "surfaced to 2026-09-07 morning.md"


async def test_a_check_with_nothing_to_say_says_that_too(config):
    report = await beat(config, [FakeCheck(notice=None)]).tick()
    assert report.outcomes[0].state == "nothing"
    assert "nothing worth surfacing" in report.outcomes[0].detail


async def test_a_failure_carries_the_error_into_the_outcome(config):
    report = await beat(config, [FakeCheck(boom="the vault vanished")]).tick()
    assert report.outcomes[0].state == "failed"
    assert "the vault vanished" in report.outcomes[0].detail


async def test_a_timeout_says_nothing_was_changed(config):
    tight = replace(config, heartbeat=replace(config.heartbeat, check_timeout_seconds=1))
    report = await beat(tight, [FakeCheck(notice=note(), delay=30)]).tick()
    assert report.outcomes[0].state == "timed_out"
    assert "nothing was changed" in report.outcomes[0].detail


# -- forcing ---------------------------------------------------------------


async def test_force_runs_a_check_that_is_not_due(config):
    """Verifying a daily check should not mean waiting until tomorrow."""
    check = FakeCheck(notice=note())
    check.due_always = False
    report = await beat(config, [check]).tick(force=("fake",))
    assert report.surfaced == ("fake",) and check.runs == 1


async def test_force_ignores_quiet_hours(config):
    check = FakeCheck(notice=note())
    report = await beat(config, [check], when=datetime(2026, 9, 7, 2, 40)).tick(force=("fake",))
    assert report.surfaced == ("fake",)


async def test_force_runs_the_morning_check_that_already_ran_today(seeded):
    """So the write, read and dismiss path can be exercised the same day."""
    inbox = Inbox(Vault(seeded.vault), seeded.vault.inbox)
    heart = Heartbeat(seeded, inbox, build_checks(seeded, build_registry(seeded, Vault(seeded.vault))),
                      now=lambda: datetime(2026, 9, 7, 9, 0))

    assert (await heart.tick()).surfaced == ("morning",)
    assert (await heart.tick()).surfaced == ()          # already ran
    assert (await heart.tick(force=("morning",))).surfaced == ("morning",)

    notices = inbox.pending()
    assert len(notices) == 2
    assert {n.path.name for n in notices} == {
        "2026-09-07 morning.md",
        "2026-09-07 morning 2.md",
    }


async def test_force_all_runs_everything(config):
    one, two = FakeCheck(name="one", notice=note(kind="one")), FakeCheck(name="two", notice=note(kind="two"))
    for check in (one, two):
        check.due_always = False
    report = await beat(config, [one, two]).tick(force=("all",))
    assert set(report.surfaced) == {"one", "two"}


async def test_force_does_not_run_a_check_that_is_already_running(config):
    """Forcing overrides the schedule, not the no-stacking rule."""
    check = FakeCheck(notice=note(), delay=0.2)
    heart = beat(config, [check])
    first = asyncio.create_task(heart.tick())
    await asyncio.sleep(0.05)
    second = await heart.tick(force=("fake",))
    await first
    assert second.skipped_running == ("fake",) and check.runs == 1
