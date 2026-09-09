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

## Telling you from Jarvis, and from the room

The hard part is not detecting speech. It is telling your voice from Jarvis's
own coming back through the microphone, and from whatever else the room is
doing.

The microphone is PortAudio's, in this process. The speaker is the browser's.
There is no echo canceller anywhere in that path, and on a laptop speaker the
microphone hears the reply at a level comparable to a person talking.

Three signals, so the bar has three terms and takes the highest:

    bar = max( floor, room x room_margin, echo x margin )

1. **The floor** (`SILENCE_RMS`). Below it nothing is speech, whatever the
   arithmetic says. The same floor the wake phrase uses, so "quiet" means one
   thing.
2. **The room** (`wake.bargein_room_margin`, 1.4), measured **continuously**.
   The microphone loop runs the whole time hands free is on, so the frames
   between replies are free and they are the room: the median of the last
   `wake.bargein_room_seconds` (10s) of them. A median, so a sentence or a door
   moves it barely at all while a fan being switched on moves it fully. Frames
   inside the tail are excluded, because they still contain Jarvis.
3. **The echo** (`wake.bargein_margin`, 1.4), measured per reply. The loudest
   0.6s of the reply's start is not what is taken — what is taken is the level
   the echo **holds** for the sustain window, so that it is the same kind of
   measurement as the one you have to beat.

Then you have to beat that bar for `wake.bargein_sustain_seconds` (220ms, about
three frames) — long enough that a consonant burst in Jarvis's own speech
cannot do it.

### Held, not peak

**A peak is one frame and the detector ignores it.** The sustain means only a
level *held* for three frames counts, so a peak overstates what the detector
will honour, and a mean is dragged down by the gaps between words.

This was a real defect, not a tuning preference. The first version took the
echo's loudest single frame as the reference. Measured on hardware that frame
came out four to five times the echo's own average, so a margin of 2.2 was
asking the operator to hold roughly nine times Jarvis's average level. Across
three calibration runs, **no margin at or above 1.0 could work at all** — the
detector had no valid setting, and the symptom was "sometimes it stops and
sometimes it doesn't", because only a chance alignment of voice peaks ever
cleared the bar.

If you are carrying a `bargein_margin` over from before that fix, discard it.
The number means something different now.

### Why the room gets its own term

Three calibration runs, minutes apart, in one seat, at one speaker volume, put
the room's held level at 1264, 85 and 423 while the operator's barely moved.
The room is the term that swings, and it was the one term measured once and
then never used. A bar set only from the echo sits underneath a room like that,
and **the room then interrupts with nobody in the chair** — a worse failure than
not stopping at all.

## Measuring it on this machine

    ranger mic-bargein

Three passes — the room, Jarvis speaking, you talking over him — repeated
`--rounds` times (3 by default), because one four-second sample of a voice is
not a voice and one of a room is certainly not a room. It prints the held,
mean and peak of every pass and the **spread across passes**, which is the
finding: a level that swings fifteen times over between rounds is a level no
single reading describes.

Jarvis speaks for himself here rather than needing a second terminal, and the
same synthesised sentence is reused for every pass so the passes are
comparable. Without an `ELEVENLABS_API_KEY` it falls back to asking you to
start the speech.

**The recommendation is not arithmetic on those numbers.** The recordings are
replayed through a real `Detector` at each candidate margin, and what is
reported is the range that never fires on the echo, never fires on the room
laid over a reply, and always fires on every one of your voice passes. A
recommendation that the code would not have honoured is worse than none.

If no margin does all three, it says so and offers no number. That is a real
state of the world and naming it is the useful answer.

**Every pass is judged against every room**, not each round against its own.
The room floor is continuous and your speaking level is independent of it, so
over an afternoon the loudest room and the quietest speech will meet, and the
detector will be in that state when they do. An earlier version paired each
round with itself — which kept the loud-room round away from the quiet-voice
round, and reported a workable margin for a machine whose room is louder than
its operator.

Alongside the scan, the levels are compared directly, because "no margin works"
is not advice and the lever depends on which two levels collided:

- **The room holds a level near your voice** → a headset. Nothing in the config
  separates two signals of the same size.
- **The echo holds a level near your voice** → turn the speaker down. Jarvis is
  reaching the microphone as loudly as you do, and no ratio lets one through
  without the other.

A margin is never printed beside either of those. One run printed
"set wake.bargein_margin = 5.6" directly above "no margin can", and the number
is the half that gets typed in.

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

## The ceiling

Level is all this has. Nothing in the path knows what Jarvis is playing, so his
voice can only ever be **outranked**, never removed — and his voice is speech,
so no amount of speech detection separates it from yours either.

That leaves two real fixes, and neither is a number in this file:

- **A headset.** It moves all three levels at once: your voice goes up because
  the microphone is at your mouth, and both the echo and the room go down
  because the microphone is no longer in the room with them. Everything above
  becomes slack rather than marginal.
- **Capturing the microphone in the browser**, where `getUserMedia` with
  `echoCancellation` has both the captured audio and the audio being played,
  because the browser is also the speaker. The echo would be cancelled rather
  than outranked. That is the structurally correct answer and it is a change to
  where the microphone lives, not to how it is judged.

If calibration reports a room that holds a level close to your own voice, no
margin can help: two signals the same size cannot be separated by size.
