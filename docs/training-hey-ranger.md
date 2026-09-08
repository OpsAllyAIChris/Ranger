# Training "hey ranger"

openWakeWord publishes six phrases. "hey ranger" is not one of them, so it has
to be trained. This is the whole sequence, start to finish, with nothing left
to ask about in the middle.

Training is synthetic. A text to speech model generates tens of thousands of
spoken variations of the phrase in many voices, an hour of music and general
audio provides the negatives, and room impulse responses make both sound like
they were said in a room. **No recording of your own voice is involved and none
is needed.**

Budget two to three hours, almost all of it unattended.

---

## 1. Locally: write the notebook

```powershell
python scripts/train_wake_word.py
```

It writes **`hey_ranger_training.ipynb`** into whatever directory you ran it
from — the repository root, if you did nothing clever. That is the only local
step. Nothing is trained on your machine and nothing can be: openWakeWord's
automated training runs on Linux only, because Piper, the text to speech
library it generates samples with, does not work on Windows.

## 2. In Colab: which notebook

It is **openWakeWord's own** `automatic_model_training.ipynb`, with three
changes, all in one cell:

- the phrase is `hey ranger`
- thirteen adversarial negatives written for that phrase
- sample counts raised to openWakeWord's own recommended minimum, which its
  example notebook sits below because it is demonstrating the mechanism rather
  than producing a model anyone will arm

Plus one cell at the end that is not upstream's at all: the acceptance test in
step 6 below.

Every dataset download is upstream's, unedited. The training config is read
from openWakeWord's `examples/custom_model.yml` **inside the clone** and then
overridden, rather than written out from scratch — a config written from
scratch drifts from whatever version the notebook clones, and it drifts
silently, as a key that is quietly never read.

Go to [colab.research.google.com](https://colab.research.google.com), upload
the file, and then **Runtime > Change runtime type > T4 GPU** before running
anything. The first cell checks and tells you if you forgot.

## 3. What to upload and what the cells do

Only the notebook. Everything else downloads itself.

| cell | what it does | roughly |
| --- | --- | --- |
| 1. Environment | Clones piper-sample-generator and openwakeword, installs the training dependencies, fetches the two feature models | 10 min |
| 2. Imports | — | seconds |
| 3. Room impulse responses | MIT's reverb survey, so the synthetic speech sounds like a room | 3 min |
| 4. Background audio | AudioSet + Free Music Archive, 2 hours. `n_hours` is a dial | 15 min |
| 5. Precomputed features | ~2,000 hours of general audio to train against, ~11 hours held out to count false fires. About 4 GB | 10 min |
| 6. Config | Loads upstream's config, applies Ranger's phrase and negatives | seconds |
| 7. Generate | Piper speaks "hey ranger" 20,000 times, and the adversarial negatives too. **The long one** | ~1 hour |
| 8. Augment | Plays every clip through a room, mixes in noise, computes features | 15–30 min |
| 9. Train | 50,000 steps | 20–40 min |
| 10. Acceptance test | Scores the model. Not upstream's | 5–10 min |
| 11. Download | Puts the .onnx in your downloads | seconds |

**If cell 7 dies part way through, run it again.** It counts the clips already
on disk and carries on. Nothing is wasted. Same for the downloads.

### What the free Colab tier gives you

You do not need paid compute for this. Two to three hours on a T4 is well
inside what free Colab hands out, and none of it is near the 16 GB the T4 has.

Be plain about the risk, though: it is not the compute, it is the session.

- Free Colab gives a **T4 when one is available**, and "available" is not a
  promise. At busy times you may be told no GPU is free and have to come back.
- Free sessions **disconnect when idle** and have a **maximum lifetime**. Google
  tunes both and does not publish exact numbers, so treat anything specific you
  read, including from me, as approximate.
- Heavy use in one day can mean you are refused a GPU later that day.

The practical consequence is that a disconnect during the one hour generation
step is the likeliest thing to go wrong, and the fix is to re-run the cell,
because it resumes. Leave the tab open and visible; a background tab is more
likely to be reaped.

If you get refused a GPU twice in a row, Colab Pro is about ten dollars for a
month and you would need it for one afternoon. That is the only point at which
paying is worth considering, and you will know before you have spent anything.

## 4. What comes back

**`hey_ranger.onnx`**, about 1.2 MB, downloaded by the last cell.

Put it next to the vault:

```
<your vault>\Ranger\models\hey_ranger.onnx
```

Not in the repository, and not in the venv. Model files are not source, and the
venv gets rebuilt.

## 5. What changes in `ranger.local.toml`

```toml
[wake]
enabled = true
phrase = "hey ranger"
model = "C:/Users/you/Obsidian/Ranger-Vault/Ranger/models/hey_ranger.onnx"
threshold = 0.6
```

Forward slashes, or doubled backslashes. **Set `threshold` from the table the
acceptance test prints, not from that 0.6** — it is a placeholder.

Then:

```powershell
ranger doctor     # says so if it cannot find the file
ranger stop
ranger ui
```

- **Restart the server: yes.** Config is read at startup and the hotword is
  built from it.
- **Re-run `ranger wake install`: no.** That fetches the two feature models
  underneath the hotword. A trained hotword replaces the phrase, not the models
  it runs on. If you rebuild the venv, you will need it again — but that is
  true regardless of training.
- **Re-run `pip install`: no.** Nothing about the dependencies changed.

## 6. Knowing it worked before trusting it around customers

The last notebook cell prints this, over two hours of music and noise that
contains no instance of the phrase:

```
 threshold   detected   false/hr
       0.3        99%       8.40
       0.4        98%       2.10
       0.5        96%       0.60
       0.6        93%       0.25
       0.7        84%       0.05
       0.8        61%       0.00
       0.9        22%       0.00
```

That shape — high detection holding flat across several thresholds while false
fires fall away — is what a good model looks like. Pick the lowest threshold
whose `false/hr` you can live with. **Under about 0.5 per hour is liveable.
Above 2 per hour you will turn the feature off within a week.**

Then three checks on your own machine, in this order, before it goes anywhere
near a customer call:

1. **Arm it and say nothing for ten minutes.** `ranger log` afterwards. Any
   fire in that window is a fire on silence and room tone, which is the worst
   kind. If there are any, raise the threshold by 0.1 and repeat.
2. **Arm it and read something aloud for ten minutes** — an email, an account
   note, anything that is not the phrase. Same check. This is the test that
   matters, because it is the only one using audio that sounds like your voice
   in your room.
3. **Say "hey ranger" ten times, normally, from where you actually sit.** Fewer
   than eight fires means the threshold is too high for your voice and your
   microphone, whatever the table said.

Only then take it into a call. Note that the mic check refuses to arm while
Teams holds the microphone, so "in a call" mostly means "armed before the call
starts", and the check disarms it when the call begins.

---

## Telling a bad model apart from a weak phrase

You asked for this to be plain, so here it is. These are different problems
with different fixes, and the symptoms overlap enough to waste an afternoon.

**Trained badly.** Retraining fixes it.

- **Detection is low on its own held-out clips.** Under about 85% at threshold
  0.5 in the table means the run failed. Those clips are the easiest possible
  test: same text to speech, same augmentation, no room, no you. If it cannot
  find the phrase there, nothing else matters.
- **Detection falls off a cliff.** 95% at 0.3, 40% at 0.5, 5% at 0.7 means the
  scores are all bunched just above the line and the model is barely separating
  anything. A good model has a plateau, not a slope.
- **No threshold works at all.** If `false/hr` is still above 5 at the lowest
  threshold where detection is usable, there is nothing to tune.
- **It fires on silence.** Not on speech — on room tone, in check 1 above. That
  means it latched onto an artefact of the augmentation rather than the phrase.

The usual cause of all four is not enough synthetic data. The notebook uses
20,000; openWakeWord's own comment says 100,000 is often best. Raise
`n_samples`, accept the longer generation, run it again.

**The phrase is inherently weak.** Retraining does not fix it, and you will
know because:

- **The table looks good and real life does not.** This is the signature. The
  background corpus is music and general audio; it is not you talking about
  ranches and ranges and rangers. A model can score 0.25 false fires per hour
  against music and still fire four times an hour against your actual working
  day. **The gap between the table and check 2 is the phrase, not the run.**
- **False fires cluster on identifiable words.** `ranger log` records every
  fire including the discarded ones with no speech after them. If they cluster
  on "arrange", "stranger", "rangers", "range rover" — that is phonetic
  overlap. Add those to `custom_negative_phrases` and retrain once. If they
  survive that, you have found the ceiling.
- **Raising the threshold trades badly.** Bad training: +0.1 on the threshold
  kills the false fires and costs a few percent of detection. Weak phrase: you
  have to go so high that you say it twice every time.

**What I actually expect from "hey ranger."** Worse than `hey jarvis`, which is
phonetically rare and was chosen to be. openWakeWord's published models target
0.2 false fires per hour. "Hey" is a common attention word and "ranger" is an
ordinary noun you say in the course of your job, so I would expect somewhere
between 0.5 and 3 per hour in a working office, and I want to be clear that is
an expectation and not a measurement — nobody has trained this model yet.

The mitigations are what make that survivable rather than fatal. A false fire
with no speech after it is discarded within three seconds, costs nothing, sends
nothing, and leaves one line in the audit log. After a week, `ranger log` gives
you the real number, and that number is worth more than anything I can predict.

If it is genuinely too talkative: raise the threshold first, add the observed
false-fire words to `custom_negative_phrases` second, and change the phrase
third. A longer, less ordinary phrase is the fix that actually works, and it is
one notebook run away.
