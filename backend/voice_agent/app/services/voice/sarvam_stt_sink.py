"""Vobiz inbound audio -> Sarvam realtime STT, one isolated Sarvam session per Vobiz stream (M4.3).

    Vobiz WS --640 B frames--> per-stream queue --> InboundAudioConsumer --> SarvamSttSink.handle_frame
        --> SarvamRealtimeSttClient.send_audio (PcmChunker: 5 frames -> one 3,200 B / 100 ms chunk)
        --> Sarvam --> events --> TranscriptEvent (call_id, stream_id, sequence) --> TranscriptPublisher

* One sink = one client = one Sarvam WebSocket, created by the factory when the stream's consumer
  starts. Nothing is shared between streams or calls.
* Byte order: Vobiz inbound L16 is little-endian (VOBIZ_INBOUND_L16_BYTE_ORDER in
  app/integrations/vobiz/media_config.py, verified in M4.2) and Sarvam linear16 is little-endian,
  so frame bytes are forwarded UNCHANGED. This module is the only place inbound byte order is
  applied (via to_little_endian, a no-op for "little"). Nothing upstream transforms the bytes
  (the WebSocket handler only base64-decodes), so audio can never be swapped twice.
* No resampling: the stream format must equal the configured Sarvam encoding/sample rate, else the
  factory refuses the stream (consumer state "failed") rather than converting.
* Shutdown (close): flush the partial last chunk, send {"event":"end"}, wait for session.end at most
  `finalize_timeout_s`, then always close the socket. The consumer drains buffered frames first.
* Sarvam auto-reconnect stays OFF: a mid-call Sarvam drop makes handle_frame raise (counted by the
  consumer, which gives up after its consecutive-error limit) instead of silently losing an utterance.
* Logs and metrics: counters, states, ids and timings only. Never transcript text, audio or keys.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr

from app.core.config import Settings
from app.integrations.vobiz.media_config import inbound_byte_order
from app.publishers.transcripts import (
    ERROR,
    PUBLISHED_TYPES,
    SESSION_END,
    TRANSCRIPT_FINAL,
    TRANSCRIPT_PARTIAL,
    VAD_SPEECH_END,
    VAD_SPEECH_START,
    LoggingTranscriptPublisher,
    TranscriptEvent,
    TranscriptPublisher,
)
from app.services.audio.audio_buffer import AudioFrame
from app.services.audio.byte_order import to_little_endian
from app.services.audio.formats import ENC_L16, ENC_MULAW, AudioFormat
from app.services.sarvam.errors import SarvamConfigError, SarvamConnectionError, SarvamError
from app.services.sarvam.realtime_protocol import RealtimeSttConfig, SttEvent
from app.services.sarvam.realtime_stt import OPEN, SarvamRealtimeSttClient

if TYPE_CHECKING:
    from app.services.voice.call_manager import StreamSession

logger = logging.getLogger(__name__)

# Vobiz stream encodings and the Sarvam encodings that carry the same bytes.
_SARVAM_ENCODING_FOR = {ENC_L16: "linear16", ENC_MULAW: "mulaw"}
CLOSE_MARGIN_S = 1.0  # time on top of the finalize budget for flush + socket close

ClientFactory = Callable[..., SarvamRealtimeSttClient]


class SarvamSttUnavailable(SarvamConnectionError):
    """The call's Sarvam session is not open (failed or closed); this frame was not sent."""


def check_stream_format(stream_format: AudioFormat, config: RealtimeSttConfig) -> None:
    """Refuse (do not convert) a stream whose audio does not match the Sarvam connection params."""
    expected = _SARVAM_ENCODING_FOR.get(stream_format.encoding)
    if expected != config.encoding or stream_format.sample_rate != config.sample_rate:
        raise SarvamConfigError(
            f"Vobiz stream format {stream_format.label} does not match Sarvam "
            f"encoding={config.encoding} sample_rate={config.sample_rate}; refusing to resample or convert."
        )


class SarvamSttSink:
    """AudioFrameSink for one Vobiz stream. Created connected by SarvamSttSinkFactory."""

    def __init__(
        self,
        session: "StreamSession",
        config: RealtimeSttConfig,
        api_key: SecretStr,
        publisher: TranscriptPublisher,
        *,
        finalize_timeout_s: float,
        client_factory: ClientFactory = SarvamRealtimeSttClient,
        clock: Callable[[], float] = time.monotonic,
        track: str | None = None,
    ) -> None:
        # `track` (M5) is the neutral logical track label for a per-track sink (None = legacy stream
        # sink). `session` may be the physical StreamSession or a per-track session view with the same
        # call_id / stream_id / audio_format; stream_id is always the PHYSICAL Vobiz stream id.
        check_stream_format(session.audio_format, config)
        self._track = track
        self._session = session
        self._publisher = publisher
        self._clock = clock
        self._finalize_timeout_s = finalize_timeout_s
        self._byte_order = inbound_byte_order(session.audio_format)  # "little" for L16, None for mu-law
        # The client's own end() wait is bounded by our finalize budget.
        self._client = client_factory(
            replace(config, end_timeout_s=finalize_timeout_s).validated(), api_key, on_event=self._on_event
        )
        self.close_timeout_s = finalize_timeout_s + CLOSE_MARGIN_S  # read by the consumer
        self.state = "connecting"
        self._sequence = 0
        self._closed = False
        self._send_failed = False
        self._lost_logged = False
        self._first_audio_at: float | None = None
        self._last_speech_end_at: float | None = None
        self.m: dict[str, Any] = {
            "frames_in": 0, "bytes_in": 0, "partials": 0, "finals": 0, "vad_speech_starts": 0,
            "vad_speech_ends": 0, "stt_errors": 0, "stt_fatal_errors": 0, "send_errors": 0,
            "publisher_errors": 0, "first_partial_ms": None, "first_final_ms": None,
            "last_final_after_speech_end_ms": None, "finalize_ms": None, "finalize_timed_out": None,
            "finalize_timeouts": 0, "session_end_received": False, "billed_audio_s": None,
            "close_reason": None,
        }

    @property
    def _ids(self) -> dict[str, Any]:
        ids: dict[str, Any] = {"call_id": self._session.call_id, "stream_id": self._session.stream_id,
                               "stt_request_id": self._client.request_id}
        if self._track is not None:
            ids["track"] = self._track
        return ids

    # --- lifecycle ----------------------------------------------------------------------------

    async def connect(self) -> None:
        try:
            await self._client.connect()  # bounded retries for transient failures (client policy)
        except BaseException:
            self.state = "failed"
            await self._client.close()
            raise
        self.state = "open"
        logger.info("vobiz_stt_session_started", extra={**self._ids, "audio_format": self._session.audio_format.label})

    async def handle_frame(self, frame: AudioFrame) -> None:
        if frame.format != self._session.audio_format:
            raise SarvamConfigError("frame format differs from the stream format")
        if self._closed or self._send_failed or self._client.state != OPEN:
            self.m["send_errors"] += 1
            self._log_lost_once()
            raise SarvamSttUnavailable(f"Sarvam session is {self._client.state}; frame not sent")
        # The ONLY inbound byte-order step: a no-op for verified little-endian L16 (and for mu-law).
        pcm = to_little_endian(frame.data, self._byte_order) if self._byte_order else frame.data
        if self._first_audio_at is None:
            self._first_audio_at = self._clock()
        try:
            await self._client.send_audio(pcm)  # PcmChunker: 3,200-byte chunks, order preserved
        except SarvamError:
            # Once a send fails, the audio stream to Sarvam has a gap: fail every later frame too,
            # instead of letting 4 of every 5 frames "succeed" into the chunker and hiding the loss.
            self._send_failed = True
            self.state = "failed"
            self.m["send_errors"] += 1
            self._log_lost_once()
            raise
        self.m["frames_in"] += 1
        self.m["bytes_in"] += len(pcm)

    async def close(self, reason: str) -> None:
        """Flush, end, wait for session.end (bounded), close. Idempotent; always releases the socket."""
        if self._closed:
            return
        self._closed = True
        self.m["close_reason"] = reason
        started = self._clock()
        try:
            if self._client.state == OPEN:
                self.state = "finalizing"
                await asyncio.wait_for(self._client.end(), self._finalize_timeout_s + CLOSE_MARGIN_S / 2)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        except SarvamError as exc:
            logger.warning("vobiz_stt_finalize_error", extra={**self._ids, "error": exc.code})
        finally:
            await self._client.close()
            self.m["finalize_ms"] = round((self._clock() - started) * 1000, 1)
            self.m["finalize_timed_out"] = not self.m["session_end_received"]
            if self.m["finalize_timed_out"]:
                self.m["finalize_timeouts"] += 1
            self.state = "closed"
            logger.info("vobiz_stt_session_closed", extra={**self._ids, **self.metrics()})

    # --- Sarvam events ------------------------------------------------------------------------

    async def _on_event(self, event: SttEvent) -> None:
        now = self._clock()
        since_audio = round((now - self._first_audio_at) * 1000, 1) if self._first_audio_at is not None else None
        if event.type == TRANSCRIPT_PARTIAL:
            self.m["partials"] += 1
            if self.m["first_partial_ms"] is None:
                self.m["first_partial_ms"] = since_audio
        elif event.type == TRANSCRIPT_FINAL:
            self.m["finals"] += 1
            if self.m["first_final_ms"] is None:
                self.m["first_final_ms"] = since_audio
            if self._last_speech_end_at is not None:
                self.m["last_final_after_speech_end_ms"] = round((now - self._last_speech_end_at) * 1000, 1)
        elif event.type == VAD_SPEECH_START:
            self.m["vad_speech_starts"] += 1
        elif event.type == VAD_SPEECH_END:
            self.m["vad_speech_ends"] += 1
            self._last_speech_end_at = now
        elif event.type == SESSION_END:
            self.m["session_end_received"] = True
            self.m["billed_audio_s"] = event.audio_duration_s
        elif event.type == ERROR and event.error is not None:
            self.m["stt_errors"] += 1
            if event.error.is_fatal:
                self.m["stt_fatal_errors"] += 1
                self.state = "failed"

        if event.type not in PUBLISHED_TYPES:
            return  # pong, config.updated, unknown: internal only
        self._sequence += 1
        transcript = TranscriptEvent(
            call_id=self._session.call_id,
            stream_id=self._session.stream_id,
            sequence=self._sequence,
            type=event.type,
            received_at=event.received_at,
            is_final=event.is_final,
            text=event.text,
            language=event.language,
            language_confidence=event.language_confidence,
            utterance_idx=event.utterance_idx,
            start_s=event.start_s,
            end_s=event.end_s,
            stt_request_id=event.request_id or self._client.request_id,
            error_code=event.error.code if event.error else None,
            error_is_fatal=event.error.is_fatal if event.error else None,
            track=self._track,  # None for legacy sinks; `speaker` is deliberately left at its default
        )
        try:
            await self._publisher.publish(transcript)
        except Exception as exc:  # a broken publisher must never break the call's STT
            self.m["publisher_errors"] += 1
            if self.m["publisher_errors"] == 1 or self.m["publisher_errors"] % 100 == 0:
                logger.warning("vobiz_transcript_publish_failed",
                               extra={**self._ids, "error": type(exc).__name__, "count": self.m["publisher_errors"]})

    # --- observability ------------------------------------------------------------------------

    def _log_lost_once(self) -> None:
        if not self._lost_logged:
            self._lost_logged = True
            err = self._client.last_error
            logger.warning("vobiz_stt_session_unavailable",
                           extra={**self._ids, "client_state": self._client.state,
                                  "error": err.code if isinstance(err, SarvamError) else None})

    def metrics(self) -> dict[str, Any]:
        s = self._client.stats
        return {
            "track": self._track,
            "stt_state": self.state,
            "stt_client_state": self._client.state,
            "stt_request_id": self._client.request_id,
            "audio_frames_forwarded": self.m["frames_in"],
            "audio_bytes_forwarded": self.m["bytes_in"],
            "audio_chunks_sent": s["chunks_sent"],
            "audio_bytes_sent": s["bytes_sent"],
            "transcript_partials": self.m["partials"],
            "transcript_finals": self.m["finals"],
            "vad_speech_starts": self.m["vad_speech_starts"],
            "vad_speech_ends": self.m["vad_speech_ends"],
            "stt_errors": self.m["stt_errors"],
            "stt_fatal_errors": self.m["stt_fatal_errors"],
            "send_errors": self.m["send_errors"],
            "publisher_errors": self.m["publisher_errors"],
            "first_partial_ms": self.m["first_partial_ms"],
            "first_final_ms": self.m["first_final_ms"],
            "last_final_after_speech_end_ms": self.m["last_final_after_speech_end_ms"],
            "finalize_ms": self.m["finalize_ms"],
            "finalize_timed_out": self.m["finalize_timed_out"],
            "finalize_timeouts": self.m["finalize_timeouts"],
            "session_end_received": self.m["session_end_received"],
            "billed_audio_s": self.m["billed_audio_s"],
            "close_reason": self.m["close_reason"],
        }


class SarvamSttSinkFactory:
    """Creates one connected SarvamSttSink per Vobiz stream. Validates configuration up front."""

    def __init__(
        self,
        settings: Settings,
        publisher: TranscriptPublisher | None = None,
        *,
        client_factory: ClientFactory = SarvamRealtimeSttClient,
    ) -> None:
        if not settings.sarvam_api_key.get_secret_value():
            raise SarvamConfigError("VOBIZ_SARVAM_STT_ENABLED=true requires SARVAM_API_KEY.")
        if not 0 < settings.vobiz_sarvam_stt_finalize_timeout_s <= 10:
            raise SarvamConfigError("VOBIZ_SARVAM_STT_FINALIZE_TIMEOUT_S must be > 0 and <= 10 seconds.")
        self._config = RealtimeSttConfig.from_settings(settings)  # raises SarvamConfigError if invalid
        self._api_key = settings.sarvam_api_key
        self._publisher = publisher or LoggingTranscriptPublisher()
        self._client_factory = client_factory
        self._finalize_timeout_s = settings.vobiz_sarvam_stt_finalize_timeout_s

    @property
    def config(self) -> RealtimeSttConfig:
        return self._config

    async def __call__(self, session: "StreamSession") -> SarvamSttSink:
        sink = SarvamSttSink(
            session, self._config, self._api_key, self._publisher,
            finalize_timeout_s=self._finalize_timeout_s, client_factory=self._client_factory,
        )
        await sink.connect()  # one new Sarvam WebSocket for this stream only
        return sink

    @property
    def close_timeout_s(self) -> float:
        """Worst-case time one sink needs to finalize and close (what its consumer waits for)."""
        return self._finalize_timeout_s + CLOSE_MARGIN_S

    async def create_for_track(self, session: Any, track: str) -> SarvamSttSink:
        """M5: one independent sink (and Sarvam WebSocket) for ONE track of a monitored physical stream.

        `session` is the per-track session view (physical call_id/stream_id, audio_format). The legacy
        `__call__` path above is unchanged. Each sink keeps its own sequence counter, so sequences are
        independent per track; events carry `track` and the physical `stream_id`.
        """
        sink = SarvamSttSink(
            session, self._config, self._api_key, self._publisher,
            finalize_timeout_s=self._finalize_timeout_s, client_factory=self._client_factory, track=track,
        )
        await sink.connect()
        return sink
