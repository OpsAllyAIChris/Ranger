"""The generated training notebook, checked the way Jupyter reads it.

`scripts/train_wake_word.py` writes a Colab notebook. The first version of it
stored each cell's source as a list of lines with the newlines stripped, so
Colab rendered every cell as one run-on line and cell one died on a
SyntaxError before the operator's three hour run had started.

**The check that was supposed to catch that rejoined the list with `"\\n"`
before parsing it.** It put the newlines back itself and then confirmed they
were there. So the one rule this file works under is that nothing here may
join a cell's source with anything but `""`, because `"".join(source)` is what
Jupyter does. A helper doing it for us is how the bug survived.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "train_wake_word.py"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("train_wake_word", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def notebook(script, tmp_path):
    """Written to disk and read back with json, not held in memory.

    The failure was in what reached the file, so a test against the dict the
    script built in memory would have passed.
    """
    out = tmp_path / "hey_ranger_training.ipynb"
    assert script.main(["--phrase", "hey ranger", "--out", str(out)]) == 0
    return json.loads(out.read_text(encoding="utf-8"))


def joined(cell) -> str:
    """A cell's source the way Jupyter reconstructs it. The only way allowed."""
    return "".join(cell["source"])


def shell_stripped(source: str) -> str:
    """Colab's `!command` lines are not Python. Everything else must be."""
    return "\n".join(
        "pass" if line.strip().startswith("!") else line for line in source.split("\n")
    )


def test_every_line_but_the_last_keeps_its_newline(notebook):
    """This is the bug, stated directly.

    nbformat's list-of-strings is a list of *lines*, each ending in a newline.
    Without them `"".join` welds the file into one line.
    """
    for index, cell in enumerate(notebook["cells"]):
        source = cell["source"]
        assert source, f"cell {index} is empty"
        for line_number, line in enumerate(source[:-1]):
            assert line.endswith("\n"), (
                f"cell {index} line {line_number} has no trailing newline: {line!r}"
            )
        assert not source[-1].endswith("\n"), f"cell {index} ends with a blank line"


def test_a_cell_rejoined_is_many_lines_not_one(notebook):
    """The symptom the operator saw: `import osimport sysimport uuid`."""
    for index, cell in enumerate(notebook["cells"]):
        text = joined(cell)
        assert text.count("\n") + 1 == len(cell["source"]), (
            f"cell {index} rejoins to {text.count(chr(10)) + 1} lines from "
            f"{len(cell['source'])} entries"
        )


def test_the_imports_cell_is_not_one_line(notebook):
    """A named case, because it is the one that failed and it is unmistakable."""
    imports = next(
        cell for cell in notebook["cells"] if joined(cell).lstrip().startswith("import os")
    )

    assert "import osimport" not in joined(imports)
    assert len(joined(imports).splitlines()) > 5


def test_every_code_cell_parses_as_python(notebook):
    """The check that actually catches it, and only because of `"".join`."""
    for index, cell in enumerate(notebook["cells"]):
        if cell["cell_type"] != "code":
            continue
        try:
            ast.parse(shell_stripped(joined(cell)))
        except SyntaxError as exc:
            pytest.fail(f"cell {index} is not valid Python: {exc}")


def test_the_notebook_is_shaped_the_way_nbformat_requires(notebook):
    assert notebook["nbformat"] == 4
    assert notebook["metadata"]["accelerator"] == "GPU"
    for index, cell in enumerate(notebook["cells"]):
        assert cell["cell_type"] in {"code", "markdown"}, index
        assert isinstance(cell["source"], list), index
        assert all(isinstance(line, str) for line in cell["source"]), index
        if cell["cell_type"] == "code":
            assert cell["outputs"] == []
            assert cell["execution_count"] is None


def test_the_phrase_and_its_negatives_reach_the_config_cell(notebook):
    config = next(
        cell for cell in notebook["cells"] if "custom_negative_phrases" in joined(cell)
    )
    text = joined(config)

    assert "'hey ranger'" in text
    assert "'hey stranger'" in text
    # Read from openWakeWord's own config in the clone, never written from
    # scratch, or it drifts silently from whatever version was cloned.
    assert "openwakeword/examples/custom_model.yml" in text


def test_the_acceptance_cell_looks_where_the_trainer_writes(notebook):
    """The trainer nests its output and the two paths are at different levels.

    output_dir/model_name.onnx for the model, output_dir/model_name/positive_test
    for the clips. Reading both from the same level found no clips, and only at
    the end of a three hour run.
    """
    acceptance = next(cell for cell in notebook["cells"] if "false/hr" in joined(cell))
    text = joined(acceptance)

    assert "./hey_ranger/hey_ranger.onnx" in text
    assert "./hey_ranger/hey_ranger/positive_test" in text
    # False fires are counted against audio the model never trained on.
    assert "./holdout_16k" in text
    assert "./audioset_16k" not in text


def test_a_one_word_phrase_is_refused(script, tmp_path):
    out = tmp_path / "ranger.ipynb"

    assert script.main(["--phrase", "ranger", "--out", str(out)]) == 2
    assert not out.exists()


def test_a_phrase_with_no_negatives_still_produces_a_notebook(script, tmp_path):
    """Refusing would be worse. It is said out loud on stdout instead."""
    out = tmp_path / "hey_bartholomew_training.ipynb"

    assert script.main(["--phrase", "hey bartholomew", "--out", str(out)]) == 0

    notebook = json.loads(out.read_text(encoding="utf-8"))
    config = next(
        cell for cell in notebook["cells"] if "custom_negative_phrases" in joined(cell)
    )
    assert "'hey bartholomew'" in joined(config)
