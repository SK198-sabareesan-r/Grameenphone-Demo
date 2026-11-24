"""Synthetic test audio (tones, silence). For testing the transport only; not TTS.

`byte_order` exists because the byte order Vobiz expects for L16 is not documented. Generating the
same tone both ways lets a real-call test tell which one sounds right. Nothing else in the
application assumes a byte order.
"""

from __future__ import annotations

import math
import struct

from app.services.audio.formats import ENC_L16, ENC_MULAW, AudioFormat, AudioFormatError


def linear_to_mulaw(sample: int) -> int:
    """Standard G.711 mu-law encode of one signed 16-bit sample."""
    sign = 0x80 if sample < 0 else 0
    magnitude = min(abs(sample), 32635) + 0x84
    exponent, mask = 7, 0x4000
    while exponent > 0 and not magnitude & mask:
        exponent -= 1
        mask >>= 1
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def tone(
    fmt: AudioFormat,
    duration_ms: int,
    frequency_hz: float = 440.0,
    *,
    amplitude: float = 0.3,
    byte_order: str = "little",
) -> bytes:
    if byte_order not in ("little", "big"):
        raise AudioFormatError("byte_order must be 'little' or 'big'")
    if not 0 < amplitude <= 1:
        raise AudioFormatError("amplitude must be in (0, 1]")
    count = fmt.sample_rate * duration_ms // 1000
    samples = [
        int(32767 * amplitude * math.sin(2 * math.pi * frequency_hz * n / fmt.sample_rate))
        for n in range(count)
    ]
    if fmt.encoding == ENC_L16:
        return struct.pack(f"{'<' if byte_order == 'little' else '>'}{count}h", *samples)
    if fmt.encoding == ENC_MULAW:
        return bytes(linear_to_mulaw(s) for s in samples)
    raise AudioFormatError(f"cannot synthesize {fmt.label}")


def silence(fmt: AudioFormat, duration_ms: int) -> bytes:
    count = fmt.sample_rate * duration_ms // 1000
    return b"\xff" * count if fmt.encoding == ENC_MULAW else b"\x00" * (count * 2)
