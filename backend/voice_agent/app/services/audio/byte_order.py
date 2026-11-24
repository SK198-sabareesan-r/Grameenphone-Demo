"""Objective comparison of the two possible byte orders of raw 16-bit PCM (L16).

The byte order is always stated explicitly ('<h' / '>h'); the host platform's order is never used.

Why the metrics separate the two readings: speech at 16 kHz is strongly correlated sample to
sample (lag-1 autocorrelation typically > 0.7). Reading the bytes in the wrong order moves the
fast-changing low byte into the high byte, which turns the signal into wideband noise: correlation
collapses towards 0, the zero-crossing rate rises towards ~0.5 and the level jumps.

These metrics only SUGGEST an answer. Verification also needs listening to both reconstructions
and comparing a transcript with the known spoken phrase. Silence, tones and very short captures
are reported as ambiguous.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Literal

Order = Literal["little", "big"]
MIN_SAMPLES = 8000  # 0.5 s at 16 kHz
FULL_SCALE = 32767
CLIP_LEVEL = 32000
SILENCE_LEVEL = 64  # |sample| below this counts as near-silent (about -54 dBFS)


def decode_l16(raw: bytes, order: Order) -> tuple[int, ...]:
    if len(raw) % 2:
        raise ValueError("L16 data must be a whole number of 2-byte samples")
    prefix = {"little": "<", "big": ">"}[order]
    return struct.unpack(f"{prefix}{len(raw) // 2}h", raw)


def swap_l16(raw: bytes) -> bytes:
    """Swap the two bytes of every sample (big-endian <-> little-endian). Explicit, not host-based."""
    if len(raw) % 2:
        raise ValueError("L16 data must be a whole number of 2-byte samples")
    swapped = bytearray(len(raw))
    swapped[0::2] = raw[1::2]
    swapped[1::2] = raw[0::2]
    return bytes(swapped)


def to_little_endian(raw: bytes, source_order: Order) -> bytes:
    """Return little-endian L16. A no-op for little-endian input; one explicit swap for big-endian."""
    return raw if source_order == "little" else swap_l16(raw)


@dataclass(frozen=True)
class L16Stats:
    order: Order
    samples: int
    rms: float
    peak: int
    mean_abs: float
    clip_fraction: float  # |x| >= 32000
    near_silent_fraction: float  # |x| < 64
    zero_crossing_rate: float  # sign changes per sample
    lag1_autocorr: float  # in [-1, 1]

    @property
    def rms_dbfs(self) -> float:
        return 20 * math.log10(self.rms / FULL_SCALE) if self.rms > 0 else float("-inf")


def analyze_l16(raw: bytes, order: Order) -> L16Stats:
    x = decode_l16(raw, order)
    n = len(x)
    if n == 0:
        return L16Stats(order, 0, 0.0, 0, 0.0, 0.0, 1.0, 0.0, 0.0)
    total_sq = sum(v * v for v in x)
    mean = sum(x) / n
    peak = max(abs(v) for v in x)
    crossings = sum(1 for a, b in zip(x, x[1:]) if (a < 0) != (b < 0))
    centred = [v - mean for v in x]
    var = sum(v * v for v in centred)
    lag1 = sum(a * b for a, b in zip(centred, centred[1:])) / var if var > 0 else 0.0
    return L16Stats(
        order=order,
        samples=n,
        rms=math.sqrt(total_sq / n),
        peak=peak,
        mean_abs=sum(abs(v) for v in x) / n,
        clip_fraction=sum(1 for v in x if abs(v) >= CLIP_LEVEL) / n,
        near_silent_fraction=sum(1 for v in x if abs(v) < SILENCE_LEVEL) / n,
        zero_crossing_rate=crossings / (n - 1) if n > 1 else 0.0,
        lag1_autocorr=lag1,
    )


@dataclass(frozen=True)
class ByteOrderVerdict:
    suggested: Order | None  # None = ambiguous
    strength: Literal["strong", "weak", "ambiguous"]
    little: L16Stats
    big: L16Stats
    reasons: list[str] = field(default_factory=list)


def compare_byte_orders(raw: bytes) -> ByteOrderVerdict:
    little, big = analyze_l16(raw, "little"), analyze_l16(raw, "big")
    reasons: list[str] = []

    if little.samples < MIN_SAMPLES:
        reasons.append(f"only {little.samples} samples; need at least {MIN_SAMPLES} (0.5 s at 16 kHz)")
        return ByteOrderVerdict(None, "ambiguous", little, big, reasons)
    if little.peak == 0:
        reasons.append("all samples are zero (digital silence); byte order cannot be determined")
        return ByteOrderVerdict(None, "ambiguous", little, big, reasons)
    if min(little.near_silent_fraction, big.near_silent_fraction) > 0.98:
        reasons.append("almost no signal in either reading; speak during the capture")
        return ByteOrderVerdict(None, "ambiguous", little, big, reasons)

    by_corr = sorted((little, big), key=lambda s: s.lag1_autocorr, reverse=True)
    best, other = by_corr
    margin = best.lag1_autocorr - other.lag1_autocorr
    reasons.append(
        f"lag-1 autocorrelation: little={little.lag1_autocorr:.3f}, big={big.lag1_autocorr:.3f}"
    )
    reasons.append(
        f"zero-crossing rate: little={little.zero_crossing_rate:.3f}, big={big.zero_crossing_rate:.3f}"
    )
    reasons.append(f"RMS dBFS: little={little.rms_dbfs:.1f}, big={big.rms_dbfs:.1f}")
    reasons.append(f"clipped fraction: little={little.clip_fraction:.4f}, big={big.clip_fraction:.4f}")

    if best.lag1_autocorr >= 0.6 and other.lag1_autocorr <= 0.3 and margin >= 0.4:
        corroborated = best.zero_crossing_rate < other.zero_crossing_rate and best.clip_fraction <= other.clip_fraction
        strength = "strong" if corroborated else "weak"
        if not corroborated:
            reasons.append("zero-crossing/clipping metrics do not corroborate the correlation result")
        return ByteOrderVerdict(best.order, strength, little, big, reasons)

    reasons.append("neither reading is clearly speech-like (needs one >= 0.6 and the other <= 0.3)")
    return ByteOrderVerdict(None, "ambiguous", little, big, reasons)
