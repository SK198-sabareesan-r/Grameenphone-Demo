"""Server side of the Vobiz bidirectional WebSocket.

One `VobizMediaStreamHandler.handle()` call serves one socket. Flow:

  connect -> authenticate (token) -> accept -> `start` -> media/playedStream/clearedAudio/dtmf ...
  -> socket closes (the only end-of-stream signal; Vobiz sends no inbound `stop`).

Authentication modes (VOBIZ_WS_AUTH_MODE):
  query        token in the URL query (?token=...), verified BEFORE the socket is accepted.
  start_event  token delivered through <Stream extraHeaders> and verified on the `start` event
               (the socket is accepted first, but nothing is processed until `start` authenticates).
Vobiz documents no custom WebSocket authentication headers, so none are used.

The token is never logged. Uvicorn logs the request line including the query string, so the
logging setup redacts `token=` values (see app/core/logging.py).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from app.core.config import Settings
from app.integrations.vobiz.stream_events import (
    ClearedAudioEvent,
    DtmfEvent,
    IncorrectPayloadEvent,
    MediaEvent,
    PlayedStreamEvent,
    StartEvent,
    StreamEvent,
    StreamProtocolError,
    UnknownEvent,
    UnsupportedStreamFormat,
    parse_message,
    token_from_extra_headers,
)
from app.integrations.vobiz.stream_sender import VobizStreamSender, check_chunk_ms
from app.integrations.vobiz.stream_token import check_stream_token, token_from_header_safe
from app.services.audio.audio_buffer import AudioFrame, BoundedFrameQueue
from app.services.audio.formats import AudioFormatError, decode_media_payload
from app.services.voice.audio_consumer import AudioSinkFactory
from app.services.voice.call_manager import CallEndedError, CallStateManager, StreamSession, spawn_tracked
from app.services.voice.playback import PlaybackController

logger = logging.getLogger(__name__)

AUTH_MODES = ("query", "start_event")
MAX_MALFORMED_PER_CONNECTION = 20
MAX_CALL_ID_LEN = 128

# WebSocket close codes
CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_UNSUPPORTED = 1003
CLOSE_POLICY = 1008
CLOSE_TOO_BIG = 1009
CLOSE_ERROR = 1011
CLOSE_TRY_LATER = 1013


class _CloseConnection(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code, self.reason = code, reason


@dataclass
class _Connection:
    call_uuid: str
    session: StreamSession | None = None
    malformed: int = 0
    rate_limited_warnings: int = 0
    kind: str = "primary"  # "monitor" for a monitored stream (M5): opt-in multi-stream + per-track routing
    # Who closed the socket first ("server" = us, "client" = Vobiz) and the code used.
    close_code: int | None = None
    closed_by: str | None = None

    def record_close(self, code: int | None, by: str) -> None:
        if self.closed_by is None:  # the first close wins; the peer's echo is not the cause
            self.close_code, self.closed_by = code, by


class VobizMediaStreamHandler:
    def __init__(
        self,
        settings: Settings,
        calls: CallStateManager,
        *,
        audio_sink_factory: AudioSinkFactory | None = None,
        monitor_sink_factory: AudioSinkFactory | None = None,
    ) -> None:
        """`audio_sink_factory` creates the per-stream inbound-audio sink (M4: Sarvam STT).

        None means no consumer: streams report consumer_state="unavailable" and frames are
        buffered and overflow exactly as before. There is deliberately no no-op default.
        """
        if settings.vobiz_ws_auth_mode not in AUTH_MODES:
            raise ValueError(f"VOBIZ_WS_AUTH_MODE must be one of {AUTH_MODES}")
        check_chunk_ms(settings.vobiz_playback_chunk_ms)
        self._settings = settings
        self._calls = calls
        self._sink_factory = audio_sink_factory
        # M5: sink factory for monitored streams (e.g. a TrackRoutingSinkFactory). None = no consumer for
        # them. Legacy streams never use it.
        self._monitor_sink_factory = monitor_sink_factory
        self._connections = 0

    @property
    def audio_sink_factory(self) -> AudioSinkFactory | None:
        return self._sink_factory

    @property
    def monitor_sink_factory(self) -> AudioSinkFactory | None:
        return self._monitor_sink_factory

    @property
    def connection_count(self) -> int:
        return self._connections

    # --- entry point --------------------------------------------------------------------------

    async def handle(self, websocket: WebSocket, call_uuid: str, kind: str = "primary") -> None:
        s = self._settings
        if not call_uuid or len(call_uuid) > MAX_CALL_ID_LEN or any(c.isspace() or ord(c) < 32 for c in call_uuid):
            logger.warning("vobiz_ws_rejected", extra={"reason": "bad_call_uuid"})
            await websocket.close(code=CLOSE_POLICY)
            return
        if self._connections >= s.vobiz_ws_max_connections:
            logger.warning("vobiz_ws_rejected", extra={"call_id": call_uuid, "reason": "connection_limit"})
            await websocket.close(code=CLOSE_TRY_LATER)
            return

        auth_token = s.vobiz_auth_token.get_secret_value()
        if not auth_token:
            logger.error("vobiz_ws_rejected", extra={"call_id": call_uuid, "reason": "auth_token_not_configured"})
            await websocket.close(code=CLOSE_ERROR)
            return

        if s.vobiz_ws_auth_mode == "query":
            status = check_stream_token(auth_token, call_uuid, websocket.query_params.get("token"))
            if status != "valid":
                # Rejected before accept: the client sees an HTTP 403 handshake failure.
                logger.warning("vobiz_ws_rejected", extra={"call_id": call_uuid, "reason": f"token_{status}"})
                await websocket.close(code=CLOSE_POLICY)
                return

        self._connections += 1
        conn = _Connection(call_uuid=call_uuid, kind=kind)
        end_reason, close_code = "socket_closed", CLOSE_NORMAL
        try:
            await websocket.accept()
            await self._receive_loop(websocket, conn)
        except _CloseConnection as stop:
            end_reason, close_code = stop.reason, stop.code
            conn.record_close(stop.code, "server")
        except WebSocketDisconnect as exc:
            end_reason = "socket_closed"
            conn.record_close(exc.code, "client")
        except asyncio.CancelledError:
            end_reason = "cancelled"
            raise
        except Exception as exc:  # never let one bad connection take the server down
            end_reason, close_code = "internal_error", CLOSE_ERROR
            logger.error(
                "vobiz_ws_error",
                extra={"call_id": call_uuid, "stream_id": conn.session.stream_id if conn.session else None,
                       "error": type(exc).__name__},
            )
        finally:
            self._connections -= 1
            await self._cleanup(websocket, conn, end_reason, close_code)

    # --- connection loop ----------------------------------------------------------------------

    async def _receive_loop(self, websocket: WebSocket, conn: _Connection) -> None:
        s = self._settings
        while True:
            waiting_for_start = conn.session is None
            timeout = s.vobiz_ws_start_timeout_s if waiting_for_start else s.vobiz_ws_idle_timeout_s
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout)
            except asyncio.TimeoutError:
                raise _CloseConnection(
                    CLOSE_POLICY if waiting_for_start else CLOSE_GOING_AWAY,
                    "start_timeout" if waiting_for_start else "idle_timeout",
                ) from None

            if message["type"] == "websocket.disconnect":
                conn.record_close(message.get("code"), "client")
                return
            text = message.get("text")
            if text is None:  # Vobiz speaks JSON text frames; binary is unexpected
                self._malformed(conn, "binary_frame")
                continue
            if len(text) > s.vobiz_ws_max_message_bytes:
                raise _CloseConnection(CLOSE_TOO_BIG, "message_too_large")

            try:
                event = parse_message(text)
            except UnsupportedStreamFormat as exc:
                logger.warning("vobiz_ws_unsupported_format", extra={"call_id": conn.call_uuid, "reason": exc.reason})
                raise _CloseConnection(CLOSE_UNSUPPORTED, "unsupported_format") from exc
            except StreamProtocolError as exc:
                self._malformed(conn, exc.reason)
                continue
            await self._dispatch(websocket, conn, event)

    def _malformed(self, conn: _Connection, reason: str) -> None:
        conn.malformed += 1
        if conn.session is not None:
            conn.session.stats.malformed += 1
        if conn.malformed <= 3 or conn.malformed % 50 == 0:  # bounded log volume
            logger.warning(
                "vobiz_ws_malformed_message",
                extra={"call_id": conn.call_uuid, "reason": reason, "count": conn.malformed},
            )
        if conn.malformed > MAX_MALFORMED_PER_CONNECTION:
            raise _CloseConnection(CLOSE_POLICY, "too_many_malformed_messages")

    # --- event handling -----------------------------------------------------------------------

    async def _dispatch(self, websocket: WebSocket, conn: _Connection, event: StreamEvent) -> None:
        session = conn.session
        if isinstance(event, StartEvent):
            await self._on_start(websocket, conn, event)
            return
        if session is None:  # anything before `start` is a protocol violation
            self._malformed(conn, f"{type(event).__name__}_before_start")
            return

        if isinstance(event, MediaEvent):
            self._on_media(conn, session, event)
        elif isinstance(event, PlayedStreamEvent):
            if not session.playback.handle_played_stream(event.name):
                session.stats.played_stream_unmatched += 1
        elif isinstance(event, ClearedAudioEvent):
            if event.stream_id is not None and event.stream_id != session.stream_id:
                session.stats.unexpected_stream += 1
                return
            session.playback.handle_cleared_audio()
        elif isinstance(event, DtmfEvent):
            session.stats.dtmf_events += 1  # the digit itself is never logged
            await self._calls.notify_dtmf(session, event)
        elif isinstance(event, IncorrectPayloadEvent):
            logger.warning(
                "vobiz_ws_incorrect_payload",
                extra={"call_id": session.call_id, "stream_id": session.stream_id, "payload_length": event.payload_length},
            )
        elif isinstance(event, UnknownEvent):
            logger.debug("vobiz_ws_unknown_event", extra={"call_id": session.call_id, "event_name": event.name})

    async def _on_start(self, websocket: WebSocket, conn: _Connection, event: StartEvent) -> None:
        s = self._settings
        if conn.session is not None:
            self._malformed(conn, "duplicate_start")  # one stream per socket; ignore the repeat
            return
        if event.call_id != conn.call_uuid:
            logger.warning("vobiz_ws_rejected", extra={"call_id": conn.call_uuid, "reason": "call_id_mismatch"})
            raise _CloseConnection(CLOSE_POLICY, "call_id_mismatch")

        if s.vobiz_ws_auth_mode == "start_event":
            supplied = token_from_header_safe(token_from_extra_headers(event.extra_headers))
            status = check_stream_token(s.vobiz_auth_token.get_secret_value(), conn.call_uuid, supplied)
            if status != "valid":
                logger.warning("vobiz_ws_rejected", extra={"call_id": conn.call_uuid, "reason": f"token_{status}"})
                raise _CloseConnection(CLOSE_POLICY, f"token_{status}")

        async def close_socket(code: int, reason: str) -> None:
            if websocket.application_state == WebSocketState.CONNECTED:
                conn.record_close(code, "server")  # e.g. hangup webhook or reconnect superseded us
                await websocket.close(code=code, reason=reason)

        async def send_text(text: str) -> None:
            if websocket.application_state != WebSocketState.CONNECTED:
                raise ConnectionError("socket not connected")
            await websocket.send_text(text)

        sender = VobizStreamSender(event.stream_id, send_text, chunk_ms=s.vobiz_playback_chunk_ms)
        session = StreamSession(
            call_id=event.call_id,
            stream_id=event.stream_id,
            account_id=event.account_id,
            tracks=event.tracks,
            audio_format=event.media_format,
            queue=BoundedFrameQueue(s.vobiz_ws_queue_frames),
            sender=sender,
            playback=PlaybackController(
                sender, checkpoint_timeout_s=s.vobiz_checkpoint_timeout_s, call_id=event.call_id
            ),
            socket_closer=close_socket,
            kind=conn.kind,
        )
        try:
            call = await self._calls.attach_stream(session)
        except CallEndedError:
            logger.info("vobiz_ws_rejected", extra={"call_id": conn.call_uuid, "reason": "call_already_ended"})
            await session.close("call_already_ended", close_socket=False)
            raise _CloseConnection(CLOSE_NORMAL, "call_already_ended") from None
        conn.session = session
        # Started after the stream is current, so a reconnect has already retired (and awaited) the
        # previous stream's consumer. Non-blocking: the sink is created inside the consumer task.
        factory = self._monitor_sink_factory if conn.kind == "monitor" else self._sink_factory
        session.start_consumer(factory, drain_timeout_s=self._settings.vobiz_audio_drain_timeout_s)
        logger.info(
            "vobiz_stream_started",
            extra={
                "call_id": session.call_id,
                "stream_id": session.stream_id,
                "kind": session.kind,
                "audio_format": session.audio_format.label,
                "tracks": list(session.tracks),
                "reconnect": call.reconnects > 0,
                "streams_seen": call.streams_seen,
            },
        )

    def _on_media(self, conn: _Connection, session: StreamSession, event: MediaEvent) -> None:
        if event.stream_id != session.stream_id:
            session.stats.unexpected_stream += 1
            if session.stats.unexpected_stream <= 3:
                logger.warning(
                    "vobiz_ws_unexpected_stream_id",
                    extra={"call_id": session.call_id, "stream_id": session.stream_id},
                )
            return
        try:
            data = decode_media_payload(event.payload, session.audio_format)
        except AudioFormatError as exc:
            self._malformed(conn, f"bad_media:{type(exc).__name__}")
            return
        frame = AudioFrame(
            data=data,
            format=session.audio_format,  # as announced in `start`; never altered
            track=event.track,
            timestamp_ms=event.timestamp_ms,
            chunk=event.chunk,
            sequence=event.sequence_number,
            stream_id=session.stream_id,
            received_at=time.monotonic(),
        )
        if session.stats.frames_received == 0:  # once per stream; proves inbound audio is flowing
            logger.info(
                "vobiz_stream_first_media",
                extra={
                    "call_id": session.call_id,
                    "stream_id": session.stream_id,
                    "track": event.track,
                    "frame_bytes": len(data),
                    "frame_ms": round(session.audio_format.duration_ms(len(data)), 1),
                    "audio_format": session.audio_format.label,
                    "ms_since_start": round((time.time() - session.started_at) * 1000),
                },
            )
        stored_without_loss = session.queue.put(frame)
        session.stats.frames_received += 1
        session.stats.bytes_received += len(data)
        session.stats.last_sequence = event.sequence_number
        if not stored_without_loss and not session.queue.closed:
            dropped = session.queue.dropped
            if dropped == 1 or dropped % 500 == 0:  # first overflow, then every 500th
                logger.warning(
                    "vobiz_audio_queue_overflow",
                    extra={
                        "call_id": session.call_id,
                        "stream_id": session.stream_id,
                        "dropped_frames": dropped,
                        "consumer_state": session.consumer.state if session.consumer else "unavailable",
                    },
                )

    # --- cleanup ------------------------------------------------------------------------------

    async def _cleanup(self, websocket: WebSocket, conn: _Connection, reason: str, code: int) -> None:
        session = conn.session
        if session is not None:
            # Run detach + the end-of-stream summary in a tracked task awaited through shield(): if this
            # handler is cancelled mid-cleanup (server shutdown, test-client teardown), the stream is
            # still detached, listeners are still notified of the end, and the summary is still logged.
            await asyncio.shield(spawn_tracked(
                self._end_stream(session, conn, reason, code), f"vobiz-stream-end:{session.stream_id}"
            ))
        if websocket.application_state == WebSocketState.CONNECTED:
            try:
                await websocket.close(code=code)
            except Exception:  # peer already gone
                pass

    async def _end_stream(self, session: StreamSession, conn: _Connection, reason: str, code: int) -> None:
        await self._calls.detach_stream(session, session.end_reason or reason)
        playback = session.playback
        consumer = session.consumer_metrics()
        logger.info(
            "vobiz_stream_ended",
            extra={
                "call_id": session.call_id,
                "stream_id": session.stream_id,
                "reason": session.end_reason or reason,
                "close_code": conn.close_code if conn.close_code is not None else code,
                "closed_by": conn.closed_by or "server",
                "duration_s": round(time.time() - session.started_at, 3),
                "frames_received": session.stats.frames_received,
                "bytes_received": session.stats.bytes_received,
                "frames_dropped": session.queue.dropped,
                "play_audio_messages": playback.play_audio_messages,
                "utterances_sent": playback.utterances_sent,
                "utterances_aborted": playback.utterances_aborted,
                "checkpoint_outcomes": playback.checkpoint_outcomes,
                "played_stream_unmatched": session.stats.played_stream_unmatched,
                "malformed": session.stats.malformed,
                "unexpected_stream": session.stats.unexpected_stream,
                "consumer_state": consumer["consumer_state"],
                "frames_processed": consumer["frames_processed"],
                "processing_errors": consumer["processing_errors"],
                "frames_discarded_on_close": consumer["frames_discarded_on_close"],
                "consumer_latency_max_ms": consumer["consumer_latency_max_ms"],
            },
        )
