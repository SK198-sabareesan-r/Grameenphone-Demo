"""Sarvam Realtime Streaming STT protocol: configuration, audio chunking, messages, event parsing.

Pure code (no I/O). Follows the CURRENT realtime API (`/speech-to-text-realtime/ws`, saaras:v4),
not the legacy `/speech-to-text/ws` protocol:

  connect   wss://api.sarvam.ai/speech-to-text-realtime/ws?<params>   header api-subscription-key
  client -> {"event":"audio_input","audio":"<base64 raw PCM>"} | {"event":"end"} | {"event":"ping"}
  server -> session.begin, vad.speech_start/end, transcript.partial/final, config.updated, pong,
            session.end, error{code,is_fatal,message,status_code?}

Audio is sent exactly as given: no resampling, no byte swapping, no container header. The caller
is responsible for supplying audio that really is `encoding` at `sample_rate`, mono.
"""

from __future__ import annotations

import base64
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit

from app.core.config import Settings
from app.services.sarvam import constants as c
from app.services.sarvam.errors import SarvamConfigError, SarvamError

BYTES_PER_SAMPLE = {"linear16": 2, "linear32": 4, "mulaw": 1, "alaw": 1}
MIN_CHUNK_MS = 20
MAX_CHUNK_MS = 250  # well below the server's 500 ms per-message cap


class SarvamProtocolError(SarvamError):
    """A server message could not be parsed. `reason` is a short code that is safe to log."""

    code = "sarvam_protocol_error"

    def __init__(self, reason: str) -> None:
        super().__init__(f"Malformed Sarvam realtime message: {reason}")
        self.reason = reason


# --- configuration ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RealtimeSttConfig:
    url: str = "wss://api.sarvam.ai/speech-to-text-realtime/ws"
    model: str = "saaras:v4"
    language_code: str = "auto"
    mode: str = "transcribe"
    stream_type: str = "balanced"
    encoding: str = "linear16"
    sample_rate: int = 16000
    endpointing: str = "vad"
    return_timestamps: bool = True
    chunk_ms: int = 100
    connect_timeout_s: float = 10.0
    keepalive_interval_s: float = 20.0
    max_reconnect_attempts: int = 3
    backoff_initial_s: float = 0.5
    backoff_max_s: float = 8.0
    end_timeout_s: float = 10.0  # wait for session.end after sending {"event":"end"}
    max_message_bytes: int = 1 << 20
    silence_duration_ms: int = 0  # 0 = not sent (Sarvam default 1000 ms); endpointing=vad only

    @classmethod
    def from_settings(cls, s: Settings) -> "RealtimeSttConfig":
        return cls(
            url=s.sarvam_realtime_ws_url,
            model=s.sarvam_model,
            language_code=s.sarvam_language,
            mode=s.sarvam_stt_mode,
            stream_type=s.sarvam_stt_stream_type,
            encoding=s.sarvam_stt_encoding,
            sample_rate=s.sarvam_stt_sample_rate,
            endpointing=s.sarvam_stt_endpointing,
            return_timestamps=s.sarvam_stt_return_timestamps,
            chunk_ms=s.sarvam_stt_chunk_ms,
            connect_timeout_s=s.sarvam_stt_connect_timeout_s,
            keepalive_interval_s=s.sarvam_stt_keepalive_interval_s,
            max_reconnect_attempts=s.sarvam_stt_max_reconnect_attempts,
            silence_duration_ms=s.sarvam_stt_silence_duration_ms,
        ).validated()

    def validated(self) -> "RealtimeSttConfig":
        """Raise SarvamConfigError for values the docs say the server rejects. Nothing is replaced."""
        problems: list[str] = []
        parts = urlsplit(self.url)
        if parts.scheme != "wss" or not parts.netloc or parts.query:
            problems.append("SARVAM_REALTIME_WS_URL must be a wss:// URL without a query string")
        if not self.model:
            problems.append("SARVAM_STT_MODEL is empty")
        if self.language_code not in c.REALTIME_STT_LANGUAGES:
            problems.append(f"language_code '{self.language_code}' is not a documented realtime value")
        checks = (
            ("mode", self.mode, c.REALTIME_STT_MODES),
            ("stream_type", self.stream_type, c.STT_STREAM_TYPES),
            ("encoding", self.encoding, c.REALTIME_STT_ENCODINGS),
            ("sample_rate", self.sample_rate, c.REALTIME_STT_SAMPLE_RATES),
            ("endpointing", self.endpointing, c.REALTIME_STT_ENDPOINTING),
        )
        for name, value, allowed in checks:
            if value not in allowed:
                problems.append(f"{name} '{value}' must be one of {sorted(allowed)}")
        if not MIN_CHUNK_MS <= self.chunk_ms <= MAX_CHUNK_MS:
            problems.append(f"chunk_ms must be {MIN_CHUNK_MS}-{MAX_CHUNK_MS}")
        if self.connect_timeout_s <= 0 or self.keepalive_interval_s <= 0 or self.end_timeout_s <= 0:
            problems.append("timeouts and keepalive interval must be positive")
        if not 0 <= self.max_reconnect_attempts <= 10:
            problems.append("max_reconnect_attempts must be 0-10")
        if self.silence_duration_ms and not 200 <= self.silence_duration_ms <= 5000:
            problems.append("SARVAM_STT_SILENCE_DURATION_MS must be 0 (default) or 200-5000")
        if problems:
            raise SarvamConfigError("Invalid Sarvam realtime STT config: " + "; ".join(problems))
        return self

    @property
    def bytes_per_sample(self) -> int:
        return BYTES_PER_SAMPLE[self.encoding]

    @property
    def chunk_bytes(self) -> int:
        """100 ms at 16 kHz linear16 mono = 1,600 samples x 2 bytes = 3,200 bytes."""
        return self.sample_rate * self.chunk_ms // 1000 * self.bytes_per_sample

    def query_params(self) -> dict[str, str]:
        params = self._base_params()
        if self.silence_duration_ms and self.endpointing == "vad":
            params["silence_duration_ms"] = str(self.silence_duration_ms)
        return params

    def _base_params(self) -> dict[str, str]:
        return {
            "model": self.model,
            "language_code": self.language_code,
            "mode": self.mode,
            "stream_type": self.stream_type,
            "encoding": self.encoding,
            "sample_rate": str(self.sample_rate),
            "endpointing": self.endpointing,
            "return_timestamps": "true" if self.return_timestamps else "false",
        }

    def build_url(self) -> str:
        """Connection URL. The API key is NEVER placed here; it goes in a header."""
        return f"{self.url}?{urlencode(self.query_params())}"

    def backoff_delay(self, attempt: int) -> float:
        """Delay before reconnect attempt `attempt` (1-based): 0.5, 1, 2, 4, 8, 8 ... seconds."""
        return min(self.backoff_max_s, self.backoff_initial_s * (2 ** max(0, attempt - 1)))


# --- audio chunking --------------------------------------------------------------------------


class PcmChunker:
    """Aggregates raw PCM into fixed-size chunks (e.g. five 20 ms Vobiz frames -> one 100 ms chunk).

    Order is preserved and bytes are never altered. Input must be whole samples; misaligned input
    raises instead of being silently fixed. Memory is bounded by one chunk.
    """

    def __init__(self, chunk_bytes: int, sample_width: int) -> None:
        if chunk_bytes <= 0 or sample_width <= 0 or chunk_bytes % sample_width:
            raise ValueError("chunk_bytes must be a positive multiple of sample_width")
        self.chunk_bytes = chunk_bytes
        self.sample_width = sample_width
        self._buffer = bytearray()
        self.bytes_in = 0
        self.chunks_out = 0

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    def push(self, data: bytes) -> list[bytes]:
        if len(data) % self.sample_width:
            raise ValueError(f"audio length {len(data)} is not a whole number of {self.sample_width}-byte samples")
        self.bytes_in += len(data)
        self._buffer += data
        out: list[bytes] = []
        while len(self._buffer) >= self.chunk_bytes:
            out.append(bytes(self._buffer[: self.chunk_bytes]))
            del self._buffer[: self.chunk_bytes]
        self.chunks_out += len(out)
        return out

    def flush(self) -> bytes | None:
        """Return the remaining partial chunk (if any) and empty the buffer."""
        if not self._buffer:
            return None
        rest = bytes(self._buffer)
        self._buffer.clear()
        self.chunks_out += 1
        return rest


# --- client -> server messages ---------------------------------------------------------------


def _dump(message: dict[str, Any]) -> str:
    return json.dumps(message, separators=(",", ":"))


def build_audio_input(chunk: bytes) -> str:
    return _dump({"event": "audio_input", "audio": base64.b64encode(chunk).decode("ascii")})


def build_end() -> str:
    return _dump({"event": "end"})


def build_ping() -> str:
    return _dump({"event": "ping"})


# --- server -> client events -----------------------------------------------------------------

SESSION_BEGIN = "session.begin"
VAD_SPEECH_START = "vad.speech_start"
VAD_SPEECH_END = "vad.speech_end"
TRANSCRIPT_PARTIAL = "transcript.partial"
TRANSCRIPT_FINAL = "transcript.final"
CONFIG_UPDATED = "config.updated"
PONG = "pong"
SESSION_END = "session.end"
ERROR = "error"
UNKNOWN = "unknown"
EVENT_TYPES = frozenset(
    {SESSION_BEGIN, VAD_SPEECH_START, VAD_SPEECH_END, TRANSCRIPT_PARTIAL, TRANSCRIPT_FINAL,
     CONFIG_UPDATED, PONG, SESSION_END, ERROR}
)  # fmt: skip


@dataclass(frozen=True)
class SttError:
    code: str
    message: str
    is_fatal: bool
    status_code: int | None = None


@dataclass(frozen=True)
class SttEvent:
    """Normalized realtime STT event (internal contract; not the raw Sarvam payload).

    `is_final` is True only for transcript.final. A partial is never a finished utterance.
    Fields the server did not send are None; nothing is invented.
    """

    type: str
    received_at: str  # ISO-8601 UTC, our clock
    received_monotonic: float
    request_id: str | None = None  # from session.begin (per connection)
    text: str | None = field(default=None, repr=False)  # customer speech: kept out of reprs/logs
    is_final: bool = False
    utterance_idx: int | None = None
    language: str | None = None  # only when language_code=auto
    language_confidence: float | None = None  # transcript.final only, auto mode only
    start_s: float | None = None  # transcript.final only, return_timestamps=true
    end_s: float | None = None
    vad_confidence: float | None = None
    error: SttError | None = None
    session_config: dict[str, Any] | None = None  # session.begin echo of the resolved config
    applied: tuple[str, ...] | None = None  # config.updated
    audio_duration_s: float | None = None  # session.end (billed audio)
    total_duration_s: float | None = None
    total_utterances: int | None = None
    raw_event: str | None = None  # original name when type == "unknown"

    @property
    def is_partial(self) -> bool:
        return self.type == TRANSCRIPT_PARTIAL

    @property
    def is_transcript(self) -> bool:
        return self.type in (TRANSCRIPT_PARTIAL, TRANSCRIPT_FINAL)


def _opt_float(value: Any) -> float | None:
    """Sarvam sends several numbers as strings (e.g. "0.93", "1.24")."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _opt_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _opt_str(value: Any, limit: int = 256) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def parse_server_message(text: str | bytes, *, request_id: str | None = None) -> SttEvent:
    """Parse one server frame. Raises SarvamProtocolError (never anything else) if malformed."""
    if isinstance(text, bytes):
        raise SarvamProtocolError("binary_frame")
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise SarvamProtocolError("invalid_json") from None
    if not isinstance(data, dict):
        raise SarvamProtocolError("not_an_object")
    name = data.get("event")
    if not isinstance(name, str) or not name:
        raise SarvamProtocolError("missing_event")

    now_iso = datetime.now(timezone.utc).isoformat()
    base: dict[str, Any] = {"received_at": now_iso, "received_monotonic": time.monotonic(), "request_id": request_id}

    if name == SESSION_BEGIN:
        rid = data.get("request_id")
        if not isinstance(rid, str) or not rid:
            raise SarvamProtocolError("session_begin_without_request_id")
        config = data.get("config")
        base["request_id"] = rid
        return SttEvent(type=name, session_config=config if isinstance(config, dict) else None, **base)

    if name in (TRANSCRIPT_PARTIAL, TRANSCRIPT_FINAL):
        transcript = data.get("text")
        if not isinstance(transcript, str):
            raise SarvamProtocolError(f"{name}_without_text")
        final = name == TRANSCRIPT_FINAL
        return SttEvent(
            type=name,
            text=transcript,
            is_final=final,
            utterance_idx=_opt_int(data.get("utterance_idx")),
            language=_opt_str(data.get("language"), 16),
            language_confidence=_opt_float(data.get("language_confidence")) if final else None,
            start_s=_opt_float(data.get("start_s")) if final else None,
            end_s=_opt_float(data.get("end_s")) if final else None,
            **base,
        )

    if name in (VAD_SPEECH_START, VAD_SPEECH_END):
        return SttEvent(
            type=name,
            utterance_idx=_opt_int(data.get("utterance_idx")),
            vad_confidence=_opt_float(data.get("confidence")),
            **base,
        )

    if name == ERROR:
        code = data.get("code")
        if not isinstance(code, str) or not code:
            raise SarvamProtocolError("error_without_code")
        return SttEvent(
            type=name,
            error=SttError(
                code=code[:64],
                message=_opt_str(data.get("message"), 300) or "",
                is_fatal=data.get("is_fatal") is True,
                status_code=_opt_int(data.get("status_code")),
            ),
            **base,
        )

    if name == CONFIG_UPDATED:
        applied = data.get("applied")
        keys = tuple(k for k in applied if isinstance(k, str)) if isinstance(applied, list) else ()
        return SttEvent(type=name, applied=keys, **base)

    if name == PONG:
        return SttEvent(type=name, **base)

    if name == SESSION_END:
        rid = data.get("request_id")
        if isinstance(rid, str) and rid:
            base["request_id"] = rid
        return SttEvent(
            type=name,
            audio_duration_s=_opt_float(data.get("audio_duration_s")),
            total_duration_s=_opt_float(data.get("total_duration_s")),
            total_utterances=_opt_int(data.get("total_utterances")),
            **base,
        )

    return SttEvent(type=UNKNOWN, raw_event=name[:64], **base)
