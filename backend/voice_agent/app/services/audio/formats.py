"""Raw-audio format metadata and validation for Vobiz streams.

This module never converts audio and never reorders bytes. Byte order of L16 is deliberately not
assumed anywhere: frames are carried as opaque bytes together with their declared format.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

ENC_L16 = "audio/x-l16"
ENC_MULAW = "audio/x-mulaw"

# Vobiz documents 20 ms inbound frames; sizes up to this are accepted without complaint.
NOMINAL_FRAME_MS = 20
DEFAULT_MAX_FRAME_MS = 200


class AudioFormatError(ValueError):
    """Audio or format metadata is invalid. The message never contains audio data."""


@dataclass(frozen=True)
class AudioFormat:
    encoding: str
    sample_rate: int

    @property
    def bytes_per_sample(self) -> int:
        return 2 if self.encoding == ENC_L16 else 1

    def bytes_for_ms(self, ms: int) -> int:
        return self.sample_rate * ms // 1000 * self.bytes_per_sample

    def duration_ms(self, n_bytes: int) -> float:
        return n_bytes / self.bytes_per_sample / self.sample_rate * 1000

    @property
    def label(self) -> str:
        return f"{self.encoding};rate={self.sample_rate}"


# What Vobiz can send to us (<Stream contentType>, reported in start.mediaFormat).
INBOUND_FORMATS = frozenset(
    {AudioFormat(ENC_L16, 8000), AudioFormat(ENC_L16, 16000), AudioFormat(ENC_MULAW, 8000)}
)
# What we can send with playAudio.
OUTBOUND_FORMATS = INBOUND_FORMATS | {AudioFormat(ENC_L16, 24000)}


def _coerce_rate(sample_rate: object) -> int:
    if isinstance(sample_rate, bool):
        raise AudioFormatError("sample rate must be an integer")
    if isinstance(sample_rate, int):
        return sample_rate
    if isinstance(sample_rate, str) and sample_rate.isdigit():
        return int(sample_rate)
    raise AudioFormatError("sample rate must be an integer")


def inbound_format(encoding: object, sample_rate: object) -> AudioFormat:
    fmt = AudioFormat(str(encoding), _coerce_rate(sample_rate))
    if fmt not in INBOUND_FORMATS:
        raise AudioFormatError(f"unsupported inbound format {fmt.label}")
    return fmt


def outbound_format(encoding: object, sample_rate: object) -> AudioFormat:
    fmt = AudioFormat(str(encoding), _coerce_rate(sample_rate))
    if fmt not in OUTBOUND_FORMATS:
        raise AudioFormatError(f"unsupported outbound format {fmt.label}")
    return fmt


def looks_like_container(data: bytes) -> bool:
    """True if the bytes start with a WAV/MP3(ID3)/Ogg/FLAC header, which raw payloads must not have."""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return True
    return data[:3] == b"ID3" or data[:4] in (b"OggS", b"fLaC")


def validate_raw_audio(data: bytes, fmt: AudioFormat, *, max_bytes: int | None = None) -> None:
    """Validate raw mono audio for `fmt`: non-empty, whole samples, no container header."""
    if not data:
        raise AudioFormatError("audio is empty")
    if len(data) % fmt.bytes_per_sample:
        raise AudioFormatError(
            f"audio length {len(data)} is not a whole number of {fmt.bytes_per_sample}-byte samples"
        )
    if max_bytes is not None and len(data) > max_bytes:
        raise AudioFormatError(f"audio is larger than the {max_bytes}-byte limit")
    if looks_like_container(data):
        raise AudioFormatError("audio has a container header (WAV/MP3/Ogg/FLAC); send raw samples only")


def decode_media_payload(payload: object, fmt: AudioFormat, *, max_frame_ms: int = DEFAULT_MAX_FRAME_MS) -> bytes:
    """Base64-decode one inbound media payload and validate it against the announced format.

    Returns the raw bytes exactly as sent. No conversion, no byte swapping.
    """
    if not isinstance(payload, str) or not payload:
        raise AudioFormatError("media payload must be a non-empty base64 string")
    max_bytes = fmt.bytes_for_ms(max_frame_ms)
    if len(payload) > (max_bytes + 2) // 3 * 4 + 4:
        raise AudioFormatError("media payload is larger than the allowed frame size")
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AudioFormatError("media payload is not valid base64") from exc
    if not data:
        raise AudioFormatError("media payload decoded to zero bytes")
    if len(data) % fmt.bytes_per_sample:
        raise AudioFormatError("media payload is not a whole number of samples")
    if len(data) > max_bytes:
        raise AudioFormatError("media payload is larger than the allowed frame size")
    return data


def encode_payload(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
