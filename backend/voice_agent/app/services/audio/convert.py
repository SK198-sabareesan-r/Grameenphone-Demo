"""Explicit conversion of synthesized speech (WAV) into raw audio Vobiz can play.

Vobiz `playAudio` takes raw mono audio (no container header) in one of the OUTBOUND_FORMATS. For L16
the byte order is little-endian (verified audible on this project's streams; see media_config.py).
WAV PCM is little-endian by specification, so a 16-bit PCM WAV's sample bytes are already in the order
Vobiz needs and are passed through unchanged. What is converted here:

* the WAV container header is removed;
* multi-channel audio is averaged to mono;
* a sample rate Vobiz cannot play (anything but 8, 16 or 24 kHz) is resampled (linear interpolation) to
  16 kHz. Telephone speech does not need better, and no new dependency is required.

Anything else (non-PCM, non-16-bit, truncated, empty) is rejected with `AudioConversionError`.
"""

from __future__ import annotations

import struct
import sys
from array import array
from dataclasses import dataclass, field

from app.services.audio.formats import ENC_L16, AudioFormat, AudioFormatError, outbound_format

PLAYABLE_L16_RATES = (8000, 16000, 24000)
FALLBACK_RATE = 16000
MIN_DURATION_MS = 20
WAVE_FORMAT_PCM = 1
WAVE_FORMAT_EXTENSIBLE = 0xFFFE


class AudioConversionError(AudioFormatError):
    """The synthesized audio cannot be converted. The message never contains audio data."""


@dataclass(frozen=True)
class WavPcm:
    pcm: bytes = field(repr=False)  # 16-bit little-endian mono
    sample_rate: int
    source_channels: int


@dataclass(frozen=True)
class PlaybackAudio:
    pcm: bytes = field(repr=False)  # raw little-endian L16 mono, no header
    format: AudioFormat
    source_sample_rate: int
    resampled: bool

    @property
    def duration_ms(self) -> float:
        return self.format.duration_ms(len(self.pcm))


def _le_to_array(data: bytes) -> array:
    samples = array("h")
    samples.frombytes(data)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def _array_to_le(samples: array) -> bytes:
    if sys.byteorder == "big":
        samples = array("h", samples)
        samples.byteswap()
    return samples.tobytes()


def decode_wav(data: bytes) -> WavPcm:
    """Parse a 16-bit PCM WAV leniently (streamed headers with unknown sizes are accepted)."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioConversionError("audio is not a WAV file")
    pos = 12
    fmt: tuple[int, int, int, int] | None = None  # (tag, channels, rate, bits)
    pcm: bytes | None = None
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        body = pos + 8
        if chunk_id == b"fmt ":
            if size < 16 or body + 16 > len(data):
                raise AudioConversionError("WAV fmt chunk is truncated")
            tag, channels, rate, _byte_rate, _align, bits = struct.unpack_from("<HHIIHH", data, body)
            if tag == WAVE_FORMAT_EXTENSIBLE and size >= 26 and body + 26 <= len(data):
                tag = struct.unpack_from("<H", data, body + 24)[0]  # sub-format GUID starts with the real tag
            fmt = (tag, channels, rate, bits)
        elif chunk_id == b"data":
            end = body + size
            if size == 0 or size == 0xFFFFFFFF or end > len(data):  # streamed/unknown length: take the rest
                end = len(data)
            pcm = data[body:end]
            break
        pos = body + size + (size & 1)  # chunks are word-aligned
    if fmt is None or pcm is None:
        raise AudioConversionError("WAV has no fmt or data chunk")
    tag, channels, rate, bits = fmt
    if tag != WAVE_FORMAT_PCM or bits != 16:
        raise AudioConversionError(f"WAV is not 16-bit PCM (format {tag}, {bits}-bit)")
    if channels < 1 or rate < 8000 or rate > 192_000:
        raise AudioConversionError("WAV has an invalid channel count or sample rate")
    frame_bytes = 2 * channels
    pcm = pcm[: len(pcm) // frame_bytes * frame_bytes]
    if not pcm:
        raise AudioConversionError("WAV contains no audio samples")
    if channels > 1:
        samples = _le_to_array(pcm)
        mono = array("h", (sum(samples[i : i + channels]) // channels for i in range(0, len(samples), channels)))
        pcm = _array_to_le(mono)
    return WavPcm(pcm=pcm, sample_rate=rate, source_channels=channels)


def resample_pcm16(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
    """Linear-interpolation resampler for 16-bit little-endian mono PCM."""
    if from_rate == to_rate:
        return pcm
    src = _le_to_array(pcm)
    n_src = len(src)
    n_out = max(1, n_src * to_rate // from_rate)
    ratio = from_rate / to_rate
    out = array("h", bytes(2 * n_out))
    last = n_src - 1
    for i in range(n_out):
        pos = i * ratio
        j = int(pos)
        if j >= last:
            out[i] = src[last]
        else:
            frac = pos - j
            out[i] = int(src[j] + (src[j + 1] - src[j]) * frac)
    return _array_to_le(out)


def wav_to_playback(wav_bytes: bytes) -> PlaybackAudio:
    """WAV bytes -> raw L16 little-endian mono audio plus the exact format to declare in playAudio."""
    wav = decode_wav(wav_bytes)
    pcm, rate, resampled = wav.pcm, wav.sample_rate, False
    if rate not in PLAYABLE_L16_RATES:
        pcm, rate, resampled = resample_pcm16(pcm, rate, FALLBACK_RATE), FALLBACK_RATE, True
    fmt = outbound_format(ENC_L16, rate)
    audio = PlaybackAudio(pcm=pcm, format=fmt, source_sample_rate=wav.sample_rate, resampled=resampled)
    if audio.duration_ms < MIN_DURATION_MS:
        raise AudioConversionError("synthesized audio is shorter than one frame")
    return audio
