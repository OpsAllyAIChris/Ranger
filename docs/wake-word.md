# Hands free

Off by default. It is turned on per session in the interface and is never
remembered, so a restart always comes back off.

## Installing it

```powershell
pip install "ranger[wake]"
ranger doctor          # says whether it imported
```

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
