"""Tier 7d. Voice in the browser, from the Python side.

The microphone is in the browser and so is the loudspeaker. That was decided
for two reasons: amplitude has to be measured where the sound actually is, or
the orb is animating a guess; and a design where the browser owns the audio
still works when the browser is not on the same machine as the server.

So the round trip is: the browser records an utterance and sends it as one
message, this transcribes it with the same `stt.py` the terminal voice loop
uses, and the reply comes back sentence by sentence as audio the browser
plays. Reusing `stt.py` and `tts.py` whole means the spoken answer in the
browser and the spoken answer in the terminal cannot drift apart.

One utterance per message rather than a stream. Push to talk and a latching
mic button both produce a bounded recording, Deepgram's pre-recorded endpoint
already handles it, and streaming would mean a second transcription path to
keep in step with the first for no gain the operator would notice.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any, AsyncIterator

#: A minute of Opus is well under this. The cap is here so a confused or
#: hostile client cannot make the server hold an arbitrary amount of audio in
#: memory while it decodes base64.
MAX_AUDIO_BYTES = 8 * 1024 * 1024

#: What MediaRecorder actually produces, plus the two the terminal path uses.
#: Anything else is refused rather than forwarded, because Deepgram rejects a
#: body whose declared type contradicts its contents and the error it gives
#: says nothing useful.
ALLOWED_MIME = {
    "audio/webm",
    "audio/webm;codecs=opus",
    "audio/ogg",
    "audio/ogg;codecs=opus",
    "audio/mp4",
    "audio/mpeg",
    "audio/wav",
    "audio/x-wav",
}


class AudioRejected(Exception):
    """The message did not carry usable audio."""


@dataclass(frozen=True)
class Utterance:
    audio: bytes
    mime: str
    seconds: float | None = None


def decode_audio(message: dict[str, Any]) -> Utterance:
    """Pull the recording out of a socket message, or say why not.

    Base64 inside JSON rather than a binary frame. A few hundred kilobytes over
    loopback costs nothing, and the framing layer refuses binary deliberately:
    every message on this socket is text that can be read in a log.
    """
    raw = message.get("audio")
    if not isinstance(raw, str) or not raw.strip():
        raise AudioRejected("no audio in that message")

    # Roughly, before decoding: base64 is four characters per three bytes.
    if len(raw) > MAX_AUDIO_BYTES // 3 * 4 + 8:
        raise AudioRejected("that recording is too long")

    try:
        audio = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AudioRejected(f"the audio was not valid base64: {exc}") from exc

    if not audio:
        raise AudioRejected("the recording was empty")
    if len(audio) > MAX_AUDIO_BYTES:
        raise AudioRejected("that recording is too long")

    mime = str(message.get("mime", "audio/webm")).strip().lower()
    # Chrome appends a codecs parameter; keep the base type if the whole string
    # is not one we know, rather than refusing something perfectly ordinary.
    if mime not in ALLOWED_MIME:
        base = mime.split(";")[0].strip()
        if base not in ALLOWED_MIME:
            raise AudioRejected(f"{mime!r} is not audio this can transcribe")
        mime = base

    seconds = message.get("seconds")
    return Utterance(
        audio=audio,
        mime=mime,
        seconds=float(seconds) if isinstance(seconds, (int, float)) else None,
    )


async def transcribe(transcriber: Any, utterance: Utterance, *, hints: bool = True):
    """One utterance through the same transcriber the terminal voice loop uses."""
    return await transcriber.transcribe(
        utterance.audio, hints=hints, content_type=utterance.mime
    )


#: What a container-less format has to be told to the browser as. Anything
#: with a header decodes itself; raw PCM has no header at all.
CONTAINERS = {
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "ulaw": "audio/basic",
    "alaw": "audio/basic",
}


def speech_format(output_format: str) -> dict[str, Any]:
    """How the browser should read the audio it is about to be sent.

    `tts.output_format` defaults to `pcm_24000`, which is 16 bit little endian
    mono samples and nothing else: no header, no container, no way for anything
    to work out what it is. `decodeAudioData` rejects it outright, which is why
    the terminal path plays it straight into PortAudio at a rate it was told
    and the browser could not play it at all.

    So the rate travels with the audio. The alternative was asking ElevenLabs
    for MP3 for the browser and PCM for the terminal, which is a second format
    to keep working and a decoder in the way of the thing that was chosen
    precisely because it needs no decoder.
    """
    from .tts import output_samplerate

    rate = output_samplerate(output_format)
    if rate is not None:
        return {"encoding": "pcm", "rate": rate, "bits": 16, "channels": 1}

    head = output_format.strip().lower().split("_")[0]
    return {"encoding": "container", "mime": CONTAINERS.get(head, "audio/mpeg")}


async def speak(speaker: Any, text: str) -> bytes:
    """Collect one sentence of speech. Returned whole, not streamed.

    The terminal path streams into PortAudio because it is playing as it
    arrives. The browser is on the other end of a socket, and a sentence of
    audio is small enough that sending it in one message is simpler than
    reassembling a stream on the far side. Latency is preserved by splitting
    into sentences, which is where it came from in the first place.
    """
    chunks: list[bytes] = []
    async for chunk in speaker.stream(text):
        chunks.append(chunk)
    return b"".join(chunks)


def encode_audio(audio: bytes) -> str:
    return base64.b64encode(audio).decode("ascii")


async def sentences(events: AsyncIterator[Any], stream: Any) -> AsyncIterator[str]:
    """Split a stream of text deltas into speakable sentences.

    Wraps `speech.SentenceStream` so the browser path and the terminal path
    break sentences identically. Yields nothing for a delta that does not
    complete one, and the caller flushes what is left at the end.
    """
    from .events import TextDelta

    async for event in events:
        if isinstance(event, TextDelta):
            for sentence in stream.feed(event.text):
                yield sentence
