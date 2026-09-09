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
