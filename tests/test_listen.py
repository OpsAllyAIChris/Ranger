"""Tier 7d: voice in the browser, from the Python side.

The browser owns the microphone and the loudspeaker, so what can be tested
here is the seam: what arrives over the socket, what is refused, and that a
spoken turn goes through the same core and the same gate a typed one does.
"""

from __future__ import annotations

import base64
import json
import threading
from dataclasses import replace

import pytest

from ranger.testing import WebSocketClient as Client

from ranger.listen import (
    ALLOWED_MIME,
    MAX_AUDIO_BYTES,
    AudioRejected,
    decode_audio,
    encode_audio,
)
from ranger.stt import Transcript, Word


def message(audio: bytes = b"opus", **extra):
    return {"audio": base64.b64encode(audio).decode(), **extra}


# -- what the socket will accept -------------------------------------------


def test_a_recording_arrives_as_base64_inside_json():
    """Not a binary frame. Every message on this socket stays readable text."""
    utterance = decode_audio(message(b"opus bytes", mime="audio/webm;codecs=opus", seconds=1.4))
    assert utterance.audio == b"opus bytes"
    assert utterance.mime == "audio/webm;codecs=opus"
    assert utterance.seconds == 1.4


def test_chromes_codec_parameter_is_kept_when_it_is_known():
    assert decode_audio(message(mime="audio/webm;codecs=opus")).mime == "audio/webm;codecs=opus"


def test_an_unknown_codec_parameter_falls_back_to_the_base_type():
    """Deepgram reads the container. A parameter nobody has seen before is not
    a reason to refuse an ordinary webm."""
    assert decode_audio(message(mime="audio/webm;codecs=vorbis")).mime == "audio/webm"


@pytest.mark.parametrize(
    "bad, why",
    [
        ({}, "no audio"),
        ({"audio": ""}, "no audio"),
        ({"audio": "not base64 at all!!"}, "base64"),
        ({"audio": base64.b64encode(b"").decode()}, "no audio"),
    ],
)
def test_a_message_without_usable_audio_is_refused(bad, why):
    with pytest.raises(AudioRejected, match=why):
        decode_audio(bad)


def test_something_that_is_not_audio_is_refused():
    with pytest.raises(AudioRejected, match="transcribe"):
        decode_audio(message(mime="text/html"))
    with pytest.raises(AudioRejected):
        decode_audio(message(mime="application/octet-stream"))


def test_every_format_a_browser_produces_is_allowed():
    for mime in ("audio/webm", "audio/ogg", "audio/mp4", "audio/wav"):
        assert mime in ALLOWED_MIME


def test_an_absurd_recording_is_refused_before_it_is_decoded():
    """A cap so a confused client cannot make the server hold an arbitrary
    amount of audio while it decodes base64."""
    with pytest.raises(AudioRejected, match="too long"):
        decode_audio({"audio": "A" * (MAX_AUDIO_BYTES // 3 * 4 + 100)})


def test_audio_round_trips_through_base64():
    import os

    raw = os.urandom(2048)
    assert decode_audio({"audio": encode_audio(raw)}).audio == raw


# -- a spoken turn, over a real socket -------------------------------------


class Ears:
    """A transcriber that hears one fixed thing, and records what it was given."""

    def __init__(self, text="where are we on Illes Foods"):
        self.text = text
        self.seen: list[tuple[int, str]] = []

    async def transcribe(self, audio, *, hints=True, content_type="audio/wav"):
        self.seen.append((len(audio), content_type))
        return Transcript(
            text=self.text,
            confidence=0.96,
            words=(Word(text="Illes", confidence=0.6),),
            model="nova-3",
        )


class Mouth:
    """A speaker that returns a byte per character, and records the sentences."""

    def __init__(self):
        self.said: list[str] = []

    async def stream(self, text):
        self.said.append(text)
        yield b"\x00" * len(text)


SCRIPT = [{"text": "Illes is quiet. Rod has not replied."}]


@pytest.fixture
def spoken(config):
    """A server whose browser session has ears and a mouth."""
    from ranger.audit import AuditLog
    from ranger.bridge import Session
    from ranger.core import Ranger
    from ranger.gate import SocketGate
    from ranger.knowledge import KnowledgeLoader
    from ranger.server import build
    from ranger.testing import ScriptedProvider
    from ranger.toolset import build_registry
    from ranger.vault import Vault

    ears, mouth = Ears(), Mouth()

    def factory(cfg, send):
        vault = Vault(cfg.vault)
        return Session(
            agent=Ranger(
                config=cfg,
                provider=ScriptedProvider(list(SCRIPT)),
                registry=build_registry(cfg, vault),
                vault=vault,
                knowledge_loader=KnowledgeLoader(vault, cfg.vault, cfg.knowledge),
                gate=SocketGate(send),
                audit=AuditLog(vault, cfg.vault.log),
                origin="browser",
            ),
            send=send,
            transcriber=ears,
            speaker=mouth,
        )

    server = build(
        replace(config, server=replace(config.server, port=0)), session_factory=factory
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, ears, mouth
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_front_end_is_told_whether_there_is_a_microphone(spoken):
    """It shows a mic only if something is behind it."""
    port, _, _ = spoken
    client = Client(port)
    try:
        hello = client.next()
        assert hello["voice"] is True
        assert hello["speech"] is True
    finally:
        client.close()


def test_a_recording_becomes_a_turn(spoken):
    port, ears, mouth = spoken
    client = Client(port)
    try:
        client.until("panel")
        client.send({"type": "audio", "audio": encode_audio(b"x" * 4096), "mime": "audio/webm",
                     "seconds": 1.7})

        heard = client.wait_for("heard")
        assert heard["text"] == "where are we on Illes Foods"
        assert heard["seconds"] == 1.7
        assert heard["shaky"] == ["Illes"], "a low confidence word is named, not hidden"

        events = client.until("done")
        reply = "".join(e["text"] for e in events if e["kind"] == "text")
        assert reply == "Illes is quiet. Rod has not replied."
    finally:
        client.close()

    assert ears.seen == [(4096, "audio/webm")], "the browser's format reached Deepgram intact"


def test_the_reply_comes_back_sentence_by_sentence(spoken):
    """Where the perceived latency lives. Waiting for a whole answer before
    saying any of it adds a second to every turn for nothing."""
    port, _, mouth = spoken
    client = Client(port)
    try:
        client.until("panel")
        client.send({"type": "audio", "audio": encode_audio(b"x" * 512), "mime": "audio/webm"})
        events = client.until("done")

        speech = [e for e in events if e["kind"] == "speech"]
        assert [e["text"] for e in speech] == ["Illes is quiet.", "Rod has not replied."]
        assert [e["index"] for e in speech] == [0, 1]
        assert all(base64.b64decode(e["audio"]) for e in speech)
    finally:
        client.close()

    assert mouth.said == ["Illes is quiet.", "Rod has not replied."]


def test_hearing_nothing_says_so_rather_than_answering_silence(config, spoken):
    port, ears, _ = spoken
    ears.text = "   "
    client = Client(port)
    try:
        client.until("panel")
        client.send({"type": "audio", "audio": encode_audio(b"x" * 512), "mime": "audio/webm"})
        assert client.wait_for("heard")["text"] == ""
        assert "heard nothing" in client.wait_for("error")["message"]
    finally:
        client.close()


def test_a_turn_still_works_when_a_sentence_cannot_be_spoken(spoken, monkeypatch):
    """Losing the voice is bad. Losing the answer is worse."""
    port, _, mouth = spoken

    async def broken(text):
        raise RuntimeError("ElevenLabs said no")
        yield b""

    mouth.stream = broken

    client = Client(port)
    try:
        client.until("panel")
        client.send({"type": "audio", "audio": encode_audio(b"x" * 512), "mime": "audio/webm"})
        events = client.until("done")
        assert any(e["kind"] == "notice" and "could not speak" in e["message"] for e in events)
        assert "".join(e["text"] for e in events if e["kind"] == "text")
    finally:
        client.close()


def test_bad_audio_is_answered_rather_than_dropped(spoken):
    port, _, _ = spoken
    client = Client(port)
    try:
        client.until("panel")
        client.send({"type": "audio", "audio": "not base64", "mime": "audio/webm"})
        assert "base64" in client.wait_for("error")["message"]
    finally:
        client.close()


@pytest.fixture
def deaf(config):
    """The same server with nothing wired for voice, which is what a machine
    with no DEEPGRAM_API_KEY gets."""
    from ranger.audit import AuditLog
    from ranger.bridge import Session
    from ranger.core import Ranger
    from ranger.gate import SocketGate
    from ranger.knowledge import KnowledgeLoader
    from ranger.server import build
    from ranger.testing import ScriptedProvider
    from ranger.toolset import build_registry
    from ranger.vault import Vault

    def factory(cfg, send):
        vault = Vault(cfg.vault)
        return Session(
            agent=Ranger(
                config=cfg,
                provider=ScriptedProvider(list(SCRIPT)),
                registry=build_registry(cfg, vault),
                vault=vault,
                knowledge_loader=KnowledgeLoader(vault, cfg.vault, cfg.knowledge),
                gate=SocketGate(send),
                audit=AuditLog(vault, cfg.vault.log),
                origin="browser",
            ),
            send=send,
        )

    server = build(
        replace(config, server=replace(config.server, port=0)), session_factory=factory
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_connection_with_no_ears_says_so(deaf):
    """The interface still opens and still types when no key is configured."""
    client = Client(deaf)
    try:
        assert client.next()["voice"] is False
        client.until("panel")
        client.send({"type": "audio", "audio": encode_audio(b"x" * 512)})
        assert "no transcription" in client.wait_for("error")["message"]
    finally:
        client.close()


def test_speaking_over_ranger_with_the_microphone_interrupts(spoken):
    """Barge-in by voice, not only by typing."""
    port, _, _ = spoken
    client = Client(port)
    try:
        client.until("panel")
        client.send({"type": "audio", "audio": encode_audio(b"x" * 512), "mime": "audio/webm"})
        client.until("done")
        # A second recording arrives while nothing is running: it just runs.
        client.send({"type": "audio", "audio": encode_audio(b"y" * 512), "mime": "audio/webm",
                     "interrupt": True})
        assert client.wait_for("heard")["text"]
    finally:
        client.close()


# -- what the front end is built from --------------------------------------


def test_the_smoothing_presets_are_named_and_ordered():
    """Judging these needs a real voice, so they have to be switchable live."""
    import re

    source = (__import__("ranger").__path__[0] + "/web/orb.js")
    text = open(source, encoding="utf-8").read()
    block = re.search(r"export const PRESETS = \{(.*?)\n\};", text, re.S).group(1)
    for name in ("quick", "natural", "slow"):
        assert f"{name}:" in block
    assert "smoothing(name)" in text
    assert "DEFAULT_PRESET = 'natural'" in text


def test_the_mic_button_matches_the_specification():
    from pathlib import Path

    style = Path(__import__("ranger").__path__[0], "web", "shell.css").read_text(encoding="utf-8")
    for value in (
        "width: 64px; height: 64px",
        "border: 1px solid rgba(255, 255, 255, 0.08)",
        "background: var(--surface)",
        "box-shadow: 0 0 24px rgba(45, 212, 168, 0.5)",
        "0 0 0 10px rgba(45, 212, 168, 0)",
        "1.4s var(--ease) infinite",
        "letter-spacing: 0.08em",
        "font-size: 11px",
    ):
        assert value in style, value


def test_the_mic_bar_lets_clicks_through_to_the_orb():
    from pathlib import Path

    style = Path(__import__("ranger").__path__[0], "web", "shell.css").read_text(encoding="utf-8")
    bar = style.split("#micbar {")[1].split("}")[0]
    assert "pointer-events: none" in bar
    button = style.split("#mic {")[1].split("}")[0]
    assert "pointer-events: auto" in button


def test_the_icon_swap_is_decided_by_css_not_by_setting_hidden_on_an_svg():
    """`hidden` is an HTMLElement property. Setting it on an svg succeeds and
    writes nothing, so the button showed a microphone while recording."""
    from pathlib import Path

    web = Path(__import__("ranger").__path__[0], "web")
    style = web.joinpath("shell.css").read_text(encoding="utf-8")
    script = web.joinpath("shell.js").read_text(encoding="utf-8")

    assert "#mic[data-state='live'] .icon-mic { display: none; }" in style
    assert "#mic[data-state='live'] .icon-stop { display: block; }" in style
    assert ".icon-mic').hidden" not in script
    assert ".icon-stop').hidden" not in script


def test_a_spoken_yes_cannot_reach_the_gate():
    """Tier 3's rule, enforced by the interface rather than by hoping."""
    from pathlib import Path

    script = Path(__import__("ranger").__path__[0], "web", "shell.js").read_text(encoding="utf-8")
    opened = script.split("function openCard(")[1].split("function ")[0]
    assert "stopListening(true)" in opened
    assert "el.mic.disabled = true" in opened
    assert "if (document.activeElement === el.say || openToken) return;" in script


# -- how the browser is told to read the audio -----------------------------


def test_raw_pcm_travels_with_its_sample_rate():
    """The default output has no header of any kind.

    tts.output_format is pcm_24000, which is 16 bit little endian samples and
    nothing else. decodeAudioData rejects it outright, so voice worked in the
    terminal, where PortAudio is told the rate separately, and was silent in
    the browser. Neither autoplay nor an ElevenLabs problem: the absence of a
    container.
    """
    from ranger.listen import speech_format

    assert speech_format("pcm_24000") == {
        "encoding": "pcm", "rate": 24000, "bits": 16, "channels": 1,
    }
    assert speech_format("pcm_16000")["rate"] == 16000


@pytest.mark.parametrize(
    "output_format, mime",
    [
        ("mp3_44100_128", "audio/mpeg"),
        ("mp3_22050_32", "audio/mpeg"),
        ("opus_48000_64", "audio/ogg"),
        ("ulaw_8000", "audio/basic"),
    ],
)
def test_a_format_with_a_container_decodes_itself(output_format, mime):
    from ranger.listen import speech_format

    assert speech_format(output_format) == {"encoding": "container", "mime": mime}


def test_the_format_is_sent_with_every_sentence(spoken):
    port, _, _ = spoken
    client = Client(port)
    try:
        client.until("panel")
        client.send({"type": "audio", "audio": encode_audio(b"x" * 512), "mime": "audio/webm"})
        speech = [e for e in client.until("done") if e["kind"] == "speech"]
        assert speech
        for event in speech:
            assert event["format"]["encoding"] in {"pcm", "container"}
    finally:
        client.close()


def test_the_browser_builds_a_buffer_for_pcm_rather_than_decoding_it():
    from pathlib import Path

    source = Path(__import__("ranger").__path__[0], "web", "voice.js").read_text(encoding="utf-8")
    assert "function pcmBuffer(" in source
    assert "getInt16(" in source
    assert "32768" in source, "scaled by the magnitude of the most negative sample"
    assert "format.encoding === 'pcm'" in source
