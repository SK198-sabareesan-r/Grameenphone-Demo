"""Development helper: stream known 16 kHz little-endian linear16 speech through Sarvam realtime STT
and measure what comes back. Used by scripts/validate_sarvam_realtime_stt.py,
scripts/analyze_vobiz_capture.py and the opt-in live tests. Not used by the application at runtime.

Audio is fed in 640-byte (20 ms) frames, exactly like Vobiz, and the client aggregates them into
`config.chunk_bytes` (3,200-byte) audio_input chunks. Nothing is resampled or byte-swapped here:
the caller must pass little-endian PCM (convert explicitly before calling if the source differs).
"""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import SecretStr

from app.services.sarvam.realtime_protocol import RealtimeSttConfig, SttEvent
from app.services.sarvam.realtime_stt import SarvamRealtimeSttClient

FRAME_BYTES = 640  # 20 ms at 16 kHz linear16, the Vobiz inbound frame size
TRAILING_SILENCE_S = 1.5  # longer than the default VAD silence (1,000 ms) so the last turn closes


@dataclass
class TranscriptRecord:
    at_ms: float  # since the first audio frame was sent
    text: str
    language: str | None
    utterance_idx: int | None
    language_confidence: float | None = None
    start_s: float | None = None
    end_s: float | None = None


@dataclass
class SpeechCheckResult:
    request_id: str | None
    audio_s: float
    partials: list[TranscriptRecord] = field(default_factory=list)
    finals: list[TranscriptRecord] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    vad_events: list[tuple[float, str]] = field(default_factory=list)
    first_partial_ms: float | None = None
    first_final_ms: float | None = None
    final_after_speech_end_ms: list[float] = field(default_factory=list)
    final_after_audio_end_ms: float | None = None  # last final vs end of the speech (before padding)
    chunks_sent: int = 0
    bytes_sent: int = 0
    chunk_bytes: int = 0
    billed_audio_s: float | None = None
    final_state: str = ""
    expected: str | None = None
    word_error_rate: float | None = None

    @property
    def transcript(self) -> str:
        return " ".join(r.text.strip() for r in self.finals if r.text.strip())


_PUNCT = re.compile(r"[\u0964\u0965]")  # Bengali/Devanagari danda


def normalize_words(text: str) -> list[str]:
    """Casefold, drop punctuation (incl. danda), keep letters/marks/digits of any script."""
    text = _PUNCT.sub(" ", unicodedata.normalize("NFC", text).casefold())
    cleaned = "".join(ch if unicodedata.category(ch)[0] in "LMN" or ch.isspace() else " " for ch in text)
    return cleaned.split()


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Word-level Levenshtein distance / reference length. 0.0 = exact match."""
    ref, hyp = normalize_words(reference), normalize_words(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, start=1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, start=1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1] / len(ref)


async def run_speech_check(
    pcm_le: bytes,
    config: RealtimeSttConfig,
    api_key: SecretStr | str,
    *,
    expected: str | None = None,
    pace: bool = True,
    trailing_silence_s: float = TRAILING_SILENCE_S,
    on_event: Callable[[SttEvent], None] | None = None,
    connect_fn: Callable[..., Awaitable[Any]] | None = None,
    pace_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    client_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> SpeechCheckResult:
    if config.encoding != "linear16":
        raise ValueError("speech check expects linear16 audio")
    if len(pcm_le) % 2:
        raise ValueError("PCM length is not a whole number of 16-bit samples")
    bytes_per_s = config.sample_rate * 2
    result = SpeechCheckResult(request_id=None, audio_s=len(pcm_le) / bytes_per_s, chunk_bytes=config.chunk_bytes,
                               expected=expected)
    t0: list[float] = []  # time of the first audio frame
    speech_end_at: list[float] = []

    def since_start() -> float:
        return (time.monotonic() - t0[0]) * 1000 if t0 else 0.0

    def handle(event: SttEvent) -> None:
        at = since_start()
        if event.type == "transcript.partial":
            result.partials.append(TranscriptRecord(at, event.text or "", event.language, event.utterance_idx))
            result.first_partial_ms = result.first_partial_ms if result.first_partial_ms is not None else at
        elif event.type == "transcript.final":
            result.finals.append(TranscriptRecord(at, event.text or "", event.language, event.utterance_idx,
                                                  event.language_confidence, event.start_s, event.end_s))
            result.first_final_ms = result.first_final_ms if result.first_final_ms is not None else at
            if speech_end_at:
                result.final_after_speech_end_ms.append(at - speech_end_at[-1])
        elif event.type in ("vad.speech_start", "vad.speech_end"):
            result.vad_events.append((at, event.type))
            if event.type == "vad.speech_end":
                speech_end_at.append(at)
        elif event.type == "error" and event.error is not None:
            result.errors.append({"at_ms": at, "code": event.error.code, "is_fatal": event.error.is_fatal,
                                  "status_code": event.error.status_code, "message": event.error.message})
        elif event.type == "session.end":
            result.billed_audio_s = event.audio_duration_s
        if on_event is not None:
            on_event(event)

    client = SarvamRealtimeSttClient(config, api_key, on_event=handle, connect_fn=connect_fn, sleep=client_sleep)
    stream = pcm_le + b"\x00\x00" * int(config.sample_rate * trailing_silence_s)
    frame_s = FRAME_BYTES / bytes_per_s
    try:
        begin = await client.connect()
        result.request_id = begin.request_id
        t0.append(time.monotonic())
        start = time.monotonic()
        for n, offset in enumerate(range(0, len(stream), FRAME_BYTES), start=1):
            await client.send_audio(stream[offset : offset + FRAME_BYTES])
            if pace:  # real-time pacing against an absolute schedule (no drift)
                delay = start + n * frame_s - time.monotonic()
                if delay > 0:
                    await pace_sleep(delay)
        await client.end()
    finally:
        await client.close()
    result.chunks_sent = client.stats["chunks_sent"]
    result.bytes_sent = client.stats["bytes_sent"]
    result.final_state = client.state
    if result.finals:
        result.final_after_audio_end_ms = result.finals[-1].at_ms - result.audio_s * 1000
    if expected is not None:
        result.word_error_rate = word_error_rate(expected, result.transcript)
    return result


def summarize(result: SpeechCheckResult) -> list[str]:
    """Human-readable report lines. Contains transcripts (this is a dev tool), never audio or keys."""
    def ms(value: float | None) -> str:
        return f"{value:.0f} ms" if value is not None else "n/a"

    full, rest = divmod(result.bytes_sent, result.chunk_bytes) if result.chunk_bytes else (0, 0)
    lines = [
        f"request_id          {result.request_id}",
        f"speech audio        {result.audio_s:.2f} s (+ trailing silence)",
        f"chunks sent         {result.chunks_sent} ({full} x {result.chunk_bytes} bytes"
        + (f" + 1 x {rest} bytes)" if rest else ")"),
        f"billed audio        {result.billed_audio_s} s",
        f"partials / finals   {len(result.partials)} / {len(result.finals)}",
        f"first partial       {ms(result.first_partial_ms)} after first audio",
        f"first final         {ms(result.first_final_ms)} after first audio",
        f"final after VAD end {', '.join(ms(v) for v in result.final_after_speech_end_ms) or 'n/a'}",
        f"last final vs end   {ms(result.final_after_audio_end_ms)} after the speech audio ended",
        f"errors              {len(result.errors)}" + (f" {result.errors}" if result.errors else ""),
        f"final state         {result.final_state}",
        f"TRANSCRIPT          {result.transcript or '(none)'}",
    ]
    if result.expected is not None:
        lines.append(f"EXPECTED            {result.expected}")
        lines.append(f"word error rate     {result.word_error_rate:.2%}")
    return lines
