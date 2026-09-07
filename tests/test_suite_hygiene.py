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
