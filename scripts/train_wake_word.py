#!/usr/bin/env python
"""Train a "hey ranger" model for openWakeWord.

openWakeWord ships a small fixed set of phrases and "hey ranger" is not among
them, so it has to be trained. Training is synthetic: a text to speech model
generates thousands of spoken variations of the phrase, and a large corpus of
ordinary speech and noise provides the negatives. No recording of the
operator's own voice is involved and none is needed.

**This does not run usefully on a laptop.** It wants a GPU and takes on the
order of an hour. Run it on Colab, which is what openWakeWord's own
documentation assumes, and copy the resulting .onnx back.

    Runtime, Change runtime type, T4 GPU
    !pip install openwakeword[full] piper-phonemize
    %run train_wake_word.py --phrase "hey ranger" --out hey_ranger.onnx

Then put the file next to the vault and point config at it:

    [wake]
    phrase = "hey ranger"
    model = "C:/Users/you/Obsidian/Ranger-Vault/Ranger/models/hey_ranger.onnx"

One honest warning, repeated from the design conversation. "Hey Ranger" is two
ordinary English words. The phrases these models do well on, like "alexa", were
chosen for phonetic rarity. Expect noticeably more false fires than the
bundled "hey jarvis", and judge the real rate only after the real phrase is in.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_STEPS = 50_000


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--phrase", default="hey ranger", help="two words, lower case")
    parser.add_argument("--out", default="hey_ranger.onnx", help="where to write the model")
    parser.add_argument("--samples", type=int, default=20_000, help="synthetic positives")
    parser.add_argument("--steps", type=int, default=DEFAULT_STEPS, help="training steps")
    parser.add_argument(
        "--false-activation-penalty",
        type=float,
        default=1500.0,
        help="how much a false fire costs relative to a miss. Higher is quieter",
    )
    args = parser.parse_args(argv)

    if len(args.phrase.split()) < 2:
        print(
            f"{args.phrase!r} is one word. A single common word is a wake phrase that "
            "fires constantly, and Ranger's config refuses one.",
            file=sys.stderr,
        )
        return 2

    try:
        from openwakeword.train import train_model
        from openwakeword.utils import generate_samples
    except ImportError as exc:
        print(
            f"{exc}. This needs the full training extras, which are not part of "
            'Ranger\'s "wake" extra because they only make sense on a GPU:\n'
            "    pip install openwakeword[full] piper-phonemize",
            file=sys.stderr,
        )
        return 1

    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)

    print(f"generating {args.samples} spoken variations of {args.phrase!r}")
    positives = generate_samples(
        text=[args.phrase],
        n_samples=args.samples,
        batch_size=100,
    )

    print(f"training, {args.steps} steps, false activation penalty {args.false_activation_penalty}")
    train_model(
        positive_samples=positives,
        model_name=output.stem,
        output_dir=str(output.parent),
        steps=args.steps,
        false_activation_penalty=args.false_activation_penalty,
    )

    print(f"\nwrote {output}")
    print("Copy it to your machine, then in ranger.local.toml:")
    print("\n[wake]")
    print(f'phrase = "{args.phrase}"')
    print(f'model = "<full path to>/{output.name}"')
    print("\nStart at threshold 0.6 and raise it if it fires when you did not speak.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
