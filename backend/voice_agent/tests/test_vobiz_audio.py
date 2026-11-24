"""Milestone 3: audio format validation, decoding, bounded queue, synthetic audio. No network."""

from __future__ import annotations

import asyncio
import base64
import os
import struct

import pytest

from app.services.audio.audio_buffer import AudioFrame, BoundedFrameQueue
from app.services.audio.formats import (
    ENC_L16,
    ENC_MULAW,
    AudioFormat,
    AudioFormatError,
    decode_media_payload,
    inbound_format,
    looks_like_container,
    outbound_format,
    validate_raw_audio,
)
from app.services.audio.synthetic import linear_to_mulaw, silence, tone

L16_8K = AudioFormat(ENC_L16, 8000)
L16_16K = AudioFormat(ENC_L16, 16000)
L16_24K = AudioFormat(ENC_L16, 24000)
MULAW_8K = AudioFormat(ENC_MULAW, 8000)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# --- formats ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "bytes_20ms", "bytes_per_sample"),
    [(L16_8K, 320, 2), (L16_16K, 640, 2), (L16_24K, 960, 2), (MULAW_8K, 160, 1)],
)
def test_frame_sizes_match_vobiz_documentation(fmt: AudioFormat, bytes_20ms: int, bytes_per_sample: int) -> None:
    assert fmt.bytes_for_ms(20) == bytes_20ms
    assert fmt.bytes_per_sample == bytes_per_sample
    assert fmt.duration_ms(bytes_20ms) == pytest.approx(20.0)


@pytest.mark.parametrize(
    ("encoding", "rate"),
    [("audio/x-l16", 8000), ("audio/x-l16", 16000), ("audio/x-mulaw", 8000), ("audio/x-l16", "16000")],
)
def test_supported_inbound_formats(encoding: str, rate) -> None:
    fmt = inbound_format(encoding, rate)
    assert fmt.encoding == encoding and fmt.sample_rate == int(rate)  # metadata preserved as announced


@pytest.mark.parametrize(
    ("encoding", "rate"),
    [
        ("audio/x-l16", 24000),  # outbound-only rate
        ("audio/x-mulaw", 16000),
        ("audio/x-l16", 44100),
        ("audio/wav", 8000),
        ("audio/x-alaw", 8000),
        ("", 8000),
        (None, 8000),
        ("audio/x-l16", None),
        ("audio/x-l16", True),
        ("audio/x-l16", 8000.5),
        ("audio/x-l16", "8k"),
    ],
)
def test_unsupported_inbound_formats_rejected(encoding, rate) -> None:
    with pytest.raises(AudioFormatError):
        inbound_format(encoding, rate)


def test_outbound_formats_include_24khz_l16_only_for_playback() -> None:
    assert outbound_format("audio/x-l16", 24000) == L16_24K
    for encoding, rate in (("audio/x-mulaw", 16000), ("audio/x-mulaw", 24000), ("audio/x-l16", 12000)):
        with pytest.raises(AudioFormatError):
            outbound_format(encoding, rate)


# --- decoding --------------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", [L16_8K, L16_16K, MULAW_8K])
def test_decode_preserves_raw_bytes_exactly(fmt: AudioFormat) -> None:
    raw = os.urandom(fmt.bytes_for_ms(20))
    assert decode_media_payload(b64(raw), fmt) == raw


def test_decode_does_not_touch_byte_order() -> None:
    little = struct.pack("<4h", 1, 2, 3, 4)
    big = struct.pack(">4h", 1, 2, 3, 4)
    assert decode_media_payload(b64(little), L16_8K) == little
    assert decode_media_payload(b64(big), L16_8K) == big
    assert little != big


@pytest.mark.parametrize(
    "payload",
    ["", None, 123, "not base64!!", "AAA", "AA=A", "AAAA\nAAAA", "AAAA AAAA", b64(b"\x00\x00")[:-1]],
)
def test_invalid_payloads_rejected(payload) -> None:
    with pytest.raises(AudioFormatError):
        decode_media_payload(payload, L16_8K)


def test_l16_payload_must_be_whole_samples() -> None:
    with pytest.raises(AudioFormatError):
        decode_media_payload(b64(b"\x01\x02\x03"), L16_8K)
    assert decode_media_payload(b64(b"\x01\x02\x03"), MULAW_8K) == b"\x01\x02\x03"  # 1-byte samples are fine


def test_oversized_frames_rejected_before_decoding() -> None:
    too_big = b64(b"\x00" * (L16_8K.bytes_for_ms(200) + 2))
    with pytest.raises(AudioFormatError):
        decode_media_payload(too_big, L16_8K)
    at_limit = b"\x00" * L16_8K.bytes_for_ms(200)
    assert decode_media_payload(b64(at_limit), L16_8K) == at_limit
    with pytest.raises(AudioFormatError):
        decode_media_payload(b64(b"\x00" * 100_000), MULAW_8K, max_frame_ms=20)


def test_non_20ms_frames_are_accepted() -> None:
    raw = os.urandom(L16_16K.bytes_for_ms(30))
    assert decode_media_payload(b64(raw), L16_16K) == raw


# --- raw-audio validation for outbound -------------------------------------------------------


def test_container_headers_detected() -> None:
    wav = b"RIFF" + struct.pack("<I", 36) + b"WAVEfmt " + b"\x00" * 40
    assert looks_like_container(wav)
    for header in (b"ID3\x04\x00\x00", b"OggS\x00\x02", b"fLaC\x00\x00"):
        assert looks_like_container(header + b"\x00" * 32)
    assert not looks_like_container(b"\x00" * 64)
    assert not looks_like_container(b"RIFF" + b"\x00" * 60)  # RIFF alone is not a WAV header


def test_validate_raw_audio_rejects_bad_input() -> None:
    wav = b"RIFF" + struct.pack("<I", 36) + b"WAVE" + b"\x00" * 40
    validate_raw_audio(b"\x00\x01" * 160, L16_8K)
    for bad in (b"", b"\x00\x01\x02", wav, b"ID3" + b"\x00" * 40):
        with pytest.raises(AudioFormatError):
            validate_raw_audio(bad, L16_8K)
    with pytest.raises(AudioFormatError):
        validate_raw_audio(b"\x00" * 100, MULAW_8K, max_bytes=99)


# --- bounded queue ---------------------------------------------------------------------------


def frame(n: int, fmt: AudioFormat = L16_8K) -> AudioFrame:
    return AudioFrame(
        data=bytes([n % 256]) * fmt.bytes_for_ms(20), format=fmt, track="inbound",
        timestamp_ms=n * 20, chunk=n, sequence=n, stream_id="s1",
    )


async def test_queue_is_fifo() -> None:
    q = BoundedFrameQueue(10)
    for n in range(3):
        assert q.put(frame(n)) is True
    assert [(await q.get()).chunk for _ in range(3)] == [0, 1, 2]
    assert len(q) == 0


async def test_queue_drops_oldest_when_full_and_counts() -> None:
    q = BoundedFrameQueue(3)
    results = [q.put(frame(n)) for n in range(10)]
    assert results == [True] * 3 + [False] * 7
    assert q.dropped == 7 and q.enqueued == 10 and q.high_water == 3 and len(q) == 3
    assert [(await q.get()).chunk for _ in range(3)] == [7, 8, 9]  # the newest survive


async def test_queue_memory_stays_bounded() -> None:
    q = BoundedFrameQueue(50)
    for n in range(100_000):
        q.put(frame(n % 256))
    assert len(q) == 50 and q.high_water == 50


async def test_consumer_waits_for_frames() -> None:
    q = BoundedFrameQueue(5)
    consumer = asyncio.create_task(q.get())
    await asyncio.sleep(0.01)
    assert not consumer.done()
    q.put(frame(1))
    assert (await asyncio.wait_for(consumer, 1)).chunk == 1


async def test_close_wakes_consumer_discards_audio_and_refuses_more() -> None:
    q = BoundedFrameQueue(5)
    waiting = asyncio.create_task(q.get())
    await asyncio.sleep(0.01)
    q.close()
    assert await asyncio.wait_for(waiting, 1) is None

    q2 = BoundedFrameQueue(5)
    q2.put(frame(1))
    q2.close()
    assert q2.stats()["buffered"] == 0
    assert await q2.get() is None
    assert q2.put(frame(2)) is False and len(q2) == 0
    q2.close()  # idempotent


def test_queue_needs_positive_size() -> None:
    with pytest.raises(ValueError):
        BoundedFrameQueue(0)


# --- synthetic audio -------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", [L16_8K, L16_16K, L16_24K, MULAW_8K])
def test_tone_length_matches_duration(fmt: AudioFormat) -> None:
    audio = tone(fmt, 100)
    assert len(audio) == fmt.bytes_for_ms(100)
    validate_raw_audio(audio, fmt)  # raw, aligned, no header


def test_tone_byte_orders_are_byte_swaps_of_each_other() -> None:
    little, big = tone(L16_8K, 20, byte_order="little"), tone(L16_8K, 20, byte_order="big")
    assert little != big
    swapped = b"".join(little[i : i + 2][::-1] for i in range(0, len(little), 2))
    assert swapped == big
    first = struct.unpack("<h", little[2:4])[0]
    assert first == struct.unpack(">h", big[2:4])[0] and first != 0


def test_tone_rejects_bad_parameters() -> None:
    for kwargs in ({"byte_order": "middle"}, {"amplitude": 0}, {"amplitude": 1.5}):
        with pytest.raises(AudioFormatError):
            tone(L16_8K, 20, **kwargs)
    with pytest.raises(AudioFormatError):
        tone(AudioFormat("audio/wav", 8000), 20)


def test_mulaw_encoder_known_values() -> None:
    assert linear_to_mulaw(0) == 0xFF
    assert linear_to_mulaw(32767) == 0x80
    assert linear_to_mulaw(-32768) == 0x00
    assert linear_to_mulaw(-1) == 0x7F


def test_silence_values() -> None:
    assert silence(MULAW_8K, 20) == b"\xff" * 160
    assert silence(L16_8K, 20) == b"\x00" * 320
