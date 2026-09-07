"""Formatting dates the same way on every platform.

strftime's unpadded directives are not portable. `%-d` is a glibc extension:
Linux prints "7", Windows raises `ValueError: Invalid format string`. Windows
spells the same thing `%#d`. Anything built from either one works on the machine
it was written on and crashes on the other.

So the convention here is: **never write `%-` or `%#`.** Use only zero-padded
strftime directives, and when unpadded output is wanted, take the integer off
the datetime and format it as a number. That is portable by construction rather
than by remembering which platform is which.

`tests/test_suite_hygiene.py` fails the suite on any `%-` or `%#` directive
anywhere in the codebase, so this cannot come back.
"""

from __future__ import annotations

from datetime import date, datetime


def day_and_month(when: date | datetime) -> str:
    """7 September"""
    return f"{when.day} {when:%B}"


def human_day(when: date | datetime) -> str:
    """Monday 7 September"""
    return f"{when:%A} {when.day} {when:%B}"


def human_datetime(when: datetime) -> str:
    """Monday 7 September, 07:27"""
    return f"{human_day(when)}, {when:%H:%M}"


def prompt_datetime(when: datetime) -> str:
    """Monday 07 September 2026, 07:27, for the system prompt."""
    return f"{when:%A %d %B %Y, %H:%M}"
