"""Minimal WAV I/O for 16-bit mono PCM test material (stdlib only).

WAV (RIFF) PCM samples are little-endian by specification, so a valid 16-bit PCM WAV is a known
little-endian source. Nothing here resamples, remixes or converts bit depth: unsuitable files are
rejected with the reason.
"""

from __future__ import annotations

import os
import wave
from dataclasses import dataclass


class WavFormatError(ValueError):
    """The file is not usable as 16-bit mono PCM at the required rate."""


@dataclass(frozen=True)
class WavInfo:
    channels: int
    sample_width: int  # bytes per sample
    sample_rate: int
    frames: int
    compression: str

    @property
    def duration_s(self) -> float:
        return self.frames / self.sample_rate if self.sample_rate else 0.0


def inspect_wav(path: str) -> WavInfo:
    try:
        with wave.open(path, "rb") as w:
            return WavInfo(w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes(), w.getcomptype())
    except (wave.Error, EOFError) as exc:  # e.g. float WAV ("unknown format: 3"), MP3 renamed to .wav
        raise WavFormatError(f"not a readable PCM WAV file: {exc}") from None


def wav_problems(info: WavInfo, *, sample_rate: int = 16000) -> list[str]:
    problems = []
    if info.channels != 1:
        problems.append(f"{info.channels} channels (need mono)")
    if info.sample_width != 2:
        problems.append(f"{8 * info.sample_width}-bit samples (need 16-bit)")
    if info.sample_rate != sample_rate:
        problems.append(f"{info.sample_rate} Hz (need {sample_rate})")
    if info.compression != "NONE":
        problems.append(f"compression {info.compression} (need uncompressed PCM)")
    if info.frames == 0:
        problems.append("no audio frames")
    return problems


def read_pcm16_mono(path: str, *, sample_rate: int = 16000) -> tuple[bytes, WavInfo]:
    """Return the raw sample bytes (header stripped, little-endian) and the file's parameters."""
    info = inspect_wav(path)
    problems = wav_problems(info, sample_rate=sample_rate)
    if problems:
        raise WavFormatError(
            "WAV not usable without conversion: " + ", ".join(problems)
            + f". Convert it first: ffmpeg -i input -ac 1 -ar {sample_rate} -sample_fmt s16 output.wav"
        )
    with wave.open(path, "rb") as w:
        pcm = w.readframes(info.frames)
    if len(pcm) != info.frames * 2:
        raise WavFormatError("WAV is truncated: fewer sample bytes than the header declares")
    return pcm, info


def write_pcm16_mono(path: str, pcm_le: bytes, sample_rate: int) -> None:
    """Write little-endian 16-bit mono PCM as a WAV file (owner-only permissions where supported)."""
    if len(pcm_le) % 2:
        raise WavFormatError("PCM length is not a whole number of 16-bit samples")
    tmp = f"{path}.tmp"
    with wave.open(tmp, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm_le)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
