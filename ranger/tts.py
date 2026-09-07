"""Text to speech, behind its own seam.

ElevenLabs over plain HTTP, streaming, no SDK. The operator's key is scoped to
Text to Speech plus Voices read only, and those are the only two endpoints this
file touches:

    POST /v1/text-to-speech/{voice_id}/stream   speak
    GET  /v1/voices                             list voices, for auditioning

Audio comes back as raw PCM where the account allows it, which plays straight
through sounddevice with no decoder. Where it does not, MP3 comes back and
soundfile decodes it. The format is config, so switching is one line.

Latency is the whole experience with push to talk, so this streams and yields
chunks as they arrive rather than waiting for the whole clip.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from .config import TtsConfig

API_ROOT = "https://api.elevenlabs.io/v1"

#: pcm_<rate> plays with no decoding at all. Everything else needs soundfile.
_PCM = re.compile(r"^pcm_(\d+)$")


class SpeechError(Exception):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class Voice:
    voice_id: str
    name: str
    labels: dict[str, str]
    preview_url: str = ""

    def describe(self) -> str:
        bits = [v for k, v in self.labels.items() if k in ("accent", "gender", "age", "use_case", "description")]
        return ", ".join(b for b in bits if b)


@runtime_checkable
class Speaker(Protocol):
    async def stream(self, text: str) -> AsyncIterator[bytes]: ...


def output_samplerate(output_format: str) -> int | None:
    """The rate to play PCM at, or None when the bytes need decoding first."""
    match = _PCM.match(output_format.strip().lower())
    return int(match.group(1)) if match else None


def decode(audio: bytes, output_format: str) -> tuple[bytes, int]:
    """Return 16 bit PCM and its sample rate, decoding only when necessary."""
    rate = output_samplerate(output_format)
    if rate is not None:
        return audio, rate

    try:
        import io

        import soundfile
    except ImportError as exc:
        raise SpeechError(
            f"tts.output_format is {output_format!r}, which is not raw PCM, so it has to be "
            "decoded. Run: pip install soundfile. Or set output_format to pcm_24000 if your "
            "ElevenLabs plan allows it, which needs no decoder at all."
        ) from exc

    try:
        data, rate = soundfile.read(io.BytesIO(audio), dtype="int16", always_2d=False)
    except Exception as exc:
        raise SpeechError(f"could not decode the {output_format} audio: {exc}") from exc
    return data.tobytes(), int(rate)


class ElevenLabsSpeaker:
    def __init__(self, config: TtsConfig, api_key: str, transport: Any | None = None) -> None:
        self.config = config
        self.api_key = api_key
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        return {"xi-api-key": self.api_key, "Accept": "*/*"}

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        """Yield audio as it arrives, so speech can start before it is finished."""
        text = text.strip()
        if not text:
            return
        if not self.config.voice_id:
            raise SpeechError(
                "tts.voice_id is not set in ranger.toml. Run 'ranger voices' to see the "
                "voices on the account and pick one."
            )

        import httpx2 as httpx

        body = {
            "text": text,
            "model_id": self.config.model_id,
            "voice_settings": {
                "stability": self.config.stability,
                "similarity_boost": self.config.similarity_boost,
                "speed": self.config.speed,
            },
        }
        url = f"{API_ROOT}/text-to-speech/{self.config.voice_id}/stream"

        try:
            async with httpx.AsyncClient(
                timeout=self.config.timeout_seconds, transport=self._transport
            ) as client:
                async with client.stream(
                    "POST",
                    url,
                    params={"output_format": self.config.output_format},
                    json=body,
                    headers=self._headers(),
                ) as response:
                    if response.status_code != 200:
                        await response.aread()
                        raise SpeechError(_explain(response, self.config))
                    async for chunk in response.aiter_bytes():
                        if chunk:
                            yield chunk
        except SpeechError:
            raise
        except Exception as exc:
            raise SpeechError(
                f"could not reach ElevenLabs: {exc}. Check the network and try again.",
                retryable=True,
            ) from exc

    async def voices(self) -> list[Voice]:
        import httpx2 as httpx

        try:
            async with httpx.AsyncClient(
                timeout=self.config.timeout_seconds, transport=self._transport
            ) as client:
                response = await client.get(f"{API_ROOT}/voices", headers=self._headers())
        except Exception as exc:
            raise SpeechError(f"could not reach ElevenLabs: {exc}", retryable=True) from exc

        if response.status_code != 200:
            raise SpeechError(_explain(response, self.config))

        try:
            payload = response.json()
        except Exception as exc:
            raise SpeechError(f"ElevenLabs sent something that is not JSON: {exc}") from exc

        found: list[Voice] = []
        for item in payload.get("voices", []) or []:
            found.append(
                Voice(
                    voice_id=str(item.get("voice_id", "")),
                    name=str(item.get("name", "")),
                    labels={str(k): str(v) for k, v in (item.get("labels") or {}).items()},
                    preview_url=str(item.get("preview_url", "") or ""),
                )
            )
        return found


def _explain(response: Any, config: TtsConfig) -> str:
    body = ""
    try:
        body = response.text[:300]
    except Exception:
        pass
    status = response.status_code

    if status == 401:
        return (
            "ElevenLabs rejected the API key. Check ELEVENLABS_API_KEY in .env, and that it "
            "is scoped to Text to Speech and Voices."
        )
    if status == 403:
        return (
            f"ElevenLabs refused ({status}): {body}. The key may not be scoped to this "
            "endpoint. Ranger needs Text to Speech and Voices read only, nothing else."
        )
    if status == 404:
        return (
            f"ElevenLabs does not know voice {config.voice_id!r}. Run 'ranger voices' and "
            "copy an id from the list into tts.voice_id."
        )
    if status == 422:
        return (
            f"ElevenLabs refused the request (422): {body}. If it names output_format, the "
            f"plan may not allow {config.output_format!r}; mp3_44100_128 is available on "
            "every plan and needs soundfile to decode."
        )
    if status == 429:
        return "ElevenLabs is rate limiting, or the character quota is spent."
    if status >= 500:
        return f"ElevenLabs is having trouble ({status}). Try again shortly."
    return f"ElevenLabs returned {status}: {body}"


def build_speaker(config: TtsConfig, api_key: str) -> ElevenLabsSpeaker:
    if config.provider == "elevenlabs":
        return ElevenLabsSpeaker(config, api_key)
    raise ValueError(
        f"unknown tts.provider {config.provider!r}; the only implementation is 'elevenlabs'"
    )
