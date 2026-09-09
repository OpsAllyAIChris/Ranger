# Barge-in

Hands free, mid-reply, you start speaking. The speech stops.

You do not say the wake phrase to do it. Nobody says a machine's name to
interrupt it mid-sentence, and requiring it would make interrupting slower than
waiting.

## What stops, and what does not

**The speaking stops. The turn does not.**

By the time a sentence is being spoken, the tools for that turn have already
run or are running. A turn cancelled half way could leave a note filed with
nothing said about it — a half-spoken answer that still changed something is
harder to recover from than one that finishes into a room where nobody is
listening. So what gets dropped is the audio, which is the thing actually in
the way, and the answer still lands in the window and in the log.

The browser's speaker queue is drained immediately, not allowed to finish the
sentence in hand. A trailing sentence after an interruption is what makes
interrupting feel like it did not work.

Conversation mode's own rule holds: **barge-in does not close the window.** You
interrupting is the most engaged you get.

## Telling you from Jarvis

The hard part is not detecting speech. It is telling your voice from Jarvis's
own, coming back through the microphone.

The microphone is PortAudio's, in this process. The speaker is the browser's.
There is no echo canceller anywhere in that path, and on a laptop speaker the
microphone hears the reply at a level comparable to a person talking. A
detector that ignores that stops Jarvis on his own second syllable, every
reply, forever.

So the separation is three cheap things stacked, each honest about what it is:

1. **Playback.** Barge-in is only ever looked for while a reply is actually
   playing, and playback state is what the browser reports, not something
   guessed from the microphone. Before and after, the wake phrase and the
   conversation window own the microphone, as they always did.
2. **A tail** (`wake.bargein_tail_seconds`, 350ms). The browser reports its
   queue drained *between sentences*, not only at the end of a reply, because
   the model is often slower than the voice. A drain inside the tail is the
   same reply carrying on; one older than the tail is the end of it. Without
   this, every sentence would re-learn the echo and the first 0.6s of each
   would be un-interruptible — in a slow reply, nearly all of it.
3. **A measured level, not a guessed one** (`wake.bargein_margin`, 2.2). While
   Jarvis is speaking and nobody has interrupted, whatever the microphone hears
   *is* the echo. The loudest frame of the reply's first 0.6s sets the bar, and
   you have to beat it by the margin for `wake.bargein_sustain_seconds` (220ms,
   about three frames) — long enough that a consonant burst in Jarvis's own
   speech cannot do it.

**With a headset none of this is needed and all of it is harmless.** The echo
floor measures near silence, so the margin is met by any speech at all. Tuning
the margin is the second-best fix; a headset removes the problem rather than
tuning it.

## Measuring it on this machine

    ranger mic-bargein

Three passes — the room, Jarvis speaking, you talking over him — printing the
mean and peak of each, the threshold your configured margin puts on them, and
either how far your voice clears it or a margin that would work.

## What the log says

Every interruption writes one line:

    speech interrupted — stopped after 2 of 5 sentences, 3 unspoken (214 characters)

The unspoken count is the diagnostic. An interruption with **nothing** left
unspoken says so in as many words, because that is the shape of Jarvis
interrupting himself: his own voice cleared the bar his reply set. A week of
those means the margin is too low, and the fix is the margin, not the tail.

## Known and stated rather than pretended otherwise

This is an energy detector, not a speech classifier.

- **A bang does not interrupt** — a door, a dropped mug, a hand on the desk is
  loud and over inside one frame, and the sustain rejects it.
- **Sustained noise that is not speech does interrupt.** A vacuum cleaner next
  to the microphone will stop the speech. It stops the speech and nothing else,
  which is a cheap thing to be wrong about.
- **Talking during the first word is the hardest case.** The first 0.6s is the
  learning window, so your voice is measured into the echo floor and that reply
  ends up harder to interrupt. That is the safe direction to be wrong in, but
  it is the case to report if it feels wrong.
