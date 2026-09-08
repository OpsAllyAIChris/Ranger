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


# -- the guards -------------------------------------------------------------
#
# A step that silently produced no files is the expensive failure: the training
# config goes on pointing at the empty directory, the run takes three hours
# anyway, and the model trains on whatever data did arrive. That happened on
# the first real run — AudioSet extracted, the glob for it found nothing, the
# conversion loop ran over an empty list, and nothing said so.


def test_every_download_step_checks_what_it_produced(notebook):
    """Not a spot check. Each of these is a step that can silently write no
    files, and each one is upstream of three hours of compute."""
    guarded = [
        joined(cell)
        for cell in notebook["cells"]
        if "produced(" in joined(cell) or "raise RuntimeError" in joined(cell)
    ]
    text = "\n".join(guarded)

    assert "./mit_rirs" in text, "impulse responses unchecked"
    assert "./audioset_16k" in text, "AudioSet conversion unchecked"
    assert "./fma" in text, "music download unchecked"
    assert "./holdout_16k" in text, "held out audio unchecked"
    assert "validation_set_features.npy" in text, "feature files unchecked"


def test_the_audioset_clips_are_searched_for_not_assumed(notebook):
    """The glob upstream uses found nothing on the first real run. Whatever the
    tar's layout is, the files are under ./audioset, so search rather than
    assume — and print the tree when the search comes back empty.
    """
    cell = next(c for c in notebook["cells"] if "audioset_16k" in joined(c))
    text = joined(cell)

    assert 'Path("audioset").rglob' in text
    assert 'Path("audioset/audio")' not in text
    assert "Nothing audio-shaped under ./audioset" in text


def test_the_config_cell_verifies_every_path_it_points_at(notebook):
    """The backstop. An empty directory is a valid directory, so the last
    chance to notice is the moment the config is written."""
    cell = next(c for c in notebook["cells"] if "custom_negative_phrases" in joined(c))
    text = joined(cell)

    assert "rir_paths" in text and "background_paths" in text
    assert "produced(" in text
    assert "piper_sample_generator_path" in text
    # Checked before the file is written, not after.
    assert text.index("produced(") < text.index('open("hey_ranger.yaml", "w")')


def test_the_helper_refuses_an_empty_directory(notebook, tmp_path):
    """Run the helper the notebook defines, rather than reading it."""
    cell = next(c for c in notebook["cells"] if "def produced" in joined(c))
    source = joined(cell)
    namespace: dict = {"Path": Path}
    exec(source[source.index("def produced") :], namespace)  # noqa: S102
    produced = namespace["produced"]

    empty = tmp_path / "audioset_16k"
    empty.mkdir()
    with pytest.raises(RuntimeError, match="expected at least"):
        produced(str(empty), at_least=100, what="clips")

    nested = tmp_path / "full" / "deeper"
    nested.mkdir(parents=True)
    for index in range(5):
        (nested / f"{index}.wav").write_bytes(b"x")
    assert len(produced(str(tmp_path / "full"), at_least=5, what="clips")) == 5


def test_the_environment_cell_checks_its_own_installs(notebook):
    """`!pip install` cannot fail a cell: a shell magic's exit code is ignored.

    That is how a failed install of openwakeword itself reached step 6 as
    ModuleNotFoundError, three cells and forty minutes later.
    """
    setup = next(c for c in notebook["cells"] if "piper-sample-generator" in joined(c))
    text = joined(setup)

    assert "!pip install" not in text, "an unchecked install is a silent failure"
    assert "subprocess.run" in text
    assert "raise RuntimeError" in text


def test_openwakeword_installs_without_its_own_dependencies(notebook):
    """speexdsp-ns publishes wheels for cp37 to cp312 and no source
    distribution, so on a newer Python there is nothing to install and nothing
    to build from, and it fails the whole install. Training never touches it.
    """
    setup = next(c for c in notebook["cells"] if "piper-sample-generator" in joined(c))
    text = joined(setup)

    assert '"-e", REPO, "--no-deps"' in text
    assert "speexdsp-ns" in text, "the reason for --no-deps has to be written down"
    # --no-deps skips onnxruntime, and AudioFeatures defaults to ONNX, so the
    # trainer would fail on its first line without it.
    assert '"onnxruntime"' in text


def test_training_runs_on_its_own_python(notebook):
    """Colab runs Python 3.13, where speexdsp-ns, piper-phonemize and
    tflite-runtime all publish wheels stopping at cp312 with no source
    distribution. No pin fixes a wheel that does not exist, so training gets
    its own 3.11 and the notebook's kernel only orchestrates.
    """
    setup = next(c for c in notebook["cells"] if "py311" in joined(c))
    text = joined(setup)

    assert '"--python", "3.11"' in text
    assert "py311/bin/python" in text

    for cell in notebook["cells"]:
        if "train.py --training_config" in joined(cell):
            assert "{PY}" in joined(cell), "the trainer must not run on the kernel's Python"


def test_the_sample_generator_comes_from_the_fork_that_has_it(notebook):
    """This is failure seven, and it is upstream contradicting itself.

    openWakeWord's notebook clones rhasspy/piper-sample-generator, which has
    been restructured into a piper_sample_generator package with no
    generate_samples module. openWakeWord's own config file names dscripka's
    fork, which still has the module train.py imports. The config is right.
    """
    setup = next(c for c in notebook["cells"] if "piper-sample-generator" in joined(c))
    text = joined(setup)

    assert "dscripka/piper-sample-generator" in text
    assert "rhasspy/piper-sample-generator releases" not in text
    # The fork's own default model, from the release it was built against. The
    # notebook downloads v2.0.0's en_US-libritts_r-medium.pt, which belongs to
    # the restructured repo and is not what this fork loads.
    assert "en-us-libritts-high.pt" in text
    # It may be named in a comment saying why it is wrong, but never fetched.
    code = [line for line in text.split("\n") if not line.lstrip().startswith("#")]
    assert "en_US-libritts_r-medium.pt" not in "\n".join(code)


def test_every_download_refuses_a_404_written_to_disk(notebook):
    """Failure four: the URL 404'd, wget saved the error body under the name it
    was given, and every step downstream reported success."""
    cells = [joined(cell) for cell in notebook["cells"]]

    assert any("def fetch(" in text and "error page and not a file" in text for text in cells)
    for text in cells:
        assert "!wget" not in text, "an unchecked download is a silent 404"


def test_the_preflight_names_the_module_not_the_package(notebook):
    """Importing piper_sample_generator would pass and step 6 would still fail
    forty minutes later. The check has to use the name train.py uses."""
    preflight = next(c for c in notebook["cells"] if "CHECKS = [" in joined(c))
    text = joined(preflight)

    assert "from generate_samples import generate_samples" in text
    assert "py311" in text, "checked where the trainer will find out"


def test_there_is_a_preflight_before_anything_is_downloaded(notebook):
    """Five incompatibilities surfaced only at execution time and three of them
    forty minutes into training. Every one was an import that could have been
    tried in thirty seconds. The preflight has to run before the downloads or
    it saves nothing.
    """
    kinds = [joined(cell) for cell in notebook["cells"]]
    preflight = next(index for index, text in enumerate(kinds) if "CHECKS = [" in text)
    downloads = next(index for index, text in enumerate(kinds) if "mit_rirs" in text)

    assert preflight < downloads

    text = kinds[preflight]
    assert "import openwakeword.train" in text, "the trainer's own import chain"
    assert "generate_samples" in text, "piper, which step 6 needs first"
    assert "raise RuntimeError" in text


def test_the_augmentation_pin_is_one_that_imports(notebook):
    """torch-audiomentations 0.11.0 is upstream's pin and calls
    torchaudio.set_audio_backend, removed in torchaudio 2.1. Colab ships far
    newer than that, so upstream's pin cannot import on today's image at all.
    """
    setup = next(c for c in notebook["cells"] if "audiomentations" in joined(c))
    text = joined(setup)

    assert "torch-audiomentations==0.11.0" not in text
    assert "torch-audiomentations==0.11.2" in text
    assert "set_audio_backend" in text, "the reason for the bump has to be written down"

