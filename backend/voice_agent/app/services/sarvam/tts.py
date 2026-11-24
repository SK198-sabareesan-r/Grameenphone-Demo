"""Sarvam text-to-speech (REST) for the voice conversation.

API (docs.sarvam.ai, "Text-to-Speech > REST API"): POST {base}/text-to-speech, header
`api-subscription-key`, JSON body {text (1-2500 chars), language_code, model, speaker, speech_sample_rate,
output_audio_codec}. The response is JSON {request_id, audios: [base64 audio]}. Sarvam ignores unknown
request fields without an error, so field names matter and are covered by tests.

This client always asks for WAV (the default codec): a self-describing, little-endian PCM container whose
rate and layout are read from the file itself, never assumed. `wav_to_playback` then produces the raw
L16 audio and exact format Vobiz needs. The API key is only ever sent in the header by SarvamClient.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import time
from dataclasses import dataclass, field

import httpx

from app.core.config import Settings
from app.services.audio.convert import PlaybackAudio, wav_to_playback
from app.services.audio.formats import AudioFormat, AudioFormatError
from app.services.sarvam.client import SarvamClient
from app.services.sarvam.constants import TTS_MAX_CHARS, TTS_PATH
from app.services.sarvam.errors import (
    SarvamInvalidAudioError,
    SarvamRequestError,
    SarvamTimeoutError,
)

MAX_AUDIO_SECONDS = 60.0


@dataclass(frozen=True)
class TtsAudio:
    """Synthesized speech ready for Vobiz playback, with the metadata of what Sarvam actually returned."""

    pcm: bytes = field(repr=False)
    format: AudioFormat  # declare this in playAudio
    duration_ms: float
    source_codec: str  # what we requested and received from Sarvam
    source_sample_rate: int
    resampled: bool
    request_id: str | None
    latency_ms: float


class SarvamTtsClient:
    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
        *,
        timeout_s: float | None = None,
    ) -> None:
        self._settings = settings
        self._sarvam = SarvamClient(settings, http_client)
        self._timeout_s = timeout_s if timeout_s is not None else settings.ai_tts_timeout_s

    async def aclose(self) -> None:
        await self._sarvam.aclose()

    def request_body(self, text: str) -> dict[str, object]:
        s = self._settings
        body: dict[str, object] = {
            "text": text,
            "language_code": s.sarvam_tts_language,
            "model": s.sarvam_tts_model,
            "speech_sample_rate": s.sarvam_tts_sample_rate,
            "output_audio_codec": "wav",
        }
        if s.sarvam_tts_speaker:  # empty = Sarvam's per-model default speaker
            body["speaker"] = s.sarvam_tts_speaker
        return body

    async def synthesize(self, text: str) -> TtsAudio:
        text = text.strip()
        if not text:
            raise SarvamRequestError("text to synthesize is empty")
        if len(text) > TTS_MAX_CHARS:
            raise SarvamRequestError(f"text is longer than the {TTS_MAX_CHARS}-character TTS limit")
        started = time.monotonic()
        try:
            data = await asyncio.wait_for(
                self._sarvam.request(
                    "POST", TTS_PATH, json=self.request_body(text), timeout=httpx.Timeout(self._timeout_s)
                ),
                self._timeout_s + 1.0,
            )
        except asyncio.TimeoutError:
            raise SarvamTimeoutError("Sarvam TTS request timed out.") from None

        audios = data.get("audios") if isinstance(data, dict) else None
        if not isinstance(audios, list) or not audios or not isinstance(audios[0], str) or not audios[0]:
            raise SarvamInvalidAudioError("Sarvam TTS response has no audio.")
        try:
            wav_bytes = base64.b64decode(audios[0], validate=True)
        except (binascii.Error, ValueError):
            raise SarvamInvalidAudioError("Sarvam TTS audio is not valid base64.") from None
        try:
            playback: PlaybackAudio = await asyncio.to_thread(wav_to_playback, wav_bytes)
        except AudioFormatError as exc:
            raise SarvamInvalidAudioError(f"Sarvam TTS audio is not usable: {exc}") from None
        if playback.duration_ms > MAX_AUDIO_SECONDS * 1000:
            raise SarvamInvalidAudioError("Sarvam TTS audio is unexpectedly long.")
        request_id = data.get("request_id")
        return TtsAudio(
            pcm=playback.pcm,
            format=playback.format,
            duration_ms=playback.duration_ms,
            source_codec="wav",
            source_sample_rate=playback.source_sample_rate,
            resampled=playback.resampled,
            request_id=request_id if isinstance(request_id, str) else None,
            latency_ms=(time.monotonic() - started) * 1000,
        )
