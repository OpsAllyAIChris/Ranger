"""Guards on the test suite itself.

These exist because a real bug hid behind an invocation difference: under
`python -m pytest` the working directory lands on sys.path, so
`from tests.conftest import ...` resolved; under a bare `pytest` it does not,
and collection failed. The suite has to pass both ways.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TESTS = Path(__file__).parent
MODULES = sorted(p for p in TESTS.glob("test_*.py"))

BAD_IMPORT = re.compile(r"^\s*(from\s+tests[.\s]|import\s+tests\b|from\s+conftest\s|import\s+conftest\b)", re.M)


def test_there_are_test_modules_to_check():
    assert MODULES, "glob found nothing, so the checks below would pass vacuously"


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_no_module_imports_from_conftest(module: Path):
    """conftest reaches tests through fixtures, never through an import.

    An import only resolves when the repo root is on sys.path, which depends on
    how pytest was invoked. Add a fixture instead.
    """
    found = BAD_IMPORT.findall(module.read_text(encoding="utf-8"))
    assert not found, (
        f"{module.name} imports from conftest ({found}). Make it a fixture: "
        "the import breaks under a bare 'pytest'."
    )


def test_tests_is_not_a_package():
    """An __init__.py here would make the bad import work and hide the problem."""
    assert not (TESTS / "__init__.py").exists()


# --- nothing in the repo may be un-checkout-able on Windows ---------------
#
# A pytest simulation once wrote a literal "C:\tmp\..." tree into the repo and
# it got committed. Git on Windows then refused the whole checkout with
# "error: invalid path", so every pull failed and the operator silently stayed
# three commits behind. A repo that cannot be cloned on the platform it is
# developed on is broken, so this fails the suite instead.

REPO = TESTS.parent

# < > : " | ? * and backslash are illegal in a Windows filename, as are control
# characters. Names cannot end in a dot or a space.
ILLEGAL_CHARS = re.compile(r'[<>:"|?*\\\x00-\x1f]')
RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{n}" for n in range(1, 10)]
    + [f"LPT{n}" for n in range(1, 10)]
)


def _tracked_paths() -> list[str]:
    import subprocess

    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    return [p for p in out.split("\0") if p]


def test_git_ls_files_returned_something():
    assert len(_tracked_paths()) > 10, "the checks below would pass vacuously"


def test_no_tracked_path_is_illegal_on_windows():
    offenders: list[tuple[str, str]] = []
    for path in _tracked_paths():
        for segment in path.split("/"):
            if ILLEGAL_CHARS.search(segment):
                offenders.append((path, "illegal character"))
            elif segment != segment.rstrip(". "):
                offenders.append((path, "trailing dot or space"))
            elif segment.split(".")[0].upper() in RESERVED:
                offenders.append((path, "reserved device name"))
    assert not offenders, (
        "these cannot be checked out on Windows, so git aborts the entire pull:\n"
        + "\n".join(f"  {why}: {path}" for path, why in offenders)
    )


def test_no_untracked_windows_shaped_junk_in_the_repo():
    """Catch it before it is ever staged, not just after."""
    strays = [p for p in REPO.iterdir() if ILLEGAL_CHARS.search(p.name)]
    assert not strays, (
        "something wrote paths into the repo root that Windows cannot represent: "
        + ", ".join(repr(p.name) for p in strays)
    )


# --- no strftime directive that only works on one platform ----------------
#
# `%-d` is a glibc extension. Linux prints "7"; Windows raises
# `ValueError: Invalid format string`. Windows spells it `%#d`. Either one is a
# crash on the other platform, and the one that shipped would have crashed the
# morning check every day and every `ranger inbox` listing.
#
# This was the fourth Linux-only assumption to reach the operator, so it is a
# guard rather than a fix.

PLATFORM_DIRECTIVE = re.compile(r"%[-#][aAbBcdfHIjmMpSUwWxXyYzZG]")

#: These two must contain the pattern: one documents the rule, one tests it.
DIRECTIVE_EXEMPT = {"dates.py", "test_suite_hygiene.py"}

SOURCE_FILES = sorted(
    p
    for p in [*(REPO / "ranger").glob("*.py"), *(REPO / "tests").glob("*.py")]
    if p.name not in DIRECTIVE_EXEMPT
)


def test_there_are_source_files_to_check():
    assert len(SOURCE_FILES) > 15, "the check below would pass vacuously"


@pytest.mark.parametrize("source", SOURCE_FILES, ids=lambda p: p.name)
def test_no_platform_specific_strftime_directive(source: Path):
    found = PLATFORM_DIRECTIVE.findall(source.read_text(encoding="utf-8"))
    assert not found, (
        f"{source.name} uses {found}, which works on one platform and raises "
        "ValueError on the other. Use a zero-padded directive, or take the number "
        "off the datetime: ranger/dates.py has helpers for the common cases."
    )


def test_the_date_helpers_are_portable():
    """What the helpers produce must not depend on the platform."""
    from datetime import datetime

    from ranger.dates import day_and_month, human_datetime, human_day, prompt_datetime

    when = datetime(2026, 9, 7, 7, 27)
    assert day_and_month(when) == "7 September"
    assert human_day(when) == "Monday 7 September"
    assert human_datetime(when) == "Monday 7 September, 07:27"
    assert prompt_datetime(when) == "Monday 07 September 2026, 07:27"

    # Two digit days keep working, which is where a naive lstrip("0") breaks.
    assert day_and_month(datetime(2026, 9, 17, 7, 27)) == "17 September"
    assert human_day(datetime(2026, 10, 1, 0, 0)) == "Thursday 1 October"
