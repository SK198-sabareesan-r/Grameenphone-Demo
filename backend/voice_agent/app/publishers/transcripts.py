"""Internal transcript events for live calls, and the publisher interface that receives them.

This is an internal contract only: no database, no public API, no frontend (later milestones plug
real publishers in here, e.g. an HTTP publisher to the Copilot backend).

Events are published in the order Sarvam produced them, one stream at a time. A publisher must be
quick: it is awaited on the STT receive path. Publisher failures are caught and counted by the
caller and never affect the call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Event types (the Sarvam realtime names are kept as-is).
SESSION_BEGIN = "session.begin"
VAD_SPEECH_START = "vad.speech_start"
VAD_SPEECH_END = "vad.speech_end"
TRANSCRIPT_PARTIAL = "transcript.partial"
TRANSCRIPT_FINAL = "transcript.final"
SESSION_END = "session.end"
ERROR = "error"
PUBLISHED_TYPES = frozenset(
    {SESSION_BEGIN, VAD_SPEECH_START, VAD_SPEECH_END, TRANSCRIPT_PARTIAL, TRANSCRIPT_FINAL, SESSION_END, ERROR}
)


@dataclass(frozen=True)
class TranscriptEvent:
    """One STT event for one call. `text` is customer speech: it is excluded from repr and logs."""

    call_id: str  # Vobiz CallUUID / start.callId
    stream_id: str  # Vobiz streamId (changes on reconnect)
    sequence: int  # per-stream, monotonically increasing: preserves event order
    type: str  # one of PUBLISHED_TYPES
    received_at: str  # ISO-8601 UTC, when we received it from Sarvam
    source: str = "sarvam"
    speaker: str = "customer"  # inbound track of the Vobiz call
    is_final: bool = False  # True only for transcript.final
    text: str | None = field(default=None, repr=False)
    language: str | None = None  # only when Sarvam language_code=auto
    language_confidence: float | None = None
    utterance_idx: int | None = None
    start_s: float | None = None  # final only, with return_timestamps
    end_s: float | None = None
    stt_request_id: str | None = None  # Sarvam session.begin request_id
    error_code: str | None = None
    error_is_fatal: bool | None = None
    # M5: neutral logical track label (e.g. "track_inbound" / "track_outbound"), None for legacy events.
    # Separate from `stream_id` (always the physical Vobiz stream) and from `speaker` (never inferred).
    track: str | None = None

    @property
    def is_partial(self) -> bool:
        return self.type == TRANSCRIPT_PARTIAL

    def log_fields(self) -> dict[str, object]:
        """Metadata that is safe to log: never the text."""
        return {
            "call_id": self.call_id, "stream_id": self.stream_id, "track": self.track, "sequence": self.sequence,
            "event_type": self.type, "is_final": self.is_final, "utterance_idx": self.utterance_idx,
            "language": self.language, "text_length": len(self.text) if self.text is not None else None,
            "stt_request_id": self.stt_request_id, "error_code": self.error_code,
        }


@runtime_checkable
class TranscriptPublisher(Protocol):
    async def publish(self, event: TranscriptEvent) -> None: ...


class LoggingTranscriptPublisher:
    """Default publisher: records that events happened (metadata only, never transcript text).

    Finals at INFO, everything else at DEBUG so partials (several per second) do not flood logs.
    """

    async def publish(self, event: TranscriptEvent) -> None:
        level = logging.INFO if event.type in (TRANSCRIPT_FINAL, ERROR) else logging.DEBUG
        if logger.isEnabledFor(level):
            logger.log(level, "vobiz_transcript_event", extra=event.log_fields())
