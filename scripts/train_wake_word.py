#!/usr/bin/env python
"""Write a Colab notebook that trains "hey ranger", ready to run top to bottom.

openWakeWord publishes six phrases and "hey ranger" is not one of them, so it
has to be trained. This script does not train it and cannot: openWakeWord's
automated training only runs on Linux, because the text to speech library it
generates samples with does not work on Windows. What it does is write a
notebook you upload to Colab and run.

    python scripts/train_wake_word.py

The notebook is openWakeWord's own `automatic_model_training.ipynb` with three
changes, all of them in one cell:

  - the phrase is "hey ranger"
  - a list of adversarial negatives written for that phrase
  - sample counts raised to openWakeWord's own recommended minimum, which its
    example notebook sits below because it is demonstrating the mechanism
    rather than producing a model anyone will use

Everything else, including every dataset download, is upstream's, and the
training config is read from openWakeWord's own `examples/custom_model.yml`
inside the clone rather than written out here. That is deliberate: a config
written from memory drifts from whatever version the notebook clones, and it
drifts silently, as a key that is quietly never read.

One cell is not upstream's at all: the last one measures the trained model
against held out clips and against an hour of music and noise, and prints
detections against false fires per hour at every threshold. That is the table
that decides whether the model is usable and what to set `wake.threshold` to.

See docs/training-hey-ranger.md for the walkthrough.
"""

from __future__ import annotations

import argparse
import json
import sys

#: Words and phrases close enough to the target to be worth training against by
#: name. openWakeWord already generates adversarial negatives automatically
#: from phoneme overlap; this is the hand written layer on top, and it is where
#: knowing the phrase is used by a person who talks about ranches, ranges and
#: rangers all day is worth more than any amount of compute.
NEAR_MISSES = {
    "hey ranger": [
        "hey stranger",
        "hey danger",
        "hey rachel",
        "hey ranch",
        "hey range",
        "hey rangers",
        "a ranger",
        "the ranger",
        "lone ranger",
        "range rover",
        "arranger",
        "ranger",
        "hey",
    ]
}

#: openWakeWord's example config sets 10,000 and its own comment calls 20,000
#: the minimum for a real model. The example notebook uses 1,000, which is a
#: demonstration and not a model. This is the floor, and the notebook says what
#: raising it costs in time.
SAMPLES = 20000
SAMPLES_VAL = 2000
STEPS = 50000


def _lines(source: str) -> list[str]:
    """A cell's source, in the shape nbformat means by a list of strings.

    Each entry **keeps its trailing newline**, and the last one does not have
    one. Jupyter reconstructs a cell with `"".join(source)`, so entries without
    newlines come back as one run-on line: every import on one line, every
    comment welded to the code after it, and a SyntaxError on the first cell.

    Splitting on "\n" and dropping the separators produced exactly that, and
    the check that was supposed to catch it rejoined the list with "\n" before
    parsing — so it put the newlines back itself and agreed with the bug.
    Anything verifying this has to join with "" or go through nbformat.
    """
    return source.rstrip("\n").splitlines(keepends=True)


def _code(source: str) -> dict:
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": _lines(source),
    }


def _markdown(source: str) -> dict:
    return {
        "cell_type": "markdown",
        "metadata": {},
        "source": _lines(source),
    }


SETUP = """\
# Step 1 of 7. Environment. Fifteen minutes, and it prints a lot.
#
# This is not upstream's setup cell. Upstream's cannot run on today's Colab,
# and the reason is one thing rather than eight: Colab now runs Python 3.13,
# and three libraries in this chain publish wheels that stop at cp312 with no
# source distribution to build from. speexdsp-ns, piper-phonemize and
# tflite-runtime are all uninstallable there, and no pin fixes a wheel that
# does not exist.
#
# So training runs in its own Python 3.11, built by uv, and the notebook's own
# kernel is only used to orchestrate and to download data. Every one of those
# libraries installs cleanly on 3.11.
#
# The second thing upstream gets wrong is which sample generator to clone. Its
# notebook clones rhasspy/piper-sample-generator, which has since been
# restructured into a package and no longer contains generate_samples.py at
# all. openWakeWord's own config file names dscripka's fork instead, and that
# one still has the module train.py imports. The notebook and the config
# disagree; the config is right.

import os
import subprocess
import sys
import urllib.request

REPO = "./openwakeword"
PIPER = "./piper-sample-generator"
PY = os.path.abspath("./py311/bin/python")


def run(*command, must_work=True):
    print("$", " ".join(str(part) for part in command))
    result = subprocess.run([str(part) for part in command], capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout[-2000:])
        print(result.stderr[-2000:])
        if must_work:
            raise RuntimeError(f"failed ({result.returncode}): {' '.join(str(c) for c in command)}")
        print(f"!! FAILED, continuing: {' '.join(str(c) for c in command)}")
    return result.returncode == 0


def fetch(url, path, at_least=1000):
    \"\"\"Download, and refuse to accept a 404 written to disk as a file.

    wget and urlretrieve both save whatever came back under the name they were
    given. A 404 page, or a zero byte file, looks exactly like a download that
    worked, and the failure surfaces hours later as an empty directory.
    \"\"\"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    urllib.request.urlretrieve(url, path)
    size = os.path.getsize(path)
    print(f"{path}: {size / 1e6:.1f} MB")
    if size < at_least:
        os.remove(path)
        raise RuntimeError(
            f"{url} returned {size} bytes, which is an error page and not a file. "
            "Nothing downstream would have noticed."
        )
    return path


# -- a Python the pins can actually be installed on ------------------------

run(sys.executable, "-m", "pip", "install", "-q", "uv")
run(sys.executable, "-m", "uv", "venv", "--python", "3.11", "py311")
print("training interpreter:", subprocess.run([PY, "-V"], capture_output=True, text=True).stdout.strip())


def uvpip(*packages, must_work=True):
    return run(sys.executable, "-m", "uv", "pip", "install", "--python", PY, *packages,
               must_work=must_work)


# -- espeak, which the sample generator phonemises with --------------------
#
# dscripka's fork imports espeak_phonemizer, which is a binding to a system
# library rather than a self-contained wheel. Upstream's notebook installs
# piper-phonemize instead, which the fork does not use and which has no wheel
# for Colab's Python anyway.

run("apt-get", "install", "-y", "-qq", "espeak-ng", "libespeak-ng1", must_work=False)

# -- the repositories ------------------------------------------------------

if not os.path.exists(PIPER):
    run("git", "clone", "--depth", "1", "https://github.com/dscripka/piper-sample-generator", PIPER)
if not os.path.exists(REPO):
    run("git", "clone", "--depth", "1", "https://github.com/dscripka/openwakeword", REPO)

# The model the fork actually defaults to, from the release it was built
# against. Upstream's notebook downloads en_US-libritts_r-medium.pt from
# v2.0.0, which belongs to the restructured repo and is not what this fork
# loads.
fetch(
    "https://github.com/rhasspy/piper-sample-generator/releases/download/v1.0.0/en-us-libritts-high.pt",
    os.path.join(PIPER, "models", "en-us-libritts-high.pt"),
    at_least=10_000_000,
)

# -- everything training needs, into the 3.11 environment ------------------

uvpip("torch", "torchaudio", "numpy", "scipy", "pyyaml", "tqdm", "requests", "scikit-learn")
uvpip("espeak-phonemizer", "webrtcvad")
uvpip("-e", REPO, "--no-deps")
uvpip("onnxruntime", "mutagen==1.47.0", "torchinfo==1.8.0", "torchmetrics==1.2.0")
uvpip("speechbrain==0.5.14", "audiomentations==0.33.0", "acoustics==0.2.6")

# torch-audiomentations 0.11.0 is openWakeWord's pin and it calls
# torchaudio.set_audio_backend, removed in torchaudio 2.1. 0.11.1 dropped the
# call and 0.11.2 still exports every transform openwakeword/data.py uses.
uvpip("torch-audiomentations==0.11.2")

uvpip("pronouncing==0.2.0", "datasets==2.14.6", "deep-phonemizer==0.0.19")

# tflite conversion. train.py runs it immediately after writing the .onnx, so
# a failure here costs nothing that matters: the file Ranger loads is already
# on disk. tensorflow 2.8.1 has no wheel past cp310, so this is expected to
# fail and step 6 says so.
tflite_ok = uvpip("tensorflow-cpu==2.8.1", "tensorflow_probability==0.16.0",
                  "onnx_tf==1.10.0", must_work=False)

# -- the same, into the notebook's own kernel ------------------------------
#
# The download and acceptance cells run here rather than in the 3.11 venv,
# because they only need to read audio and score a model.

run(sys.executable, "-m", "pip", "install", "-q", "-e", REPO, "--no-deps")
run(sys.executable, "-m", "pip", "install", "-q", "onnxruntime", "datasets==2.14.6")

# -- the feature models, which the wheel does not ship ---------------------

RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
target = os.path.join(REPO, "openwakeword", "resources", "models")
for feature in ["embedding_model.onnx", "embedding_model.tflite",
                "melspectrogram.onnx", "melspectrogram.tflite"]:
    fetch(RELEASE + feature, os.path.join(target, feature), at_least=100_000)

print()
print("Setup done. Run the preflight before downloading anything.")
if not tflite_ok:
    print("tflite conversion deps did not install, as expected. The .onnx is written first.")
"""


PREFLIGHT = """\
# Step 1b of 7. Preflight. Thirty seconds, and it is the cheapest cell here.
#
# Five separate incompatibilities in this notebook surfaced only at execution
# time, and three of them surfaced forty minutes into step 6. Every one was an
# import that could have been tried immediately.
#
# train.py imports torch, torchinfo, torchmetrics and openwakeword.data, which
# pulls in audiomentations, torch_audiomentations, speechbrain, torchaudio and
# acoustics. Any of those can be incompatible with the Colab image of the day.
# Import the whole chain now, in the Python 3.11 environment the trainer is
# launched with, before a single byte is downloaded.
#
# If this cell passes, step 6 will start. It cannot promise it will finish.

import os
import subprocess
import sys

PY = os.path.abspath("./py311/bin/python")

CHECKS = [
    ("the trainer and everything it imports", "import openwakeword.train"),
    ("the wake word model loader", "from openwakeword.model import Model"),
    ("the augmentation chain", "import openwakeword.data"),
    (
        # The exact name train.py imports, not the package around it.
        # rhasspy's repo has a piper_sample_generator package and no
        # generate_samples module, so importing the package would pass here and
        # step 6 would still fail. dscripka's fork has the module.
        "the sample generator (generate_samples, by that name)",
        "import sys; sys.path.insert(0, 'piper-sample-generator');"
        " from generate_samples import generate_samples",
    ),
]

failed = []
for label, statement in CHECKS:
    result = subprocess.run([PY, "-c", statement], capture_output=True, text=True)
    print(("ok    " if result.returncode == 0 else "FAIL  ") + label)
    if result.returncode != 0:
        failed.append(label)
        print(result.stderr[-1500:])

if failed:
    raise RuntimeError(
        f"{len(failed)} of {len(CHECKS)} preflight checks failed: {failed}. "
        "Fix these before downloading anything. The traceback above names the "
        "library and the call, which is usually a pin in step 1 that is older or "
        "newer than the Colab image."
    )

print()
print("Preflight clear. Everything step 6 imports is importable.")
"""


IMPORTS = """\
import os
import subprocess
import sys
import urllib.request
import uuid
from pathlib import Path

#: The training interpreter, in case this cell is the first one re-run after a
#: kernel restart. Everything that touches openwakeword's trainer goes through
#: it; the notebook's own kernel only downloads data and scores the result.
PY = os.path.abspath("./py311/bin/python")

import numpy as np
import scipy
import torch
import yaml
import datasets
from tqdm import tqdm


def fetch(url, path, at_least=1000):
    \"\"\"Download, and refuse a 404 written to disk as though it were a file.

    This is failure four. The AudioSet URL returned 404, wget saved the error
    body under the name it was given, tar found nothing in it, the conversion
    loop ran over an empty list, and the training config went on pointing at an
    empty directory. Every step reported success.
    \"\"\"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        urllib.request.urlretrieve(url, path)
    except Exception as exc:
        raise RuntimeError(f"{url} could not be downloaded: {exc}") from exc
    size = os.path.getsize(path)
    print(f"{path}: {size / 1e6:.1f} MB")
    if size < at_least:
        os.remove(path)
        raise RuntimeError(
            f"{url} returned {size} bytes, which is an error page and not a file."
        )
    return path


def produced(folder, at_least=1, what="files"):
    \"\"\"Fail loudly if a download or conversion step produced nothing.

    A step that silently writes no files is the expensive failure here: the
    training config goes on pointing at the empty directory, the run takes
    three hours anyway, and the model is trained on whatever data did arrive.
    Nothing downstream complains, because an empty directory is a valid
    directory.
    \"\"\"
    found = sorted(path for path in Path(folder).rglob("*") if path.is_file())
    print(f"{folder}: {len(found)} {what}")
    if len(found) < at_least:
        raise RuntimeError(
            f"{folder} has {len(found)} {what}, expected at least {at_least}. "
            "Do not continue: the step above did not do what it looked like it did."
        )
    return found
"""

RIRS = """\
# Step 2 of 7. Room impulse responses, so the synthetic speech sounds like it
# was said in a room rather than into a vacuum. A few minutes.

output_dir = "./mit_rirs"
os.makedirs(output_dir, exist_ok=True)
rir_dataset = datasets.load_dataset(
    "davidscripka/MIT_environmental_impulse_responses", split="train", streaming=True
)
for row in tqdm(rir_dataset):
    name = row["audio"]["path"].split("/")[-1]
    scipy.io.wavfile.write(
        os.path.join(output_dir, name), 16000, (row["audio"]["array"] * 32767).astype(np.int16)
    )

produced(output_dir, at_least=100, what="impulse responses")
"""

BACKGROUND = """\
# Step 3 of 7. Background audio: noise, speech and music the phrase gets mixed
# into. Ten to twenty minutes, mostly download.
#
# n_hours below is the dial. Upstream's notebook uses 1 as an example. More
# background is the cheapest way to reduce false fires, and 2 is a reasonable
# compromise against the free tier's session length.

n_hours = 2
ALLOW_MUSIC_ONLY = False  # see the AudioSet block below

# -- AudioSet --------------------------------------------------------------
#
# The .flac files are found by searching, not by a fixed path. Upstream's
# notebook globs audioset/audio/**/*.flac and that found nothing on this run:
# the tar extracted, and the conversion loop then ran over an empty list and
# wrote no files, silently. Whatever the tar's internal layout is, the files
# are somewhere under ./audioset, so look there and say what turned up.

fname = "bal_train09.tar"
link = "https://huggingface.co/datasets/agkphysics/AudioSet/resolve/main/data/" + fname
if not os.path.exists(f"audioset/{fname}"):
    try:
        fetch(link, f"audioset/{fname}", at_least=50_000_000)
    except RuntimeError as exc:
        print(exc)
        print()
        print("AudioSet is not reachable at that URL. This is survivable: it is one of")
        print("two background sources and the other is music. Expect somewhat more")
        print("false fires. To go on without it, set ALLOW_MUSIC_ONLY = True above and")
        print("re-run. To do better, point background_paths at any folder of ordinary")
        print("speech and noise you have.")
        if not globals().get("ALLOW_MUSIC_ONLY"):
            raise

if os.path.exists(f"audioset/{fname}"):
    run_tar = subprocess.run(["tar", "-xf", fname], cwd="audioset", capture_output=True, text=True)
    if run_tar.returncode != 0:
        raise RuntimeError(f"tar failed: {run_tar.stderr[-500:]}")

clips = [
    str(path)
    for path in Path("audioset").rglob("*")
    if path.suffix.lower() in {".flac", ".wav", ".mp3", ".ogg", ".m4a"}
]
if not clips:
    print("Nothing audio-shaped under ./audioset. What is there:")
    for path in sorted(Path("audioset").rglob("*"))[:40]:
        print("   ", path)
    if not ALLOW_MUSIC_ONLY:
        raise RuntimeError(
            "the AudioSet tar extracted no audio files this cell can find. Look at the "
            "listing above and widen the search, rather than continuing without them."
        )
print(f"found {len(clips)} AudioSet clips")

output_dir = "./audioset_16k"
os.makedirs(output_dir, exist_ok=True)
audioset_dataset = datasets.Dataset.from_dict({"audio": clips or []})
audioset_dataset = audioset_dataset.cast_column("audio", datasets.Audio(sampling_rate=16000))
for row in tqdm(audioset_dataset):
    name = Path(row["audio"]["path"]).stem + ".wav"
    scipy.io.wavfile.write(
        os.path.join(output_dir, name), 16000, (row["audio"]["array"] * 32767).astype(np.int16)
    )

if clips:
    produced(output_dir, at_least=100, what="converted AudioSet clips")

# -- Free Music Archive ----------------------------------------------------

output_dir = "./fma"
os.makedirs(output_dir, exist_ok=True)
fma_dataset = datasets.load_dataset("rudraml/fma", name="small", split="train", streaming=True)
fma_dataset = iter(fma_dataset.cast_column("audio", datasets.Audio(sampling_rate=16000)))
for _ in tqdm(range(n_hours * 3600 // 30)):  # every FMA clip is 30 seconds
    row = next(fma_dataset)
    name = Path(row["audio"]["path"]).stem + ".wav"
    scipy.io.wavfile.write(
        os.path.join(output_dir, name), 16000, (row["audio"]["array"] * 32767).astype(np.int16)
    )

produced(output_dir, at_least=n_hours * 3600 // 30 - 10, what="music clips")
"""


FEATURES = """\
# Step 4 of 7. Two precomputed feature files: ~2,000 hours of general audio to
# train against, and ~11 hours held out to count false fires. About 4 GB, five
# to ten minutes.

FEATURES_BASE = ("https://huggingface.co/datasets/davidscripka/openwakeword_features/"
                 "resolve/main/")
for feature_file, smallest in [
    ("openwakeword_features_ACAV100M_2000_hrs_16bit.npy", 1_000_000_000),
    ("validation_set_features.npy", 10_000_000),
]:
    if not os.path.exists(feature_file):
        fetch(FEATURES_BASE + feature_file, feature_file, at_least=smallest)

# wget writes whatever came back, including an error page, under the name it
# was given. Opening each as a memory-mapped array reads the header only, so
# this costs nothing and proves the file is what it claims to be.
for feature_file in ["openwakeword_features_ACAV100M_2000_hrs_16bit.npy",
                     "validation_set_features.npy"]:
    if not os.path.exists(feature_file):
        raise RuntimeError(f"{feature_file} did not download")
    array = np.load(feature_file, mmap_mode="r")
    print(f"{feature_file}: {array.shape} {array.dtype}, "
          f"{os.path.getsize(feature_file) / 1e9:.2f} GB")
    if array.shape[0] < 1000:
        raise RuntimeError(
            f"{feature_file} loaded but has only {array.shape[0]} rows, which is "
            "a truncated download. Delete it and run this cell again."
        )
"""


HOLDOUT = """\
# Step 7 of 7, part one. An hour of music the model has never seen. Five minutes.
#
# The audio downloaded in step 4 was mixed into the training negatives, so
# counting false fires against it flatters the model: it has already been
# trained not to fire on those exact clips. This continues the same stream past
# where step 4 stopped, so what comes out is genuinely held out.

import itertools

BACKGROUND_HOURS = 2  # must match n_hours in step 4
HOLDOUT_HOURS = 1

os.makedirs("./holdout_16k", exist_ok=True)
stream = datasets.load_dataset("rudraml/fma", name="small", split="train", streaming=True)
stream = stream.cast_column("audio", datasets.Audio(sampling_rate=16000))
already = BACKGROUND_HOURS * 3600 // 30
for row in tqdm(itertools.islice(iter(stream), already, already + HOLDOUT_HOURS * 3600 // 30)):
    name = row["audio"]["path"].split("/")[-1].replace(".mp3", ".wav")
    scipy.io.wavfile.write(
        os.path.join("./holdout_16k", name), 16000, (row["audio"]["array"] * 32767).astype(np.int16)
    )

produced("./holdout_16k", at_least=HOLDOUT_HOURS * 3600 // 30 - 10, what="held out clips")
"""


def config_cell(phrase: str, name: str, negatives: list[str]) -> str:
    negative_lines = "\n".join(f"    {word!r}," for word in negatives)
    return f'''\
# Step 5 of 7. The training config. Seconds.
#
# Read from openWakeWord's own example config inside the clone and then
# overridden, rather than written from scratch. A config written from scratch
# drifts from whatever version was cloned, and it drifts silently: an unread
# key looks exactly like a key with no effect.

config = yaml.safe_load(open("openwakeword/examples/custom_model.yml").read())

config["target_phrase"] = [{phrase!r}]
config["model_name"] = {name!r}
config["output_dir"] = "./{name}"

# Phrases close enough to {phrase!r} to be worth naming. openWakeWord already
# derives adversarial negatives from phoneme overlap; this is the layer on top
# that knows the phrase belongs to someone who says "range", "ranch" and
# "rangers" in ordinary conversation.
config["custom_negative_phrases"] = [
{negative_lines}
]

# openWakeWord's own comment calls 20,000 the minimum for a real model. Its
# example notebook uses 1,000, which demonstrates the mechanism and produces
# nothing anyone should arm. Raising these raises generation time roughly
# linearly: 20,000 is on the order of an hour.
config["n_samples"] = {SAMPLES}
config["n_samples_val"] = {SAMPLES_VAL}
config["steps"] = {STEPS}

config["background_paths"] = ["./audioset_16k", "./fma"]
config["false_positive_validation_data_path"] = "validation_set_features.npy"
config["feature_data_files"] = {{
    "ACAV100M_sample": "openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
}}

# Every path the config points at, checked before anything is written. This is
# the backstop: an empty directory is a valid directory, so a step that
# silently produced nothing reaches the trainer as a config pointing at
# nothing, and the run takes three hours anyway and trains on what did arrive.
for key in ["rir_paths", "background_paths"]:
    for folder in config[key]:
        produced(folder, at_least=1, what=f"files for {{key}}")

for label, feature_file in list(config["feature_data_files"].items()) + [
    ("false_positive_validation", config["false_positive_validation_data_path"])
]:
    if not os.path.exists(feature_file):
        raise RuntimeError(f"{{label}} points at {{feature_file}}, which does not exist")
    print(f"{{label}}: {{os.path.getsize(feature_file) / 1e9:.2f}} GB")

if not os.path.exists(os.path.join(config["piper_sample_generator_path"], "models")):
    raise RuntimeError(
        f"no piper sample generator at {{config['piper_sample_generator_path']}}. "
        "Step 1 did not finish."
    )

with open("{name}.yaml", "w") as handle:
    yaml.dump(config, handle)

print()
print("phrase:      ", config["target_phrase"])
print("negatives:   ", len(config["custom_negative_phrases"]))
print("samples:     ", config["n_samples"])
print("target FP/hr:", config["target_false_positives_per_hour"])
'''


def generate_cell(name: str) -> str:
    return f'''\
# Step 6 of 7, part one. Generate the synthetic speech. The long one: on the
# order of an hour for {SAMPLES} samples on a free T4.
#
# If it dies part way, run it again. It counts what is already on disk and
# carries on from there, so nothing is wasted.

!{{PY}} openwakeword/openwakeword/train.py --training_config {name}.yaml --generate_clips
'''


def augment_cell(name: str) -> str:
    return f'''\
# Step 6 of 7, part two. Play every clip through a room and mix in noise, then
# turn it all into features. Fifteen to thirty minutes.

!{{PY}} openwakeword/openwakeword/train.py --training_config {name}.yaml --augment_clips
'''


def train_cell(name: str) -> str:
    return f'''\
# Step 6 of 7, part three. Train. Twenty to forty minutes.
#
# Watch the false positives per hour figure it prints as it goes. That is the
# number the whole run is optimising, and it is the number that decides whether
# this is usable.

!{{PY}} openwakeword/openwakeword/train.py --training_config {name}.yaml --train_model
'''


def verify_cell(name: str, phrase: str) -> str:
    return f'''\
# Step 7 of 7, part two. Does it work? Not upstream's cell. The acceptance test.
#
# Two measurements at every threshold:
#
#   detected   - the fraction of held out clips of {phrase!r} it fires on.
#                These are synthetic and a different voice from yours, so treat
#                this as an upper bound rather than a promise.
#   false/hr   - fires per hour across the held out music from part one.
#                Nothing in it says {phrase!r}, so every one of these is wrong,
#                and the model has never been trained against these clips.
#
# Pick the lowest threshold whose false/hr you can live with. Anything under
# about 0.5 per hour is liveable; above 2 per hour it will interrupt you often
# enough that you turn it off.

import numpy as np, scipy.io.wavfile
from pathlib import Path
from openwakeword.model import Model

MODEL = f"./{name}/{name}.onnx"
THRESHOLDS = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
REFRACTORY = 20  # frames (1.6s) before the same fire can count twice

model = Model(wakeword_models=[MODEL], inference_framework="onnx")


def scores_for(path):
    """Every frame score for one wav, with the model reset first."""
    model.reset()
    rate, data = scipy.io.wavfile.read(path)
    if data.ndim > 1:
        data = data[:, 0]
    out = []
    for start in range(0, len(data) - 1280, 1280):
        frame = data[start:start + 1280].astype(np.int16)
        out.append(max(model.predict(frame).values()))
    return np.array(out), len(data) / rate


# The trainer nests its output: output_dir/model_name/positive_test, and the
# exported model at output_dir/model_name.onnx. Not the same level.
positives = sorted(Path("./{name}/{name}/positive_test").glob("*.wav"))
background = sorted(Path("./holdout_16k").glob("*.wav"))
assert positives, "no held out positives found. Did step 6 part one finish?"
assert background, "no held out background found. Run step 7 part one first."
print(f"{{len(positives)}} held out positives, {{len(background)}} held out background clips")

positive_peaks = []
for path in positives[:500]:
    peaks, _ = scores_for(path)
    positive_peaks.append(peaks.max() if len(peaks) else 0.0)
positive_peaks = np.array(positive_peaks)

false_fires = {{threshold: 0 for threshold in THRESHOLDS}}
background_seconds = 0.0
for path in background:
    peaks, seconds = scores_for(path)
    background_seconds += seconds
    for threshold in THRESHOLDS:
        above = peaks > threshold
        last = -REFRACTORY
        for index in np.flatnonzero(above):
            if index - last >= REFRACTORY:
                false_fires[threshold] += 1
                last = index

hours = background_seconds / 3600
print(f"\\nmeasured over {{hours:.2f}} hours of background audio\\n")
print(f"{{'threshold':>10}}  {{'detected':>9}}  {{'false/hr':>9}}")
for threshold in THRESHOLDS:
    detected = (positive_peaks > threshold).mean()
    print(f"{{threshold:>10.1f}}  {{detected:>8.0%}}  {{false_fires[threshold] / hours:>9.2f}}")
'''


def download_cell(name: str) -> str:
    return f'''\
# Download it. This is the file that goes on your machine.

from google.colab import files
files.download(f"./{name}/{name}.onnx")
'''


def notebook_for(phrase: str, name: str, negatives: list[str]) -> dict:
    cells = [
        _markdown(
            f"# Training `{phrase}` for Ranger\n"
            "\n"
            "openWakeWord's own `automatic_model_training.ipynb`, with the config cell\n"
            f"rewritten for `{phrase}` and one cell added at the end that measures the\n"
            "result. Every dataset download is upstream's.\n"
            "\n"
            "**Before anything else: Runtime > Change runtime type > T4 GPU.** The first\n"
            "cell checks and will tell you if you forgot.\n"
            "\n"
            "Run the cells in order. Expect two to three hours end to end, most of it in\n"
            "step 6. Nothing here needs watching except the last cell, which is the one\n"
            "that decides whether the model is worth using."
        ),
        _code(SETUP),
        _code(PREFLIGHT),
        _code(IMPORTS),
        _code(RIRS),
        _code(BACKGROUND),
        _code(FEATURES),
        _code(config_cell(phrase, name, negatives)),
        _code(generate_cell(name)),
        _code(augment_cell(name)),
        _code(train_cell(name)),
        _code(HOLDOUT),
        _code(verify_cell(name, phrase)),
        _code(download_cell(name)),
        _markdown(
            f"`{name}.onnx` is now in your downloads. Put it next to the vault, at\n"
            f"`<vault>/Ranger/models/{name}.onnx`, then in `ranger.local.toml`:\n"
            "\n"
            "```toml\n"
            "[wake]\n"
            "enabled = true\n"
            f'phrase = "{phrase}"\n'
            f'model = "C:/path/to/vault/Ranger/models/{name}.onnx"\n'
            "threshold = 0.6\n"
            "```\n"
            "\n"
            "Set `threshold` from the table in step 7, not from this default. Then\n"
            "`ranger doctor`, which will tell you if it cannot find the file.\n"
            "\n"
            "`ranger wake install` does not need re-running: it fetches the two feature\n"
            "models underneath the hotword, and a trained hotword replaces the phrase,\n"
            "not the models it runs on."
        ),
    ]
    return {
        "cells": cells,
        "metadata": {
            "accelerator": "GPU",
            "colab": {"provenance": []},
            "kernelspec": {"display_name": "Python 3", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--phrase", default="hey ranger", help="two words, lower case")
    parser.add_argument(
        "--out", default=None, help="where to write the notebook. Defaults to <phrase>_training.ipynb"
    )
    args = parser.parse_args(argv)

    phrase = " ".join(args.phrase.strip().lower().split())
    if len(phrase.split()) < 2:
        print(
            f"{phrase!r} is one word. A single common word fires constantly, and "
            "Ranger's config refuses one.",
            file=sys.stderr,
        )
        return 2

    name = phrase.replace(" ", "_")
    negatives = NEAR_MISSES.get(phrase, [])
    output = args.out or f"{name}_training.ipynb"

    with open(output, "w", encoding="utf-8") as handle:
        json.dump(notebook_for(phrase, name, negatives), handle, indent=1)
        handle.write("\n")

    print(f"wrote {output}")
    print()
    if not negatives:
        print(
            f"No adversarial negatives are written for {phrase!r}. Add words that sound "
            "like it to custom_negative_phrases in the config cell before running, or it "
            "will fire on all of them."
        )
        print()
    print("Next:")
    print("  1. Open https://colab.research.google.com and upload this file")
    print("  2. Runtime > Change runtime type > T4 GPU")
    print("  3. Run every cell in order. Two to three hours, mostly unattended")
    print(f"  4. The last cells print a threshold table and download {name}.onnx")
    print()
    print("See docs/training-hey-ranger.md for what the numbers mean and what a bad")
    print("model looks like.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
