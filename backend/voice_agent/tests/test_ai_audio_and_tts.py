"""AI voice conversation: WAV -> Vobiz playback conversion and the Sarvam TTS client.

Everything is mocked: Sarvam is an httpx.MockTransport. No network, no real credentials.
"""

from __future__ import annotations

import base64
import json
import struct

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.services.audio.convert import (
    AudioConversionError,
    decode_wav,
    resample_pcm16,
    wav_to_playback,
)
from app.services.audio.formats import ENC_L16, AudioFormat
from app.services.sarvam.errors import (
    SarvamInvalidAudioError,
    SarvamRateLimitError,
    SarvamRequestError,
    SarvamServerError,
    SarvamTimeoutError,
)
from app.services.sarvam.tts import SarvamTtsClient

KEY = "sk-test-SECRET-tts-key"


def make_wav(samples: list[int], rate: int = 16000, channels: int = 1, bits: int = 16, tag: int = 1,
             data_size: int | None = None, extensible: bool = False) -> bytes:
    pcm = struct.pack(f"<{len(samples)}h", *samples)
    if extensible:
        fmt = struct.pack("<HHIIHHHHIH14s", 0xFFFE, channels, rate, rate * channels * bits // 8,
                          channels * bits // 8, bits, 22, bits, 0, tag, b"\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71")
    else:
        fmt = struct.pack("<HHIIHH", tag, channels, rate, rate * channels * bits // 8, channels * bits // 8, bits)
    size = len(pcm) if data_size is None else data_size
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", size) + pcm
    return b"RIFF" + struct.pack("<I", len(body)) + body


def tone_samples(n: int) -> list[int]:
    return [((i * 37) % 2000) - 1000 for i in range(n)]


# --- conversion ----------------------------------------------------------------------------------


def test_playable_rate_passes_through_unchanged_little_endian() -> None:
    samples = [0x0102, -2, 300, 7] * 200
    audio = wav_to_playback(make_wav(samples, rate=16000))
    assert audio.format == AudioFormat(ENC_L16, 16000)
    assert audio.pcm == struct.pack(f"<{len(samples)}h", *samples)  # same bytes: header removed only
    assert audio.pcm[:2] == b"\x02\x01"  # little-endian, as Vobiz outbound L16 requires
    assert not audio.resampled and audio.source_sample_rate == 16000


@pytest.mark.parametrize("rate", [8000, 24000])
def test_other_vobiz_rates_are_kept(rate: int) -> None:
    audio = wav_to_playback(make_wav(tone_samples(rate // 5), rate=rate))
    assert audio.format == AudioFormat(ENC_L16, rate) and not audio.resampled
    assert audio.duration_ms == pytest.approx(200, abs=1)


def test_unsupported_rate_is_resampled_to_16k() -> None:
    audio = wav_to_playback(make_wav(tone_samples(22050), rate=22050))  # 1 s
    assert audio.format == AudioFormat(ENC_L16, 16000)
    assert audio.resampled and audio.source_sample_rate == 22050
    assert audio.duration_ms == pytest.approx(1000, abs=2)
    assert len(audio.pcm) % 2 == 0


def test_stereo_is_downmixed_to_mono() -> None:
    left_right = [1000, 3000] * 400
    wav = decode_wav(make_wav(left_right, channels=2))
    assert wav.source_channels == 2
    assert struct.unpack(f"<{len(wav.pcm) // 2}h", wav.pcm) == (2000,) * 400


def test_streamed_header_with_unknown_data_size_is_accepted() -> None:
    for size in (0xFFFFFFFF, 0):
        assert len(decode_wav(make_wav(tone_samples(800), data_size=size)).pcm) == 1600


def test_extensible_pcm_wav_is_accepted() -> None:
    assert len(decode_wav(make_wav(tone_samples(800), extensible=True)).pcm) == 1600


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not a wav file at all",
        b"ID3\x04" + b"\x00" * 64,  # mp3
        make_wav(tone_samples(100), tag=3),  # float
        make_wav(tone_samples(100), bits=8),
        make_wav([]),  # no samples
        make_wav([1, 2, 3], rate=16000),  # shorter than one 20 ms frame
    ],
)
def test_invalid_audio_is_rejected(data: bytes) -> None:
    with pytest.raises(AudioConversionError):
        wav_to_playback(data)


def test_resample_halves_and_doubles_length() -> None:
    pcm = struct.pack("<400h", *tone_samples(400))
    assert len(resample_pcm16(pcm, 16000, 8000)) == len(pcm) // 2
    assert len(resample_pcm16(pcm, 8000, 16000)) == len(pcm) * 2
    assert resample_pcm16(pcm, 8000, 8000) == pcm


# --- Sarvam TTS client ---------------------------------------------------------------------------


def settings(**kw) -> Settings:
    values = dict(sarvam_api_key=SecretStr(KEY), sarvam_tts_language="en-IN", sarvam_tts_speaker="simran_en_customer",
                  sarvam_tts_model="bulbul:v4-flash", sarvam_tts_sample_rate=16000)
    values.update(kw)
    return Settings(**values)


def tts_client(handler, **kw) -> SarvamTtsClient:
    s = settings(**kw)
    http = httpx.AsyncClient(base_url=s.sarvam_base_url, transport=httpx.MockTransport(handler))
    return SarvamTtsClient(s, http, timeout_s=2.0)


def ok_response(wav: bytes, request_id: str = "req-1") -> httpx.Response:
    return httpx.Response(200, json={"request_id": request_id, "audios": [base64.b64encode(wav).decode()]})


async def test_request_shape_and_secret_handling() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok_response(make_wav(tone_samples(1600)))

    audio = await tts_client(handler).synthesize("  Hello there  ")
    request = seen[0]
    body = json.loads(request.content)
    assert request.method == "POST" and request.url.path == "/text-to-speech"
    assert body == {
        "text": "Hello there", "language_code": "en-IN", "model": "bulbul:v4-flash",
        "speech_sample_rate": 16000, "output_audio_codec": "wav", "speaker": "simran_en_customer",
    }
    assert request.headers["api-subscription-key"] == KEY
    assert KEY not in str(request.url) and KEY.encode() not in request.content  # header only
    assert audio.format == AudioFormat(ENC_L16, 16000)
    assert (audio.source_codec, audio.source_sample_rate, audio.request_id) == ("wav", 16000, "req-1")
    assert audio.duration_ms == pytest.approx(100, abs=1) and audio.latency_ms >= 0


async def test_speaker_is_omitted_when_not_configured() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return ok_response(make_wav(tone_samples(1600)))

    await tts_client(handler, sarvam_tts_speaker="").synthesize("Hi")
    assert "speaker" not in bodies[0]


async def test_unexpected_rate_from_sarvam_is_converted_not_assumed() -> None:
    audio = await tts_client(lambda r: ok_response(make_wav(tone_samples(22050), rate=22050))).synthesize("Hi")
    assert audio.format == AudioFormat(ENC_L16, 16000) and audio.resampled and audio.source_sample_rate == 22050


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"request_id": "r", "audios": []}),
        httpx.Response(200, json={"request_id": "r"}),
        httpx.Response(200, json={"request_id": "r", "audios": [""]}),
        httpx.Response(200, json={"request_id": "r", "audios": ["!!!not-base64!!!"]}),
        httpx.Response(200, json={"request_id": "r", "audios": [base64.b64encode(b"plain bytes, not a wav").decode()]}),
        httpx.Response(200, json={"request_id": "r", "audios": [base64.b64encode(make_wav([])).decode()]}),
    ],
)
async def test_invalid_audio_responses_are_rejected(response: httpx.Response) -> None:
    with pytest.raises(SarvamInvalidAudioError) as info:
        await tts_client(lambda r: response).synthesize("Hi")
    assert KEY not in str(info.value)


@pytest.mark.parametrize(
    "status, error",
    [(429, SarvamRateLimitError), (500, SarvamServerError), (400, SarvamRequestError)],
)
async def test_http_errors_map_to_sarvam_errors_without_leaking_the_key(status: int, error: type) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "nope", "code": "x", "request_id": "r"}})

    with pytest.raises(error) as info:
        await tts_client(handler).synthesize("Hi")
    assert KEY not in str(info.value)


async def test_transport_timeout_maps_to_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(SarvamTimeoutError):
        await tts_client(handler).synthesize("Hi")


async def test_empty_and_oversized_text_are_rejected_locally() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return ok_response(make_wav(tone_samples(1600)))

    client = tts_client(handler)
    with pytest.raises(SarvamRequestError):
        await client.synthesize("   ")
    with pytest.raises(SarvamRequestError):
        await client.synthesize("x" * 2501)
    assert calls == []
