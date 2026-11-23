"""Sarvam text-to-speech. Used by the replay script to make telephone-quality test audio."""

import base64

import httpx

from config.settings import Settings


def strip_wav_header(data: bytes) -> bytes:
    """Return raw samples when the API wraps them in a WAV container."""
    if data[:4] != b"RIFF":
        return data
    marker = data.find(b"data")
    return data[marker + 8 :] if marker > 0 else data


async def synthesize_mulaw(settings: Settings, text: str, speaker: str) -> bytes:
    """Return 8 kHz mulaw audio for the text, ready to stream as a phone call."""
    key = settings.sarvam_api_key.get_secret_value()
    body = {
        "text": text,
        "language_code": settings.sarvam_tts_language,
        "speech_sample_rate": 8000,
        "output_audio_codec": "mulaw",
    }
    if settings.sarvam_tts_model:
        body["model"] = settings.sarvam_tts_model
    if speaker:
        body["speaker"] = speaker
    async with httpx.AsyncClient(timeout=30) as client:
        reply = await client.post(
            settings.sarvam_tts_url, json=body, headers={"api-subscription-key": key}
        )
        reply.raise_for_status()
    audio_b64 = reply.json()["audios"][0]
    return strip_wav_header(base64.b64decode(audio_b64))
