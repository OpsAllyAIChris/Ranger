"""Running the heartbeat without a terminal open.

The 07:00 brief is the only part of Ranger that has to happen whether or not
the operator remembered to start something, and until now it depended on a
PowerShell window being left open overnight. This registers a Windows Task
Scheduler entry instead.

Task Scheduler rather than a real Windows service, deliberately. A service
needs a wrapper or pywin32 to speak the service control protocol, and it runs
as SYSTEM, which has none of the operator's environment: not their `.env`, not
their `HKCU`, not their mapped drives. A scheduled task runs as them.

Three settings carry most of the value and two of them default the wrong way
for a laptop:

- **StartWhenAvailable.** A task whose time passed while the machine was asleep
  runs shortly after it wakes instead of being skipped. This is the answer to
  the sleep and wake question that has been open since Tier 5, and it is why
  the task fires hourly and lets Ranger's own scheduler decide what is due:
  one source of truth, and a missed hour is caught by the next one.
- **DisallowStartIfOnBatteries defaults to true.** On an unplugged laptop the
  morning brief would simply never run, with nothing in the log to say why.
- **StopIfGoingOnBatteries defaults to true.** Same failure, halfway through.

The task runs `ranger heartbeat --once --log <path>` directly, with no shell.
Redirecting output through `cmd /c` would mean nested quoting inside an XML
attribute inside a command line, which is three chances to get a path with a
space in it wrong. The `--log` flag does the same job and behaves identically
when the operator runs it by hand.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from .config import Config

#: What it is called in Task Scheduler. Shown in the library, and the handle
#: for removing it, so it is stable rather than derived from anything.
#: Deliberately still "Ranger". A scheduled task name is installed state on the
#: operator's machine, not an identity string: renaming it would leave the old
#: task registered and running alongside a new one, which is two things
#: starting the interface at logon. Same category as the CLI command and the
#: vault folder, and the same reason -- the rename is cosmetic and the
#: migration is not.
TASK_NAME = "Ranger heartbeat"

#: The second task: keep the interface up so a pinned icon only has to open a
#: window at a server that is already listening.
INTERFACE_TASK_NAME = "Ranger interface"

#: Hourly. Ranger's own scheduler decides what is actually due, so the trigger
#: only has to be frequent enough that a missed slot is caught soon.
DEFAULT_INTERVAL_MINUTES = 60


class ScheduleError(Exception):
    """Anything that stops the task being registered."""


@dataclass(frozen=True)
class Plan:
    """Exactly what will be registered, in words the operator can check."""

    name: str
    command: Path
    arguments: str
    working_directory: Path
    log_path: Path
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES
    start_hour: int = 7
    #: At logon and then left running, rather than on a clock. The interface is
    #: a server: it starts once and stays up, and restarting it hourly would
    #: drop whatever window was connected to it.
    at_logon: bool = False
    #: Who the task belongs to. Empty means the account registering it, which
    #: is what a per-user task needs to say to avoid needing an elevated shell.
    user: str = ""

    def describe(self) -> list[str]:
        when = (
            "at logon, and stays running"
            if self.at_logon
            else f"every {self.interval_minutes} minutes, from {self.start_hour:02d}:00"
        )
        return [
            f"name        {self.name}",
            f"runs        {self.command}",
            f"with        {self.arguments}",
            f"in          {self.working_directory}",
            f"when        {when}",
            f"log         {self.log_path}",
        ]


def executable(windowless: bool = False) -> Path:
    """The launcher to register.

    `ranger.exe` is what the operator asked for and it is the same thing they
    type. It is a console program, so Task Scheduler flashes a console window
    on every run; `windowless` swaps in `pythonw.exe`, which has no console at
    all, at the cost of the command in Task Scheduler no longer being the
    command they know.
    """
    scripts = Path(sys.executable).parent
    if windowless:
        for name in ("pythonw.exe", "pythonw"):
            candidate = scripts / name
            if candidate.exists():
                return candidate
        raise ScheduleError(
            f"no pythonw next to {sys.executable}, so there is no way to run without a window"
        )

    for name in ("ranger.exe", "ranger"):
        candidate = scripts / name
        if candidate.exists():
            return candidate
    raise ScheduleError(
        f"no ranger executable in {scripts}. Run 'pip install -e \".[dev]\"' first, "
        "with the virtual environment activated"
    )


def default_log(config: Config) -> Path:
    """Beside the vault's own log folder, so everything is in one place."""
    return config.vault.log / "heartbeat.log"


def build_plan(
    config: Config,
    *,
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    log_path: Path | None = None,
    windowless: bool = False,
    config_path: Path | None = None,
    command: Path | None = None,
) -> Plan:
    if interval_minutes < 1 or interval_minutes > 1440:
        raise ScheduleError("the interval must be between 1 and 1440 minutes")

    launcher = command or executable(windowless)
    log = log_path or default_log(config)

    # Always an absolute -c, never a relative one and never none. A scheduled
    # task can be started from anywhere, and a Jarvis that cannot find
    # ranger.toml fails with a config error that says nothing about why the
    # working directory was C:\Windows\System32.
    settings = config_path or config.source_path
    if settings is None:
        raise ScheduleError("the loaded config has no path, so the task cannot point at it")

    arguments: list[str] = []
    if windowless:
        arguments += ["-m", "ranger"]
    arguments += ["-c", _quote(Path(settings).resolve())]
    arguments += ["heartbeat", "--once", "--log", _quote(log)]

    return Plan(
        name=TASK_NAME,
        command=launcher,
        arguments=" ".join(arguments),
        working_directory=Path(settings).resolve().parent,
        log_path=log,
        interval_minutes=interval_minutes,
        start_hour=config.schedule.morning_hour,
        user=current_user(),
    )


def build_interface_plan(
    config: Config,
    *,
    log_path: Path | None = None,
    windowless: bool = False,
    config_path: Path | None = None,
    command: Path | None = None,
) -> Plan:
    """Keep `ranger ui` running from logon.

    Same mechanism as the heartbeat task and a different trigger: a server is
    started once and left alone, so a repeating trigger would kill the window
    the operator is looking at every hour.
    """
    launcher = command or executable(windowless)
    log = log_path or (config.vault.log / "ui.log")

    settings = config_path or config.source_path
    if settings is None:
        raise ScheduleError("the loaded config has no path, so the task cannot point at it")

    arguments: list[str] = []
    if windowless:
        arguments += ["-m", "ranger"]
    arguments += ["-c", _quote(Path(settings).resolve()), "ui", "--log", _quote(log)]

    return Plan(
        name=INTERFACE_TASK_NAME,
        command=launcher,
        arguments=" ".join(arguments),
        working_directory=Path(settings).resolve().parent,
        log_path=log,
        at_logon=True,
        user=current_user(),
    )


def _quote(path: Path) -> str:
    text = str(path)
    return f'"{text}"' if " " in text else text


def _triggers(plan: Plan) -> str:
    """At logon for the interface, on a clock for the heartbeat."""
    if plan.at_logon:
        return (
            "    <LogonTrigger>\n"
            "      <Enabled>true</Enabled>\n"
            f"      <UserId>{escape(plan.user)}</UserId>\n"
            "      <Delay>PT20S</Delay>\n"
            "    </LogonTrigger>"
        )
    return f"""    <CalendarTrigger>
      <StartBoundary>2026-01-01T{plan.start_hour:02d}:00:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
      <Repetition>
        <Interval>PT{plan.interval_minutes}M</Interval>
        <Duration>P1D</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </CalendarTrigger>"""


def to_xml(plan: Plan) -> str:
    """The task definition. Written out rather than built with schtasks flags.

    `schtasks /Create` with flags cannot set StartWhenAvailable or the battery
    settings, and those are the three that decide whether this works on a
    laptop at all.
    """
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(
        "Jarvis's heartbeat. Surfaces the morning brief and anything else that is due. "
        "Registered by 'ranger schedule install'."
    )}</Description>
    <URI>\\{escape(plan.name)}</URI>
  </RegistrationInfo>
  <Triggers>
{_triggers(plan)}
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(plan.user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>{"PT0S" if plan.at_logon else "PT1H"}</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(plan.command))}</Command>
      <Arguments>{escape(plan.arguments)}</Arguments>
      <WorkingDirectory>{escape(str(plan.working_directory))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


# -- talking to Windows -----------------------------------------------------


def current_user() -> str:
    """DOMAIN\\user, the way Task Scheduler wants to be told who this is for.

    Load bearing rather than cosmetic. A LogonTrigger with no UserId means
    "when *any* user logs on", which is a machine-wide setting and needs an
    elevated shell to register. Named to this user it is an ordinary per-user
    task, and the heartbeat task never hit this because a calendar trigger has
    no such distinction.
    """
    import getpass

    user = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{user}" if domain else user


def on_windows() -> bool:
    return os.name == "nt"


def _schtasks() -> str:
    found = shutil.which("schtasks")
    if not found:
        raise ScheduleError("schtasks is not on PATH, so the task cannot be registered")
    return found


def _run(arguments: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_schtasks(), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def install(plan: Plan, *, replace: bool = True) -> str:
    """Register the task. Returns whatever schtasks said."""
    if not on_windows():
        raise ScheduleError("Task Scheduler is Windows only. 'ranger schedule xml' still works")

    import tempfile

    # UTF-16 with a BOM: schtasks /XML rejects anything else, and the error it
    # gives says nothing about encoding.
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".xml", delete=False, encoding="utf-16", newline="\r\n"
    )
    try:
        handle.write(to_xml(plan))
        handle.close()
        arguments = ["/Create", "/TN", plan.name, "/XML", handle.name]
        if replace:
            arguments.append("/F")
        result = _run(arguments)
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass

    if result.returncode != 0:
        raise ScheduleError(_explain(plan, arguments, result))
    return (result.stdout or "").strip()


#: What Windows says when a task needs rights the shell does not have. Checked
#: as text because schtasks returns 1 for everything.
DENIED = ("access is denied", "e_accessdenied", "0x80070005")


def _explain(plan: Plan, arguments: list[str], result: subprocess.CompletedProcess) -> str:
    """The whole failure, not a fragment of it.

    "ERROR: Access is denied." on its own says nothing about what was denied,
    which invocation produced it, or whether elevation would help. Every one of
    those is knowable here.
    """
    output = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part and part.strip()
    )
    lines = [
        f"schtasks refused to register {plan.name} (exit {result.returncode}).",
        "",
        "  what ran:",
        "    schtasks " + " ".join(_show(a) for a in arguments),
        "",
        "  what it said:",
    ]
    lines += [f"    {line}" for line in (output or "nothing at all").splitlines()]

    if any(marker in output.lower() for marker in DENIED):
        lines += [
            "",
            "  This is an elevation problem, and it is a task setting rather than",
            "  a permissions accident: a logon trigger with no user named on it",
            "  means 'when anyone logs on', which is machine wide. It is now named",
            "  to " + plan.user + ", which should not need an elevated shell.",
            "",
            "  If it still refuses, run this once from an elevated PowerShell:",
            "    ranger schedule install --interface",
        ]
    return "\n".join(lines)


def _show(argument: str) -> str:
    return f'"{argument}"' if " " in argument else argument


def remove(name: str = TASK_NAME) -> bool:
    """False if there was no such task, which is not an error."""
    if not on_windows():
        raise ScheduleError("Task Scheduler is Windows only")
    result = _run(["/Delete", "/TN", name, "/F"])
    return result.returncode == 0


def status(name: str = TASK_NAME) -> dict[str, str] | None:
    """What Task Scheduler currently thinks, or None if it has never heard of it."""
    if not on_windows():
        return None
    result = _run(["/Query", "/TN", name, "/FO", "LIST", "/V"])
    if result.returncode != 0:
        return None
    return parse_query(result.stdout)


def parse_query(text: str) -> dict[str, str]:
    """schtasks /FO LIST output into a dict. Its own format, not ours."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        label = label.strip()
        if label:
            found[label] = value.strip()
    return found


#: The fields worth showing back, in the order they answer "is it working".
INTERESTING = (
    "Status",
    "Next Run Time",
    "Last Run Time",
    "Last Result",
    "Schedule Type",
    "Repeat: Every",
    "Task To Run",
    "Start In",
    "Logon Mode",
    "Power Management",
)
