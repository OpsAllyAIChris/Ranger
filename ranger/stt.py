"""Speech to text, behind its own seam.

Deepgram's pre-recorded endpoint over plain HTTP. Push-to-talk records the whole
utterance before sending, so there is no streaming and no websocket: one POST
with the WAV, one JSON back.

Vocabulary hinting is the reason this file has more than ten lines in it. The
operator's words are packaging industry and named accounts, which is exactly
what a general model gets wrong, and the parameter Deepgram wants depends on
the model:

    nova-3 and later   keyterm=Wexxar&keyterm=Pregis      (keyterm prompting)
    nova-2 and earlier keywords=Wexxar:2&keywords=Pregis:2 (keyword boosting)

Sending the wrong one is a 400, so the model chooses the parameter rather than
the config repeating itself. If Deepgram has moved on since this was written,
the error path names the parameter it rejected instead of swallowing it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .config import SttConfig

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"

#: Models that take keyterm prompting rather than keyword boosting.
_KEYTERM_MODELS = ("nova-3",)


class TranscriptionError(Exception):
    """Deepgram could not be reached, or refused, said in a sentence."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class Word:
    text: str
    confidence: float = 1.0
    start: float = 0.0
    end: float = 0.0

    @property
    def shaky(self) -> bool:
        return self.confidence < 0.75


@dataclass(frozen=True)
class Transcript:
    text: str
    confidence: float = 0.0
    words: tuple[Word, ...] = ()
    model: str = ""
    hinted: tuple[str, ...] = ()
    latency_ms: int = 0
    audio_seconds: float = 0.0

    @property
    def empty(self) -> bool:
        return not self.text.strip()

    @property
    def shaky_words(self) -> tuple[Word, ...]:
        return tuple(w for w in self.words if w.shaky)


@runtime_checkable
class Transcriber(Protocol):
    async def transcribe(
        self, wav: bytes, *, hints: bool = True, content_type: str = "audio/wav"
    ) -> Transcript: ...


def hint_parameter(model: str) -> str:
    """Which parameter this model wants for vocabulary hinting."""
    name = (model or "").strip().lower()
    return "keyterm" if any(name.startswith(m) for m in _KEYTERM_MODELS) else "keywords"


def build_params(config: SttConfig, *, hints: bool = True) -> list[tuple[str, str]]:
    """The query string, as an ordered list so repeated keys survive."""
    params: list[tuple[str, str]] = [
        ("model", config.model),
        ("language", config.language),
        ("smart_format", "true" if config.smart_format else "false"),
        ("punctuate", "true" if config.punctuate else "false"),
    ]
    if hints and config.keyterms:
        parameter = hint_parameter(config.model)
        for term in config.keyterms[: config.max_hints]:
            # Keyword boosting takes an intensifier; keyterm prompting does not.
            params.append((parameter, term if parameter == "keyterm" else f"{term}:{config.boost}"))
    return params


def parse_response(payload: dict[str, Any], *, model: str = "") -> Transcript:
    """Pull the transcript out, without assuming every field is present."""
    try:
        channels = payload["results"]["channels"]
        alternative = channels[0]["alternatives"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise TranscriptionError(
            f"Deepgram returned a response with no transcript in it: {exc}"
        ) from exc

    words = tuple(
        Word(
            text=str(w.get("punctuated_word") or w.get("word", "")),
            confidence=float(w.get("confidence", 1.0)),
            start=float(w.get("start", 0.0)),
            end=float(w.get("end", 0.0)),
        )
        for w in alternative.get("words", []) or []
    )
    duration = 0.0
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        duration = float(metadata.get("duration", 0.0) or 0.0)

    return Transcript(
        text=str(alternative.get("transcript", "")).strip(),
        confidence=float(alternative.get("confidence", 0.0) or 0.0),
        words=words,
        model=model,
        audio_seconds=duration,
    )


class DeepgramTranscriber:
    """One POST, one JSON. No SDK, no websocket."""

    def __init__(self, config: SttConfig, api_key: str, transport: Any | None = None) -> None:
        self.config = config
        self.api_key = api_key
        self._transport = transport

    async def transcribe(
        self, wav: bytes, *, hints: bool = True, content_type: str = "audio/wav"
    ) -> Transcript:
        """`content_type` because the browser does not record WAV.

        MediaRecorder produces webm/opus, and Deepgram reads the container
        rather than trusting the header, but it does reject a body whose
        declared type contradicts its contents.
        """
        if not wav:
            raise TranscriptionError("there is no audio to transcribe.")

        import httpx2 as httpx

        params = build_params(self.config, hints=hints)
        sent_hints = tuple(v.split(":")[0] for k, v in params if k in ("keyterm", "keywords"))
        started = time.monotonic()

        try:
            async with httpx.AsyncClient(
                timeout=self.config.timeout_seconds, transport=self._transport
            ) as client:
                response = await client.post(
                    DEEPGRAM_URL,
                    params=params,
                    content=wav,
                    headers={
                        "Authorization": f"Token {self.api_key}",
                        "Content-Type": content_type or "audio/wav",
                    },
                )
        except Exception as exc:
            raise TranscriptionError(
                f"could not reach Deepgram: {exc}. Check the network and try again.",
                retryable=True,
            ) from exc

        if response.status_code != 200:
            raise TranscriptionError(_explain(response, self.config))

        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise TranscriptionError(f"Deepgram sent something that is not JSON: {exc}") from exc

        transcript = parse_response(payload, model=self.config.model)
        return Transcript(
            text=transcript.text,
            confidence=transcript.confidence,
            words=transcript.words,
            model=self.config.model,
            hinted=sent_hints,
            latency_ms=int((time.monotonic() - started) * 1000),
            audio_seconds=transcript.audio_seconds,
        )


def _explain(response: Any, config: SttConfig) -> str:
    body = ""
    try:
        body = response.text[:300]
    except Exception:
        pass
    status = response.status_code

    if status in (401, 403):
        return (
            "Deepgram rejected the API key. Check DEEPGRAM_API_KEY in .env, and that the "
            "key is allowed to use the transcription endpoint."
        )
    if status == 400:
        parameter = hint_parameter(config.model)
        return (
            f"Deepgram refused the request (400): {body}. If it names {parameter!r}, this "
            f"model does not take that hinting parameter. Either change stt.model, or set "
            "stt.keyterms = [] to send no hints at all."
        )
    if status == 402:
        return "Deepgram says the account is out of credit."
    if status == 429:
        return "Deepgram is rate limiting. Wait and try again."
    if status >= 500:
        return f"Deepgram is having trouble ({status}). Try again shortly."
    return f"Deepgram returned {status}: {body}"


def build_transcriber(config: SttConfig, api_key: str) -> Transcriber:
    if config.provider == "deepgram":
        return DeepgramTranscriber(config, api_key)
    raise ValueError(
        f"unknown stt.provider {config.provider!r}; the only implementation is 'deepgram'"
    )
