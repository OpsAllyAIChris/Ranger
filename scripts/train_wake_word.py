#!/usr/bin/env python
"""Write the training config for a "hey ranger" model, and say what to run.

openWakeWord publishes six phrases and "hey ranger" is not one of them, so it
has to be trained. This script does not train it. openWakeWord's own trainer
does, driven by a YAML config, and what was missing was a correct config for
Ranger's phrase and an honest account of what the training run needs. That is
what this writes.

    python scripts/train_wake_word.py --phrase "hey ranger" --out hey_ranger.yaml

Training is synthetic. A text to speech model generates thousands of spoken
variations of the phrase, a large corpus of ordinary speech and noise provides
the negatives, and room impulse responses make both sound like they were said
in a room. No recording of the operator's own voice is involved and none is
needed.

**It does not run usefully on a laptop.** It wants a CUDA GPU and takes on the
order of an hour, and it needs three things that are not pip installable: a
clone of the piper sample generator, a set of room impulse responses, and hours
of background audio. openWakeWord's own documentation assumes Colab, so this
config is written for the paths Colab ends up with.

The full sequence, on a Colab notebook with a T4:

    !git clone https://github.com/rhasspy/piper-sample-generator
    !wget -O piper-sample-generator/models/en_US-libritts_r-medium.pt \\
        https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt
    !pip install openwakeword piper-phonemize webrtcvad mutagen torchinfo torchmetrics speechbrain audiomentations
    # room impulse responses and background audio, per openWakeWord's notebook
    !python -m openwakeword.train --training_config hey_ranger.yaml \\
        --generate_clips --augment_clips --train_model

The run writes `hey_ranger.onnx`. Copy it back to the machine, put it next to
the vault, and point config at it:

    [wake]
    phrase = "hey ranger"
    model = "C:/Users/you/Obsidian/Ranger-Vault/Ranger/models/hey_ranger.onnx"

The feature models still come from `ranger wake install`; a trained hotword
replaces the published one, not the two models underneath it.

One honest warning, repeated from the design conversation. "Hey Ranger" is two
ordinary English words. The phrases these models do well on, like "alexa", were
chosen for phonetic rarity. Expect noticeably more false fires than the
published "hey jarvis", and judge the real rate only after the real phrase is
in. The `target_false_positives_per_hour` setting below is the dial for that:
lower is quieter and misses more.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

#: Words that sound enough like the phrase to be worth training against
#: explicitly. openWakeWord calls these adversarial negatives, and they are the
#: difference between a model that fires on "hey ranger" and one that fires on
#: anything with the same shape.
NEAR_MISSES = {
    "hey ranger": [
        "hey stranger",
        "hey danger",
        "a ranger",
        "the ranger",
        "rangers",
        "hey rain",
        "hey ranch",
        "hey range",
        "range rover",
        "hey rachel",
    ]
}


def config_for(phrase: str, name: str, false_positives_per_hour: float) -> str:
    """The YAML openWakeWord's trainer reads. Keys are its own, not Ranger's."""
    negatives = NEAR_MISSES.get(phrase.lower(), [])
    negative_lines = "\n".join(f"  - {word!r}" for word in negatives) or "  []"

    return f"""\
# openWakeWord training config for Ranger, written by scripts/train_wake_word.py.
# Run it with:
#   python -m openwakeword.train --training_config {name}.yaml \\
#       --generate_clips --augment_clips --train_model

target_phrase:
  - {phrase!r}
model_name: {name}
model_type: dnn
layer_size: 32

# Recomputed from the real clip lengths when --augment_clips runs. Set here so
# a training-only run has something to read.
total_length: 32000

# Synthetic speech. More is better and slower; these are the numbers
# openWakeWord's own notebook uses for a phrase of this length.
n_samples: 20000
n_samples_val: 2000
tts_batch_size: 50
custom_negative_phrases:
{negative_lines}

# Augmentation: every clip is replayed through a room and mixed with noise.
augmentation_rounds: 1
augmentation_batch_size: 16

# Paths that are not pip installable and must exist before the run. These are
# where they land on Colab following openWakeWord's notebook.
piper_sample_generator_path: ./piper-sample-generator
rir_paths:
  - ./mit_rirs
background_paths:
  - ./audioset_16k
  - ./fma_16k
background_paths_duplication_rate:
  - 1
  - 1

# Precomputed negative features from openWakeWord's released training data, and
# the held out set false fires are counted against.
feature_data_files:
  ACAV100M_sample: ./openwakeword_features_ACAV100M_2000_hrs_16bit.npy
false_positive_validation_data_path: ./validation_set_features.npy
batch_n_per_class:
  ACAV100M_sample: 1024
  adversarial_negative: 50
  positive: 50

steps: 50000
max_negative_weight: 1500

# The dial for how talkative it is. Lower is quieter and misses more.
target_false_positives_per_hour: {false_positives_per_hour}

output_dir: ./{name}
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--phrase", default="hey ranger", help="two words, lower case")
    parser.add_argument("--out", default="hey_ranger.yaml", help="where to write the config")
    parser.add_argument(
        "--false-positives-per-hour",
        type=float,
        default=0.2,
        help="how talkative to allow it to be. Lower is quieter and misses more",
    )
    args = parser.parse_args(argv)

    phrase = args.phrase.strip().lower()
    if len(phrase.split()) < 2:
        print(
            f"{phrase!r} is one word. A single common word is a wake phrase that fires "
            "constantly, and Ranger's config refuses one.",
            file=sys.stderr,
        )
        return 2

    output = Path(args.out)
    name = output.stem
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        config_for(phrase, name, args.false_positives_per_hour), encoding="utf-8"
    )

    print(f"wrote {output}")
    if phrase not in NEAR_MISSES:
        print(
            f"\nNote: there are no adversarial negatives written for {phrase!r}. "
            "Add words that sound like it under custom_negative_phrases before "
            "training, or it will fire on all of them."
        )
    print("\nOn a Colab notebook with a GPU:")
    print("  !git clone https://github.com/rhasspy/piper-sample-generator")
    print("  !pip install openwakeword piper-phonemize webrtcvad mutagen \\")
    print("      torchinfo torchmetrics speechbrain audiomentations")
    print("  # then the impulse responses and background audio, per openWakeWord's notebook")
    print(f"  !python -m openwakeword.train --training_config {output.name} \\")
    print("      --generate_clips --augment_clips --train_model")
    print(f"\nIt writes {name}.onnx. Copy that back, then in ranger.local.toml:")
    print("\n[wake]")
    print("enabled = true")
    print(f'phrase = "{phrase}"')
    print(f'model = "<full path to>/{name}.onnx"')
    print("\nStart at threshold 0.6 and raise it if it fires when you did not speak.")
    print("The feature models still come from 'ranger wake install'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
