"""LIVE provider tests. NOT part of the standard suite (pytest.ini testpaths = tests).

They call the real Sarvam API, use your SARVAM_API_KEY and consume a few seconds of billed audio.
Run explicitly:

    $env:RUN_LIVE_SARVAM="1"
    $env:SARVAM_LIVE_SPEECH_WAV="C:\\recordings\\en_16k.wav"           # 16 kHz mono 16-bit PCM
    $env:SARVAM_LIVE_EXPECTED="My internet connection is not working"  # what is actually said
    $env:SARVAM_LIVE_LANGUAGE="en-IN"                                  # optional
    .\\.venv\\Scripts\\python.exe -m pytest tests_live -v -s

Optional Vobiz capture check (after a controlled test call with dev capture enabled):
    $env:VOBIZ_LIVE_CAPTURE="C:\\...\\vobiz-captures\\<file>.l16"
    $env:VOBIZ_LIVE_EXPECTED="<phrase you spoke on the call>"
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.services.audio.byte_order import compare_byte_orders, to_little_endian  # noqa: E402
from app.services.audio.wav import read_pcm16_mono  # noqa: E402
from app.services.sarvam.realtime_protocol import RealtimeSttConfig  # noqa: E402
from app.services.sarvam.speech_check import run_speech_check, summarize  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("RUN_LIVE_SARVAM") != "1", reason="set RUN_LIVE_SARVAM=1")


def _config(language: str | None) -> tuple[RealtimeSttConfig, Settings]:
    settings = Settings.from_env()
    if not settings.sarvam_api_key.get_secret_value():
        pytest.skip("SARVAM_API_KEY is not set")
    config = RealtimeSttConfig.from_settings(settings)
    if language:
        config = replace(config, language_code=language).validated()
    return config, settings


def test_live_handshake() -> None:
    config, settings = _config(None)
    result = asyncio.run(run_speech_check(b"\x00\x00" * 16000, config, settings.sarvam_api_key))
    print("\n".join(summarize(result)))
    assert result.request_id and result.final_state == "closed" and result.errors == []


def test_live_real_speech_transcription() -> None:
    wav = os.environ.get("SARVAM_LIVE_SPEECH_WAV")
    if not wav:
        pytest.skip("set SARVAM_LIVE_SPEECH_WAV to a real 16 kHz mono 16-bit speech recording")
    config, settings = _config(os.environ.get("SARVAM_LIVE_LANGUAGE"))
    pcm, _ = read_pcm16_mono(wav)
    expected = os.environ.get("SARVAM_LIVE_EXPECTED")
    result = asyncio.run(run_speech_check(pcm, config, settings.sarvam_api_key, expected=expected))
    print("\n".join(summarize(result)))
    assert result.finals, "no transcript.final received"
    assert all(e["is_fatal"] is False for e in result.errors)
    if expected:
        assert result.word_error_rate is not None and result.word_error_rate <= 0.5


def test_live_vobiz_capture_byte_order_and_transcript() -> None:
    capture = os.environ.get("VOBIZ_LIVE_CAPTURE")
    if not capture:
        pytest.skip("set VOBIZ_LIVE_CAPTURE to a .l16 capture from a controlled test call")
    raw = Path(capture).read_bytes()
    verdict = compare_byte_orders(raw)
    print(f"\nsuggested byte order: {verdict.suggested} ({verdict.strength}); " + "; ".join(verdict.reasons))
    if verdict.suggested is None:
        pytest.fail("byte order is ambiguous: re-capture while speaking clearly for several seconds")
    config, settings = _config(os.environ.get("SARVAM_LIVE_LANGUAGE"))
    result = asyncio.run(run_speech_check(to_little_endian(raw, verdict.suggested), config, settings.sarvam_api_key,
                                          expected=os.environ.get("VOBIZ_LIVE_EXPECTED")))
    print("\n".join(summarize(result)))
    assert result.finals, "no transcript.final for the capture"
