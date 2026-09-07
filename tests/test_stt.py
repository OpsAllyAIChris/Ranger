"""Deepgram, against a mock transport.

The sandbox cannot reach api.deepgram.com (http 000), so nothing here proves the
service accepts what is sent. What it does prove is that the request is built
the way the docs describe, that the hinting parameter follows the model, and
that every failure comes back as a sentence rather than a stack trace.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from ranger.compare import compare, tokens
from ranger.config import SttConfig
from ranger.stt import (
    DeepgramTranscriber,
    TranscriptionError,
    Word,
    build_params,
    build_transcriber,
    hint_parameter,
    parse_response,
)

WAV = b"RIFF....WAVEfmt " + b"\x00" * 64


def deepgram_payload(text="where are we on Illes Foods", confidence=0.98, words=None, duration=2.5):
    words = words if words is not None else [
        {"word": w.lower(), "punctuated_word": w, "confidence": 0.99, "start": i * 0.3, "end": i * 0.3 + 0.25}
        for i, w in enumerate(text.split())
    ]
    return {
        "metadata": {"duration": duration},
        "results": {
            "channels": [
                {"alternatives": [{"transcript": text, "confidence": confidence, "words": words}]}
            ]
        },
    }


def transport(status=200, payload=None, body=None):
    import httpx2 as httpx

    seen: dict = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["content"] = request.content
        if body is not None:
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=payload if payload is not None else deepgram_payload())

    return httpx.MockTransport(handler), seen


# -- which hinting parameter ----------------------------------------------


@pytest.mark.parametrize(
    "model,expected",
    [
        ("nova-3", "keyterm"),
        ("nova-3-general", "keyterm"),
        ("nova-2", "keywords"),
        ("nova-2-phonecall", "keywords"),
        ("enhanced", "keywords"),
        ("base", "keywords"),
        ("", "keywords"),
    ],
)
def test_the_model_picks_the_hinting_parameter(model, expected):
    """Sending the wrong one is a 400, so the config must not repeat itself."""
    assert hint_parameter(model) == expected


def test_keyterm_prompting_sends_bare_terms(config):
    params = build_params(replace(config.stt, model="nova-3"))
    hints = [v for k, v in params if k == "keyterm"]
    assert hints == ["Wexxar", "Illes", "corrugated"]
    assert not any(k == "keywords" for k, _ in params)


def test_keyword_boosting_sends_an_intensifier(config):
    params = build_params(replace(config.stt, model="nova-2", boost=3.0))
    hints = [v for k, v in params if k == "keywords"]
    assert hints == ["Wexxar:3.0", "Illes:3.0", "corrugated:3.0"]


def test_hints_can_be_turned_off_for_an_ab_test(config):
    params = build_params(config.stt, hints=False)
    assert not any(k in ("keyterm", "keywords") for k, _ in params)
    assert ("model", "nova-3") in params


def test_an_empty_keyterm_list_sends_no_hints(config):
    params = build_params(replace(config.stt, keyterms=()))
    assert not any(k in ("keyterm", "keywords") for k, _ in params)


def test_hints_are_capped(config):
    many = tuple(f"term{i}" for i in range(500))
    params = build_params(replace(config.stt, keyterms=many, max_hints=100))
    assert len([1 for k, _ in params if k == "keyterm"]) == 100


def test_the_usual_parameters_are_present(config):
    params = dict(build_params(config.stt))
    assert params["language"] == "en"
    assert params["smart_format"] == "true"
    assert params["punctuate"] == "true"


# -- parsing ---------------------------------------------------------------


def test_a_normal_response_is_parsed():
    transcript = parse_response(deepgram_payload(), model="nova-3")
    assert transcript.text == "where are we on Illes Foods"
    assert transcript.confidence == pytest.approx(0.98)
    assert len(transcript.words) == 6
    assert transcript.audio_seconds == 2.5


def test_punctuated_words_win_over_bare_ones():
    payload = deepgram_payload(words=[{"word": "illes", "punctuated_word": "Illes,", "confidence": 0.9}])
    assert parse_response(payload).words[0].text == "Illes,"


def test_low_confidence_words_are_flagged():
    payload = deepgram_payload(
        text="where are we on Ellis",
        words=[
            {"word": "where", "confidence": 0.99},
            {"word": "ellis", "confidence": 0.41},
        ],
    )
    shaky = parse_response(payload).shaky_words
    assert [w.text for w in shaky] == ["ellis"]


def test_a_response_with_no_transcript_says_so():
    with pytest.raises(TranscriptionError, match="no transcript"):
        parse_response({"results": {"channels": []}})


def test_an_empty_transcript_is_not_an_error():
    transcript = parse_response(deepgram_payload(text="", words=[]))
    assert transcript.empty


# -- the request over the wire --------------------------------------------


async def test_the_request_is_shaped_the_way_deepgram_wants(config):
    mock, seen = transport()
    result = await DeepgramTranscriber(config.stt, "dg-key", transport=mock).transcribe(WAV)

    assert seen["headers"]["authorization"] == "Token dg-key"
    assert seen["headers"]["content-type"] == "audio/wav"
    assert seen["content"] == WAV
    assert "api.deepgram.com/v1/listen" in seen["url"]
    assert "model=nova-3" in seen["url"]
    assert seen["url"].count("keyterm=") == 3
    assert result.text == "where are we on Illes Foods"
    assert result.hinted == ("Wexxar", "Illes", "corrugated")
    assert result.model == "nova-3"


async def test_hints_are_reported_as_none_when_off(config):
    mock, seen = transport()
    result = await DeepgramTranscriber(config.stt, "k", transport=mock).transcribe(WAV, hints=False)
    assert result.hinted == ()
    assert "keyterm=" not in seen["url"]


async def test_empty_audio_is_refused_before_the_network(config):
    mock, seen = transport()
    with pytest.raises(TranscriptionError, match="no audio"):
        await DeepgramTranscriber(config.stt, "k", transport=mock).transcribe(b"")
    assert seen == {}


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "rejected the API key"),
        (403, "rejected the API key"),
        (402, "out of credit"),
        (429, "rate limiting"),
        (500, "having trouble"),
        (503, "having trouble"),
    ],
)
async def test_every_failure_is_a_sentence(config, status, expected):
    mock, _ = transport(status=status, body="nope")
    with pytest.raises(TranscriptionError, match=expected):
        await DeepgramTranscriber(config.stt, "k", transport=mock).transcribe(WAV)


async def test_a_400_points_at_the_hinting_parameter(config):
    """If Deepgram has moved on, the error names what it rejected."""
    mock, _ = transport(status=400, body="keyterm is not supported for this model")
    with pytest.raises(TranscriptionError) as caught:
        await DeepgramTranscriber(config.stt, "k", transport=mock).transcribe(WAV)
    message = str(caught.value)
    assert "'keyterm'" in message
    assert "stt.keyterms = []" in message


async def test_a_network_failure_is_marked_retryable(config):
    import httpx2 as httpx

    def boom(request):
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(TranscriptionError) as caught:
        await DeepgramTranscriber(config.stt, "k", transport=httpx.MockTransport(boom)).transcribe(WAV)
    assert caught.value.retryable
    assert "could not reach Deepgram" in str(caught.value)


async def test_non_json_is_reported_plainly(config):
    mock, _ = transport(status=200, body="<html>gateway</html>")
    with pytest.raises(TranscriptionError, match="not JSON"):
        await DeepgramTranscriber(config.stt, "k", transport=mock).transcribe(WAV)


def test_an_unknown_provider_is_rejected(config):
    with pytest.raises(ValueError, match="unknown stt.provider"):
        build_transcriber(replace(config.stt, provider="whisper"), "k")


# -- comparing what was said with what was heard ---------------------------


def test_a_perfect_transcript():
    result = compare("where are we on Illes Foods", "Where are we on Illes Foods?")
    assert result.perfect and result.word_error_rate == 0.0


def test_a_single_misheard_account_name():
    result = compare("where are we on Illes Foods", "where are we on Ellis foods")
    assert result.substitutions == 1 and result.insertions == 0 and result.deletions == 0
    assert result.word_error_rate == pytest.approx(1 / 6)
    assert "said 'illes'  ->  heard 'ellis'" in result.render()


def test_a_name_split_into_two_words():
    result = compare("Pregis quoted us", "pre gis quoted us")
    assert result.errors == 2          # one substitution and one insertion
    assert "heard 'pre'" in result.render() or "heard 'gis'" in result.render()


def test_dropped_and_added_words_are_counted_separately():
    assert compare("a b c", "a c").deletions == 1
    assert compare("a c", "a b c").insertions == 1


def test_word_error_rate_of_an_empty_expectation():
    assert compare("", "anything at all").word_error_rate == 0.0


def test_tokens_ignore_punctuation_and_case():
    assert tokens("Where are we on Illes Foods?") == ["where", "are", "we", "on", "illes", "foods"]
