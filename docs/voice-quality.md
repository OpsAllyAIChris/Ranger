# Slurring: what is actually being sent, and what is in the way

Written before changing any default, because "it sounds bad" has four
candidate causes and three of them are cheap to rule out.

## 1. What the request contains

Every call sends exactly this, and sent nothing else before today:

```json
{"model_id": "eleven_flash_v2_5",
 "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "speed": 1.0}}
```

with `output_format=pcm_24000` as a query parameter.

**No `style`. No `use_speaker_boost`.** Not disabled — absent, so ElevenLabs
applies its own defaults for them. Both are now config keys and both are still
sent only when set, so an untouched config makes the request it always made.
`tests/test_tts.py::test_the_voice_settings_that_are_sent` is the record.

`stability: 0.5` is the middle of the range, not the bottom, but it is inside
the band where a latency-optimised model slurs. It is the first thing to try
moving, and it is a knob to hear rather than to reason about: 0 is the most
expressive and the most variable, 1 the most consistent and the flattest.

## 2. What happens to the audio between ElevenLabs and the speaker

**Nothing reprocesses it, and the smoothing preset is not in the audio path at
all.**

`natural` is one of three pairs of time constants — attack 45ms, release 220ms
— in `orb.js`. They smooth a single number between 0 and 1 that a shader reads
as brightness. The orb never sees an `AudioBuffer`, a sample, or a rate; a test
asserts the scene file contains no audio API at all. It cannot affect
articulation, and suspecting it was reasonable.

What the audio does pass through:

- **`pcmBuffer`**: raw 16-bit little-endian samples are copied into an
  `AudioBuffer` declared at 24000Hz. No filtering, no gain, no interpolation —
  a divide by 32768 per sample.
- **One resample, by the browser.** The `AudioContext` runs at the device rate,
  usually 48000, and Web Audio resamples the 24k buffer on playback. That is a
  high-quality documented conversion and an unlikely cause of slurring, but it
  is a resample and it is the only one.
- **Scheduling**: each sentence is queued at `max(now, playhead)`, so sentences
  butt up against each other exactly.

That last point is the one worth knowing about. **Jarvis synthesises one
sentence per request**, as the model produces them, and each request is
independent: ElevenLabs has no idea what came before or after, so prosody
restarts at every sentence and the joins are abrupt. ElevenLabs accepts
`previous_text` and `next_text` for exactly this. We send neither. That is a
real, fixable cause of "run together", and it is deliberately **not** changed
yet — it alters what every spoken turn sends, and the model comparison should
happen first so the two changes are not entangled.

## 3. Hearing them, without a rebuild

Every voice setting is already overridable in `ranger.local.toml` (the local
file is merged over `ranger.toml` and every `[tts]` key is a known key), so a
long-term choice goes there. For comparing, the command line is faster:

```
ranger say "Rod asked for the numbers by Thursday, and Illes wants the film pricing." \
  --compare eleven_flash_v2_5,eleven_turbo_v2_5,eleven_multilingual_v2
```

The same sentence, through each model in turn, each announced before it speaks.
Then the settings, one at a time:

```
ranger say "..." --stability 0.75
ranger say "..." --stability 0.75 --speaker-boost
ranger say "..." --model eleven_turbo_v2_5 --stability 0.7 --style 0.15
ranger say "..." --speed 0.95            # slower, if it is running words together
```

Every run prints the settings it used and the time to first chunk, so a
recording can be matched to what produced it and the latency cost of each
choice is visible. `--keep out.wav` saves the audio for a side-by-side.

**Latency is the thing being traded.** Flash is the fastest and the least
articulate; Turbo is close and steadier; Multilingual v2 is the most articulate
and clearly slower to first sound. With push-to-talk the wait is felt on every
reply, which is why Flash was the default — but a reply that has to be repeated
costs more than the milliseconds saved.

## What to change, in order

1. `--stability 0.7` on the current model. Cheapest possible fix.
2. `--compare` the three models on one sentence.
3. `--speaker-boost`, if the voice is a clone.
4. Only then, `previous_text`/`next_text` across sentence boundaries, which is
   a change to the speech path rather than a setting.

Nothing above changes a default. When you have heard them, the winner goes in
`ranger.local.toml` and the reasoning goes in `ranger.toml` beside the key.

## Numbers are spoken as words

`$292,187` sent to ElevenLabs as digits is a guess it has to make — a price, a
part number, a year — and on a long figure it slurs the middle. Sent as
"two hundred ninety-two thousand one hundred eighty-seven dollars" there is
nothing left to guess at.

### Where the split lives

`ranger/saying.py`, applied in `ElevenLabsSpeaker.stream` and **nowhere else**.
That is the one place in the project where text becomes audio, so the transform
cannot reach anything that gets kept:

- the window shows the model's own words, with the digits in them;
- a filed note, a draft, a document and a GP entry are all written from the
  model's words or from Python's figures;
- the figure audit compares the reply's digits against what Python computed, and
  runs on the written reply.

**Digits on the screen, words in the ear.** A test asserts that `tts.py` is the
only module in the project that imports `saying`, because spelled-out numbers in
a filed note would be the exact opposite of what the figure audit exists for.

### What it says

| written | spoken |
| --- | --- |
| `$292,187` | two hundred ninety-two thousand one hundred eighty-seven dollars |
| `$12.50` | twelve dollars fifty cents |
| `$300.00` | three hundred dollars (no "zero cents") |
| `12.5%` | twelve point five percent |
| `2026-09-09` | September ninth, twenty twenty-six |
| `2026-08` | August twenty twenty-six |
| `14:30` / `9:05` | fourteen thirty / nine oh five |
| `07700900123` | zero seven seven zero zero nine zero zero one two three |
| `3.14` | three point one four |

A decimal that is not money reads digit by digit after the point, because
"point five one" and "point fifty-one" are different numbers to a listener and
only one of them is what was written.

### Mixed number and jargon

- **`3,000 MOQ` → "three thousand MOQ".** The number is spelled out; the word is
  left exactly as written. Guessing that MOQ should be "M O Q" is the same class
  of mistake as guessing at the digits, and it is not one this can make from
  three letters.
- **`60/40 terms` → "sixty forty terms"**, which is what it is called out loud.
- **`SKU-4471` → "SKU four four seven one".** A token with letters and digits in
  it is a reference, not a quantity, and a person reads one digit by digit. The
  parts are spaced, because `Q3` said as one word comes out "Qthree".
- **`Tier 1` → "Tier one".** The space is what tells a quantity from an
  identifier.
- A run of seven or more unseparated digits is read out rather than counted: an
  account number, an order number, a phone number. **A four-digit extension is
  ambiguous and is read as a quantity** — that one is a judgement call and it may
  be wrong for you.

## Sentence context

Each sentence is its own request, so without context prosody restarts at every
boundary and a sentence opening with a figure starts cold.

`previous_text` goes with every sentence after the first. `next_text` goes only
when it is already known — one chunk of the model's output often yields several
sentences at once, and then the following one is in hand for free. **Forcing it
would mean holding each sentence back until the next arrived**, adding a
sentence of latency to every reply, which is the thing speaking sentence by
sentence exists to remove. The last sentence of a reply correctly has nothing
after it.

Both are sent through the same number transform. Handing the model `$292,187` as
the run-up to a sentence it is being asked to read as words would tell it two
different things about how the same figure sounds.
