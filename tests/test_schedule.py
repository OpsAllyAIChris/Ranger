"""Running the heartbeat with no terminal open.

Task Scheduler itself cannot be exercised here, so what is tested is the thing
that would be wrong if it were wrong: the task definition. The three settings
that decide whether this works on a laptop all default the wrong way, and two
of them fail silently, so they are asserted rather than trusted.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

import pytest

from ranger.schedule import (
    DEFAULT_INTERVAL_MINUTES,
    TASK_NAME,
    Plan,
    ScheduleError,
    build_plan,
    default_log,
    parse_query,
    to_xml,
)

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
EXE = Path("C:/Users/Chris.Hardwick/Ranger/.venv/Scripts/ranger.exe")


@pytest.fixture
def plan(config):
    return build_plan(config, command=EXE)


def parse(plan: Plan):
    return ElementTree.fromstring(to_xml(plan))


def find(plan: Plan, path: str) -> str:
    node = parse(plan).find(path, NS)
    assert node is not None, f"{path} is missing from the task definition"
    return (node.text or "").strip()


# -- the three that decide whether it works at all -------------------------


def test_a_run_missed_while_asleep_happens_after_the_wake(plan):
    """The sleep and wake question, answered by a setting rather than a guess.

    Without this a 07:00 task on a machine that was asleep at 07:00 is simply
    skipped, and the morning brief silently never happens.
    """
    assert find(plan, "t:Settings/t:StartWhenAvailable") == "true"


def test_it_still_runs_on_battery(plan):
    """Both of these default to true, and both fail silently on a laptop.

    DisallowStartIfOnBatteries means it never starts; StopIfGoingOnBatteries
    means it dies halfway. Neither writes anything anywhere explaining itself.
    """
    assert find(plan, "t:Settings/t:DisallowStartIfOnBatteries") == "false"
    assert find(plan, "t:Settings/t:StopIfGoingOnBatteries") == "false"


def test_it_runs_only_while_the_operator_is_logged_on(plan):
    """As asked. A SYSTEM task would have none of their environment: no .env,
    no HKCU, no mapped drives."""
    assert find(plan, "t:Principals/t:Principal/t:LogonType") == "InteractiveToken"
    assert find(plan, "t:Principals/t:Principal/t:RunLevel") == "LeastPrivilege"


def test_it_does_not_wake_the_machine_up(plan):
    """Surfacing a brief is not worth a laptop spinning up at 07:00 in a bag."""
    assert find(plan, "t:Settings/t:WakeToRun") == "false"


def test_a_slow_run_cannot_stack_up_behind_itself(plan):
    assert find(plan, "t:Settings/t:MultipleInstancesPolicy") == "IgnoreNew"
    assert find(plan, "t:Settings/t:ExecutionTimeLimit") == "PT1H"


def test_it_does_not_wait_for_the_machine_to_be_idle(plan):
    assert find(plan, "t:Settings/t:RunOnlyIfIdle") == "false"
    assert find(plan, "t:Settings/t:IdleSettings/t:StopOnIdleEnd") == "false"


# -- what it actually runs -------------------------------------------------


def test_it_runs_the_executable_the_operator_asked_for(plan):
    assert find(plan, "t:Actions/t:Exec/t:Command") == str(EXE)


def test_the_config_path_is_absolute_and_always_passed(config, plan):
    """A scheduled task can start from anywhere.

    Without an explicit -c, Ranger looks for ranger.toml in the working
    directory and fails with a config error that says nothing about why the
    working directory was C:\\Windows\\System32.
    """
    arguments = find(plan, "t:Actions/t:Exec/t:Arguments")
    assert "-c" in arguments
    assert str(config.source_path.resolve()) in arguments
    assert "heartbeat --once" in arguments


def test_the_output_goes_somewhere_the_operator_can_tail(config, plan):
    """A scheduled task has no terminal, and terminal output is how every
    Windows bug in this build was found."""
    assert str(default_log(config)) in find(plan, "t:Actions/t:Exec/t:Arguments")
    assert default_log(config).parent == config.vault.log


def test_a_path_with_a_space_in_it_is_quoted(config):
    spaced = Path("C:/Users/Chris.Hardwick/Obsidian/Ranger Vault/log/heartbeat.log")
    plan = build_plan(config, command=EXE, log_path=spaced)
    assert f'"{spaced}"' in find(plan, "t:Actions/t:Exec/t:Arguments")


def test_nothing_is_shelled_out_to(plan):
    """No cmd /c. Redirecting through a shell would mean nested quoting inside
    an XML attribute inside a command line, and --log does the same job."""
    arguments = find(plan, "t:Actions/t:Exec/t:Arguments")
    assert "cmd" not in find(plan, "t:Actions/t:Exec/t:Command").lower()
    for shell_ism in (">>", "2>&1", "&&", "|"):
        assert shell_ism not in arguments


# -- the schedule ----------------------------------------------------------


def test_it_checks_often_enough_that_a_missed_hour_is_caught(plan, config):
    """Ranger's own scheduler decides what is due, so this only has to be
    frequent. One source of truth for the morning hour, and it is the inbox."""
    assert find(plan, "t:Triggers/t:CalendarTrigger/t:Repetition/t:Interval") == (
        f"PT{DEFAULT_INTERVAL_MINUTES}M"
    )
    assert find(plan, "t:Triggers/t:CalendarTrigger/t:Repetition/t:Duration") == "P1D"


def test_it_starts_at_the_configured_morning_hour(config):
    from dataclasses import replace

    early = replace(config, schedule=replace(config.schedule, morning_hour=6))
    start = find(build_plan(early, command=EXE), "t:Triggers/t:CalendarTrigger/t:StartBoundary")
    assert start.endswith("T06:00:00")


@pytest.mark.parametrize("minutes", [0, -5, 1441])
def test_a_nonsense_interval_is_refused(config, minutes):
    with pytest.raises(ScheduleError):
        build_plan(config, command=EXE, interval_minutes=minutes)


def test_the_task_name_is_stable(plan):
    assert plan.name == TASK_NAME
    assert find(plan, "t:RegistrationInfo/t:URI") == f"\\{TASK_NAME}"


# -- reading Windows back --------------------------------------------------


def test_schtasks_output_is_parsed_into_something_readable():
    text = """
Folder: \\
HostName:                             CHRIS-LAPTOP
TaskName:                             \\Ranger heartbeat
Next Run Time:                        07/09/2026 08:00:00
Status:                               Ready
Last Run Time:                        07/09/2026 07:00:02
Last Result:                          0
Task To Run:                          C:\\Users\\Chris.Hardwick\\Ranger\\.venv\\Scripts\\ranger.exe
Power Management:                     Stop On Battery Mode, No Start On Batteries
"""
    parsed = parse_query(text)
    assert parsed["Status"] == "Ready"
    assert parsed["Last Result"] == "0"
    assert parsed["Next Run Time"] == "07/09/2026 08:00:00"
    # The colon in a path must not truncate the value.
    assert parsed["Task To Run"].endswith("ranger.exe")


def test_the_definition_is_valid_xml_with_a_path_that_needs_escaping(config):
    awkward = Path("C:/Users/Chris & Co/Ranger/.venv/Scripts/ranger.exe")
    xml = to_xml(build_plan(config, command=awkward))
    assert "&amp;" in xml
    assert ElementTree.fromstring(xml) is not None
