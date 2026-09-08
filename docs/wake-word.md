# Hands free

Off by default. It is turned on per session in the interface and is never
remembered, so a restart always comes back off.

## Installing it

```powershell
# Stop the server first: a running one holds ranger.exe open.
ranger stop
pip install -e ".[wake]"
ranger doctor          # says whether it imported
```

**`pip install "ranger[wake]"` does not work**, and worse, it does not fail. It
resolves the already-installed package, prints `WARNING: ranger 0.1.0 does not
provide the extra 'wake'`, and exits successfully having done nothing. The
extra only exists in this working tree, so the install has to point at it:
`-e ".[wake]"`.

An optional extra rather than a dependency: `onnxruntime` wheels for new Python
versions arrive late, and nothing else in Ranger may stop working because a
wake word would not install. If it will not install, hands free simply is not
offered and everything else is unaffected.

## The phrase

openWakeWord ships a small fixed set of phrases. **"hey ranger" is not one of
them** and has to be trained, which needs a GPU and about an hour on Colab.

Until then the default is `hey jarvis`, which works and proves the whole
mechanism. Be careful what you conclude from it: `hey jarvis` is phonetically
rare and "hey ranger" is two ordinary English words, so anything observed with
jarvis is flattering.

To train the real one, see `scripts/train_wake_word.py`. It writes an `.onnx`
you copy back and point `wake.model` at.

## What happens when it fires

1. **The microphone is checked again.** If another application has taken it
   since arming, nothing is recorded, hands free turns off, and the log says
   which application it was.
2. **It waits up to `grace_seconds` for speech.** Nothing spoken in that window
   is **discarded**, not sent. Silence costs money and transcribes to nothing.
3. **Speech is recorded until `silence_seconds` of quiet**, or the
   `max_seconds` ceiling.
4. **The recording starts before the phrase fired.** Detection lags by a few
   hundred milliseconds and people run the phrase into the request, so a
   rolling `preroll_seconds` buffer is always kept. The phrase is then stripped
   off the front of the *transcript*, never cut out of the audio: the audio
   boundary is a guess, and guessing it wrong eats the first word.

## Every firing is logged

Including the discarded ones. `ranger log` shows a `hands-free fired ...` row
for each, so false fires can be counted over a week rather than guessed at, and
a fire during a call it should have disarmed for is visible rather than
invisible.

```
| 09:14:02 | browser | hands-free fired no_speech | 'hey jarvis' fired at 0.71 and nothing followed, so it was discarded |
| 11:02:44 | browser | hands-free fired spoke     | 'hey jarvis' fired at 0.93, 4.2s captured |
```

## The microphone check

Windows records which applications have the microphone open, and Ranger reads
the same source the taskbar indicator does. The rule is strict: **any other
consumer wins.** Hands free will not arm while anything else holds the
microphone, and disarms if something takes it.

Strict rather than lenient because most of the operator's meetings are browser
calls, and a browser holding the microphone cannot be told apart from a browser
holding it for a call. Lenient would have left uncovered exactly the case the
check exists for.

**And it fails closed.** If the check cannot run, hands free refuses to arm and
says so. A check that cannot run has found nothing, not found nobody. That also
means hands free does not work on anything but Windows, because there is no
consent store to read anywhere else.

**One Chrome looks like every other Chrome.** Windows records microphone use
per executable, so every window and every profile of a browser shares one
entry. Ranger's own interface and a Teams call in a tab are the same row, and
there is no way to exclude one without excluding the other.

That does not weaken the check, but it did once deadlock it. The interface used
to open its microphone on the first click and hold it for the life of the page,
so Chrome was listed as in use permanently and hands free refused to arm for
Ranger's own idle stream. The stream is now released the moment recording
stops, and arming puts it down explicitly before asking. The cost is a few
hundred milliseconds of device startup per utterance and the recording
indicator appearing and disappearing as it should.

So while hands free is armed, Python holds the microphone and the browser holds
nothing. Clicking the mic button while armed says so rather than taking it
back.

**Check that it works before trusting it.**

```powershell
ranger mic          # what the consent store says, and whether hands free could arm
```

Open a call, run it again, and see whether Ranger sees what your taskbar sees.
The whole check reads a Windows registry key, which cannot be exercised
anywhere without one, so this exists to make it confirmable in one command. A
check nobody can confirm is a check nobody should trust.

If it lists no applications at all, that is treated as **not knowing** rather
than as nobody using the microphone, and hands free refuses. On a real machine
the store always has entries, so an empty result means the enumeration is
looking in the wrong place, and the symptom of accepting it would be a check
that silently always says yes.

It runs three times: before arming, on a timer while armed, and again whenever
the phrase fires. A check only at arming misses a call that starts afterwards.
A check only at fire time leaves the microphone held for a whole call that
nothing happens to fire during.

The consequence: **Python owns the microphone while hands free is armed**, so
the browser does not touch it. While armed, the level the orb follows for input
arrives over the socket instead of being measured in the page. Playback
amplitude is unchanged and still measured where the sound comes out.

## What turns it off

- Clicking the banner, or the hands free control in the header.
- **Escape**, from anywhere.
- A confirmation card opening. A spoken yes is not consent, and an open
  microphone is exactly how one would be given by accident.
- `idle_disarm_minutes` with no interaction, fifteen by default.
- Another application taking the microphone.
- The interface being closed.
