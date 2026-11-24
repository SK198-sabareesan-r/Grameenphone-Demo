"""WAV validation and L16 byte-order analysis (M4.2). Unit tests on synthetic signals only.

Synthetic signals here test the ANALYSIS LOGIC. They are not evidence about Vobiz: that requires a
real capture of speech, a listening test and a transcript comparison.
"""

from __future__ import annotations

import math
import os
import random
import struct
import wave

import pytest

from app.integrations.vobiz import media_config
from app.services.audio.byte_order import (
    analyze_l16,
    compare_byte_orders,
    decode_l16,
    swap_l16,
    to_little_endian,
)
from app.services.audio.formats import AudioFormat
from app.services.audio.wav import WavFormatError, inspect_wav, read_pcm16_mono, write_pcm16_mono


def speech_like(seconds: float = 2.0, rate: int = 16000, level: int = 6000, seed: int = 7) -> list[int]:
    """Low-pass-filtered noise with a syllable envelope: correlated like speech (not a pure tone)."""
    rng = random.Random(seed)
    out, y = [], 0.0
    for n in range(int(seconds * rate)):
        y = 0.92 * y + 0.08 * rng.uniform(-1, 1)  # one-pole low-pass
        envelope = 0.25 + 0.75 * abs(math.sin(math.pi * n / (0.25 * rate)))
        out.append(max(-32768, min(32767, int(y * level * 8 * envelope))))
    return out


def pack(samples: list[int], order: str) -> bytes:
    return struct.pack(f"{'<' if order == 'little' else '>'}{len(samples)}h", *samples)


def write_wav(path, *, channels=1, width=2, rate=16000, frames: bytes | None = None) -> str:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(frames if frames is not None else b"\x00" * (width * channels * rate // 10))
    return str(path)


# --- WAV validation --------------------------------------------------------------------------


def test_valid_wav_returns_headerless_little_endian_pcm(tmp_path) -> None:
    samples = [0, 1, -1, 1000, -1000, 32767, -32768]
    path = write_wav(tmp_path / "ok.wav", frames=pack(samples, "little"))
    pcm, info = read_pcm16_mono(path)
    assert pcm == pack(samples, "little")  # header stripped, bytes unchanged
    assert not pcm.startswith(b"RIFF")
    assert (info.channels, info.sample_width, info.sample_rate, info.frames) == (1, 2, 16000, len(samples))


@pytest.mark.parametrize(
    ("kwargs", "problem"),
    [({"channels": 2}, "2 channels"), ({"rate": 8000}, "8000 Hz"), ({"rate": 44100}, "44100 Hz"),
     ({"width": 1}, "8-bit"), ({"width": 3}, "24-bit")],
)
def test_wrong_wav_parameters_are_rejected_not_converted(tmp_path, kwargs: dict, problem: str) -> None:
    path = write_wav(tmp_path / "bad.wav", **kwargs)
    with pytest.raises(WavFormatError) as info:
        read_pcm16_mono(path)
    assert problem in str(info.value) and "ffmpeg" in str(info.value)


def test_empty_wav_is_rejected(tmp_path) -> None:
    with pytest.raises(WavFormatError):
        read_pcm16_mono(write_wav(tmp_path / "empty.wav", frames=b""))


@pytest.mark.parametrize("content", [b"", b"not audio at all", b"ID3\x04\x00" + b"\x00" * 64])
def test_non_wav_files_are_rejected(tmp_path, content: bytes) -> None:
    path = tmp_path / "x.wav"
    path.write_bytes(content)
    with pytest.raises(WavFormatError):
        inspect_wav(str(path))


def test_float_wav_is_rejected(tmp_path) -> None:
    # Minimal IEEE-float (format 3) WAV header: Python's wave module refuses it.
    data = struct.pack("<4f", 0.0, 0.1, -0.1, 0.0)
    fmt = struct.pack("<HHIIHH", 3, 1, 16000, 64000, 4, 32)
    riff = b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(data)) + b"WAVE"
    path = tmp_path / "float.wav"
    path.write_bytes(riff + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data)
    with pytest.raises(WavFormatError):
        read_pcm16_mono(str(path))


def test_write_then_read_round_trip(tmp_path) -> None:
    pcm = pack(speech_like(0.3), "little")
    path = str(tmp_path / "rt.wav")
    write_pcm16_mono(path, pcm, 16000)
    assert read_pcm16_mono(path)[0] == pcm
    assert not os.path.exists(path + ".tmp")
    with pytest.raises(WavFormatError):
        write_pcm16_mono(path, b"\x00\x01\x02", 16000)


# --- byte-order interpretation ---------------------------------------------------------------


def test_decode_is_explicit_about_byte_order() -> None:
    raw = b"\x01\x02\xff\x7f"
    assert decode_l16(raw, "little") == (0x0201, 0x7FFF)
    assert decode_l16(raw, "big") == (0x0102, -129)
    with pytest.raises(ValueError):
        decode_l16(b"\x00", "little")


def test_swap_and_to_little_endian() -> None:
    samples = speech_like(0.1)
    little, big = pack(samples, "little"), pack(samples, "big")
    assert swap_l16(big) == little and swap_l16(little) == big
    assert to_little_endian(little, "little") is little  # untouched, not copied or swapped
    assert to_little_endian(big, "big") == little
    with pytest.raises(ValueError):
        swap_l16(b"\x00\x01\x02")


def test_speech_like_little_endian_capture_is_identified() -> None:
    verdict = compare_byte_orders(pack(speech_like(), "little"))
    assert verdict.suggested == "little" and verdict.strength == "strong"
    assert verdict.little.lag1_autocorr > 0.6 and verdict.big.lag1_autocorr < 0.3
    assert verdict.little.zero_crossing_rate < verdict.big.zero_crossing_rate


def test_speech_like_big_endian_capture_is_identified() -> None:
    verdict = compare_byte_orders(pack(speech_like(), "big"))
    assert verdict.suggested == "big" and verdict.strength == "strong"


def test_quiet_speech_like_signal_is_still_identified() -> None:
    verdict = compare_byte_orders(pack(speech_like(level=300), "little"))
    assert verdict.suggested == "little"


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        (b"\x00\x00" * 32000, "digital silence"),
        (pack(speech_like(0.2), "little"), "too short"),
        (pack([random.Random(1).randint(-20000, 20000) for _ in range(32000)], "little"), "white noise"),
        (pack([random.Random(2).randint(-20, 20) for _ in range(32000)], "little"), "no signal"),
    ],
)
def test_ambiguous_inputs_are_not_given_a_verdict(raw: bytes, why: str) -> None:
    verdict = compare_byte_orders(raw)
    assert verdict.suggested is None and verdict.strength == "ambiguous", why
    assert verdict.reasons


def test_analysis_metrics_are_sane() -> None:
    stats = analyze_l16(pack([32767, -32768, 0, 0], "little"), "little")
    assert stats.peak == 32768 and stats.clip_fraction == 0.5 and stats.near_silent_fraction == 0.5
    empty = analyze_l16(b"", "little")
    assert empty.samples == 0 and empty.rms == 0.0


# --- existing audio format configuration -----------------------------------------------------


def test_media_config_records_verified_inbound_and_outbound_byte_order() -> None:
    assert media_config.VOBIZ_INBOUND_FORMAT == AudioFormat("audio/x-l16", 16000)
    assert media_config.VOBIZ_INBOUND_L16_BYTE_ORDER == "little"  # verified by M4.2 capture + transcript
    assert media_config.VOBIZ_OUTBOUND_L16_BYTE_ORDER == "little"
    assert media_config.inbound_byte_order(media_config.VOBIZ_INBOUND_FORMAT) == "little"
    assert media_config.inbound_byte_order(AudioFormat("audio/x-mulaw", 8000)) is None


def test_verified_inbound_order_means_no_conversion_for_linear16() -> None:
    raw = pack(speech_like(0.5), "little")
    assert to_little_endian(raw, media_config.VOBIZ_INBOUND_L16_BYTE_ORDER) is raw  # bytes pass through as-is
