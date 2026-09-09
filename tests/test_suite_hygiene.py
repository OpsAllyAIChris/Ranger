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
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True,
        encoding="utf-8", errors="replace",
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


# -- the account write path stays binary ------------------------------------


ACCOUNT_WRITE_PATH = [
    Path(__file__).resolve().parent.parent / "ranger" / "marker.py",
]


def test_the_marker_module_never_uses_text_mode_io():
    """Nine tests failed on Windows and none on Linux because of one call.

    `Path.write_text` opens in text mode. On Windows it rewrites every `\n` as
    `\r\n`, so an existing `\r\n` becomes `\r\r\n`, and `read_text` folds
    that to `\n\n`. The account write path's entire design is byte identity,
    so a single text-mode call anywhere in it makes the guard refuse files that
    are fine -- or, worse in the migration, rewrite the CRM half it exists to
    protect.

    Linux translates nothing, so the suite round-tripped happily and agreed
    with itself. This is the rule written where it can be enforced.
    """
    for source in ACCOUNT_WRITE_PATH:
        text = source.read_text(encoding="utf-8")
        code = "\n".join(
            line for line in text.splitlines()
            if not line.lstrip().startswith(("#", "#:"))
        )
        for banned in ("write_text(", "read_text("):
            assert banned not in code, (
                f"{source.name} calls {banned} -- the account write path is binary "
                "end to end, because it promises byte identity"
            )


def test_append_below_marker_never_uses_text_mode_io():
    import inspect

    from ranger.vault import Vault

    body = inspect.getsource(Vault.append_below_marker)
    code = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )
    assert "write_text(" not in code
    assert "read_text(" not in code
    assert "read_bytes()" in code and "write_bytes(" in code


def test_the_digest_cannot_be_handed_a_string():
    """The bug in one call. Bytes-only means it cannot come back quietly."""
    import pytest as _pytest

    from ranger.marker import digest

    with _pytest.raises(TypeError):
        digest("text that has already been through a reader")



# --- nothing may rely on the platform's default encoding -------------------
#
# A Windows default the Linux side never sees, which is the shape of every real
# bug in this build. Twice now:
#
#   `read_text()` on an account note turned CRLF into a doubled newline, and
#   the suite could not see it because Linux has no CRLF.
#
#   `subprocess.run(..., text=True)` decoded the node harness's UTF-8 with
#   cp1252, and the first thing the rendered-panel check ever said on Windows
#   was `assert 'Ã—' == '×'`. The button was fine; the test could not read it.
#
# The one that had not surfaced yet was worse: `git` in the snapshot took the
# staging list on stdin in text mode, so a vault note called `Café.md` would
# have been *sent* to git as cp1252 bytes, matched nothing, and been quietly
# missing from the day's backup.
#
# So the encoding is stated everywhere, and this is what keeps it stated.

SOURCE = sorted(REPO.glob("ranger/**/*.py")) + sorted(TESTS.glob("*.py"))

#: Reading bytes needs no encoding, and neither does a codec-owning library.
_BINARY_MODE = re.compile(r"['\"][rwax]*b[rwax+]*['\"]")


def _has(call, name: str) -> bool:
    return any(word.arg == name for word in call.keywords)


def encoding_offenders(tree, module: str) -> list[str]:
    import ast

    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        line = getattr(node, "lineno", 0)
        function = node.func
        name = getattr(function, "attr", None) or getattr(function, "id", None)

        # `Path.read_text()` takes no positional argument and `Vault.read_text`
        # takes the path, which is how one is told from the other. The vault's
        # own method states the encoding in one place, for everything.
        if name == "read_text" and not node.args and not _has(node, "encoding"):
            problems.append(f"{module}:{line} read_text() with no encoding")
        if name == "write_text" and not _has(node, "encoding"):
            problems.append(f"{module}:{line} write_text() with no encoding")

        if name == "open" and isinstance(function, ast.Name):
            mode = node.args[1] if len(node.args) > 1 else None
            literal = ast.unparse(mode) if mode is not None else "'r'"
            if not _BINARY_MODE.search(literal) and not _has(node, "encoding"):
                problems.append(f"{module}:{line} open() in text mode with no encoding")

        if name in ("run", "Popen", "check_output") and isinstance(function, ast.Attribute):
            if getattr(function.value, "id", "") != "subprocess":
                continue
            texty = _has(node, "text") or _has(node, "universal_newlines") or (
                _has(node, "encoding")
            )
            if texty and not _has(node, "encoding"):
                problems.append(f"{module}:{line} subprocess in text mode with no encoding")
    return problems


@pytest.mark.parametrize("source", SOURCE, ids=lambda p: p.name)
def test_nothing_reads_or_writes_with_the_platform_default_encoding(source: Path):
    """Every read, write and subprocess says what encoding it means.

    Not style. `text=True` means cp1252 on the operator's machine and UTF-8 on
    the machine that runs the suite, so anything relying on the default is a
    test that passes here and a behaviour that differs there -- which is where
    every real defect in this build has come from.
    """
    import ast

    tree = ast.parse(source.read_text(encoding="utf-8"))
    problems = encoding_offenders(tree, source.name)

    assert not problems, "\n".join(problems) + (
        "\n\nAdd encoding='utf-8' (and errors='replace' for a program's output). "
        "The platform default is not the same on Windows."
    )


def test_the_encoding_check_can_actually_fail():
    """A guard that cannot fail is decoration. This is what it catches."""
    import ast

    bad = ast.parse(
        "import subprocess\n"
        "p.read_text()\n"
        "open('x.txt')\n"
        "subprocess.run(['git'], text=True)\n"
    )
    problems = encoding_offenders(bad, "example.py")

    assert len(problems) == 3
    assert any("read_text" in item for item in problems)
    assert any("open()" in item for item in problems)
    assert any("subprocess" in item for item in problems)
