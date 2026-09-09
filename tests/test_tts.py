"""ElevenLabs, against a mock transport.

The sandbox cannot reach api.elevenlabs.io, so nothing here proves the service
accepts what is sent. It proves the request shape, that only the two endpoints
the operator's key is scoped to are touched, and that failures are sentences.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from ranger.tts import (
    ElevenLabsSpeaker,
    SpeechError,
    build_speaker,
    decode,
    output_samplerate,
)


def transport(status=200, chunks=(b"\x01\x02", b"\x03\x04"), payload=None, body=None):
    import httpx2 as httpx

    seen: dict = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        if request.content:
            try:
                seen["json"] = json.loads(request.content)
            except ValueError:
                seen["json"] = None
        if body is not None:
            return httpx.Response(status, text=body)
        if payload is not None:
            return httpx.Response(status, json=payload)
        return httpx.Response(status, content=b"".join(chunks))

    return httpx.MockTransport(handler), seen


async def speak(config, mock, text="Rod owes you volumes."):
    speaker = ElevenLabsSpeaker(config.tts, "el-key", transport=mock)
    return b"".join([chunk async for chunk in speaker.stream(text)])


# -- output format ---------------------------------------------------------


def test_pcm_needs_no_decoder():
    assert output_samplerate("pcm_24000") == 24000
    assert output_samplerate("pcm_16000") == 16000


def test_mp3_is_not_raw_pcm():
    assert output_samplerate("mp3_44100_128") is None


def test_decoding_pcm_is_a_passthrough():
    pcm, rate = decode(b"\x01\x02\x03\x04", "pcm_24000")
    assert pcm == b"\x01\x02\x03\x04" and rate == 24000


def test_decoding_mp3_without_soundfile_says_what_to_install(monkeypatch):
    import builtins

    real = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "soundfile":
            raise ImportError("no soundfile")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(SpeechError) as caught:
        decode(b"\xff\xfb", "mp3_44100_128")
    assert "pip install soundfile" in str(caught.value)
    assert "pcm_24000" in str(caught.value)


# -- the request -----------------------------------------------------------


async def test_the_request_is_shaped_the_way_elevenlabs_wants(config):
    mock, seen = transport()
    audio = await speak(config, mock)

    assert audio == b"\x01\x02\x03\x04"
    assert seen["method"] == "POST"
    assert "/v1/text-to-speech/test-voice-id/stream" in seen["url"]
    assert "output_format=pcm_24000" in seen["url"]
    assert seen["headers"]["xi-api-key"] == "el-key"
    assert seen["json"]["text"] == "Rod owes you volumes."
    assert seen["json"]["model_id"] == "eleven_flash_v2_5"
    assert seen["json"]["voice_settings"]["stability"] == 0.5


async def test_only_the_two_scoped_endpoints_are_ever_called(config):
    """The operator's key is Text to Speech plus Voices read only."""
    mock, seen = transport()
    await speak(config, mock)
    assert "/v1/text-to-speech/" in seen["url"]

    mock, seen = transport(payload={"voices": []})
    await ElevenLabsSpeaker(config.tts, "k", transport=mock).voices()
    assert seen["url"].endswith("/v1/voices")
    assert seen["method"] == "GET"


async def test_empty_text_never_reaches_the_network(config):
    mock, seen = transport()
    speaker = ElevenLabsSpeaker(config.tts, "k", transport=mock)
    assert [c async for c in speaker.stream("   ")] == []
    assert seen == {}


async def test_an_unset_voice_id_says_to_run_ranger_voices(config):
    mock, _ = transport()
    speaker = ElevenLabsSpeaker(replace(config.tts, voice_id=""), "k", transport=mock)
    with pytest.raises(SpeechError, match="ranger voices"):
        [c async for c in speaker.stream("hello")]


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "rejected the API key"),
        (403, "scoped to this"),
        (404, "does not know voice"),
        (422, "output_format"),
        (429, "rate limiting"),
        (500, "having trouble"),
    ],
)
async def test_every_failure_is_a_sentence(config, status, expected):
    mock, _ = transport(status=status, body="nope")
    with pytest.raises(SpeechError, match=expected):
        await speak(config, mock)


async def test_a_422_names_the_plan_friendly_fallback(config):
    mock, _ = transport(status=422, body="output_format pcm_24000 not allowed on your plan")
    with pytest.raises(SpeechError) as caught:
        await speak(config, mock)
    assert "mp3_44100_128" in str(caught.value)


async def test_a_network_failure_is_retryable(config):
    import httpx2 as httpx

    def boom(request):
        raise httpx.ConnectError("no route", request=request)

    speaker = ElevenLabsSpeaker(config.tts, "k", transport=httpx.MockTransport(boom))
    with pytest.raises(SpeechError) as caught:
        [c async for c in speaker.stream("hello")]
    assert caught.value.retryable


# -- voices ----------------------------------------------------------------


async def test_voices_are_parsed(config):
    mock, _ = transport(
        payload={
            "voices": [
                {
                    "voice_id": "abc123",
                    "name": "Chris",
                    "labels": {"accent": "American", "gender": "male", "description": "casual"},
                    "preview_url": "https://example.com/a.mp3",
                }
            ]
        }
    )
    voices = await ElevenLabsSpeaker(config.tts, "k", transport=mock).voices()
    assert len(voices) == 1
    assert voices[0].voice_id == "abc123" and voices[0].name == "Chris"
    assert "American" in voices[0].describe()


async def test_a_voice_with_no_labels_still_parses(config):
    mock, _ = transport(payload={"voices": [{"voice_id": "x", "name": "Y"}]})
    voices = await ElevenLabsSpeaker(config.tts, "k", transport=mock).voices()
    assert voices[0].describe() == ""


def test_an_unknown_provider_is_rejected(config):
    with pytest.raises(ValueError, match="unknown tts.provider"):
        build_speaker(replace(config.tts, provider="azure"), "k")


# -- what is actually sent, and what is not --------------------------------
#
# Jarvis mumbles and slurs. Before changing a default, this is the record of
# what the request contains, so the answer to "what are we sending" is a test
# rather than a memory.


def sent_body(config_tts, text="Illes pricing lands Thursday."):
    """The JSON body and query one stream() call puts on the wire."""
    import asyncio

    mock, seen = transport()
    speaker = ElevenLabsSpeaker(config_tts, "el-key", transport=mock)

    async def drain():
        async for _ in speaker.stream(text):
            pass

    asyncio.run(drain())
    return seen


def test_the_voice_settings_that_are_sent(config):
    """**Three, and only three, unless they are changed.** Nothing sends
    `style` or `use_speaker_boost` by default, and that is deliberate rather
    than forgotten: an untouched config makes the request it always made."""
    from dataclasses import replace

    body = sent_body(replace(config.tts, voice_id="a-voice"))["json"]

    assert body["model_id"] == "eleven_flash_v2_5"
    assert body["voice_settings"] == {
        "stability": 0.5, "similarity_boost": 0.75, "speed": 1.0
    }
    assert "style" not in body["voice_settings"]
    assert "use_speaker_boost" not in body["voice_settings"]


def test_style_and_speaker_boost_are_sent_when_they_are_set(config):
    from dataclasses import replace

    tuned = replace(config.tts, voice_id="a-voice", style=0.35, speaker_boost=True,
                    stability=0.7)
    settings = sent_body(tuned)["json"]["voice_settings"]

    assert settings["style"] == 0.35
    assert settings["use_speaker_boost"] is True
    assert settings["stability"] == 0.7


def test_the_output_format_is_a_query_parameter_not_a_setting(config):
    from dataclasses import replace

    seen = sent_body(replace(config.tts, voice_id="a-voice"))
    assert "output_format=pcm_24000" in seen["url"]


def test_the_orb_smoothing_preset_never_touches_the_audio():
    """`natural` is a pair of time constants on a number between 0 and 1 that
    controls how bright a shader draws. It cannot affect articulation, and the
    question is worth a test because it is a reasonable thing to suspect."""
    from pathlib import Path

    orb = (Path(__file__).resolve().parent.parent / "ranger" / "web" / "orb.js")
    text = orb.read_text(encoding="utf-8")

    preset = text.split("export const PRESETS")[1].split("};")[0]
    assert "attack" in preset and "release" in preset
    # The presets are consumed by exactly one thing: the uniform the shaders
    # read. Nothing in the scene touches an AudioBuffer or a sample.
    assert "uVoiceBright" in text
    for audio_word in ("AudioBuffer", "createBufferSource", "sampleRate", "decodeAudioData"):
        assert audio_word not in text, f"the scene touches {audio_word}"
