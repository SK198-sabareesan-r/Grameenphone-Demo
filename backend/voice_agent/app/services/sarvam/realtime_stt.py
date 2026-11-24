"""Async client for Sarvam Realtime Streaming STT (saaras:v4). Independent of Vobiz.

Lifecycle:
    client = SarvamRealtimeSttClient(config, api_key, on_event=handler)
    await client.connect()            # handshake + wait for session.begin (bounded retries)
    await client.send_audio(pcm)      # any size; aggregated into config.chunk_ms chunks
    await client.end()                # flush, {"event":"end"}, wait for session.end, close
    (or: async with client: ...)      # close() is always safe and idempotent

* Auth: `api-subscription-key` header only. The key never goes into the URL, logs or errors.
* One persistent socket; one receiver task; one keepalive task that sends {"event":"ping"} when no
  message has been sent for `keepalive_interval_s` (the server closes idle sessions after 60 s).
* Reconnect: bounded exponential backoff, only for transient failures (timeouts, network errors,
  HTTP 5xx, close 1011). Never for bad keys, quota/rate limits (close 1003), invalid parameters
  (close 4000), or inactivity (1008). Mid-session auto-reconnect is opt-in (`auto_reconnect`);
  while reconnecting, send_audio() raises instead of silently dropping audio.
* Logs: event names, ids, counters, error codes. Never audio, base64, the key, or transcript text.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import SecretStr
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from app.services.sarvam import constants as c
from app.services.sarvam.errors import (
    SarvamAuthError,
    SarvamConfigError,
    SarvamConnectionError,
    SarvamError,
    SarvamRateLimitError,
    SarvamRequestError,
    SarvamServerError,
    SarvamStreamClosedError,
    SarvamTimeoutError,
)
from app.services.sarvam.realtime_protocol import (
    ERROR,
    PONG,
    SESSION_BEGIN,
    SESSION_END,
    TRANSCRIPT_FINAL,
    TRANSCRIPT_PARTIAL,
    UNKNOWN,
    PcmChunker,
    RealtimeSttConfig,
    SarvamProtocolError,
    SttError,
    SttEvent,
    build_audio_input,
    build_end,
    build_ping,
    parse_server_message,
)

logger = logging.getLogger(__name__)

EventHandler = Callable[[SttEvent], "Awaitable[None] | None"]
ConnectFn = Callable[..., Awaitable[Any]]

# States
IDLE, CONNECTING, OPEN, RECONNECTING, CLOSING, CLOSED, FAILED = (
    "idle", "connecting", "open", "reconnecting", "closing", "closed", "failed",
)
MAX_PROTOCOL_ERRORS = 50


# --- error mapping ---------------------------------------------------------------------------


def error_from_event(err: SttError) -> SarvamError:
    info = {"upstream_status": err.status_code, "upstream_code": err.code}
    text = f"Sarvam realtime error '{err.code}': {err.message}"
    if err.code == "invalid_subscription_key":
        return SarvamAuthError(text, **info)
    if err.code in ("invalid_request", "invalid_model"):
        return SarvamRequestError(text, **info)
    if err.code == "inactivity_timeout":
        return SarvamStreamClosedError(text, close_code=c.REALTIME_CLOSE_INACTIVITY, **info)
    if err.status_code is not None and err.status_code >= 500:
        return SarvamServerError(text, **info)
    return SarvamRequestError(text, **info)


def error_from_close(code: int | None, reason: str) -> SarvamError:
    reason = (reason or "")[:123]
    if code == c.REALTIME_CLOSE_AUTH_OR_QUOTA:
        return SarvamAuthError(f"Sarvam closed the stream (1003: invalid key, rate limit or quota). {reason}".strip())
    if code == c.REALTIME_CLOSE_INVALID_REQUEST:
        return SarvamRequestError(f"Sarvam rejected the connection parameters (4000). {reason}".strip())
    if code == c.REALTIME_CLOSE_SERVER_ERROR:
        return SarvamServerError(f"Sarvam internal error (1011). {reason}".strip())
    if code == c.REALTIME_CLOSE_INACTIVITY:
        return SarvamStreamClosedError("Sarvam closed the stream: no audio for 60 s (1008).", close_code=code)
    if code in (1000, 1001):
        return SarvamStreamClosedError(f"Sarvam closed the stream ({code}).", close_code=code)
    return SarvamConnectionError(f"Sarvam stream closed abnormally ({code}).")


def error_from_handshake_status(status: int) -> SarvamError:
    if status in (401, 403):
        return SarvamAuthError(f"Sarvam rejected the API key during the WebSocket handshake (HTTP {status}).",
                               upstream_status=status)
    if status == 429:
        return SarvamRateLimitError("Sarvam realtime rate limit (HTTP 429).", upstream_status=status)
    if status >= 500:
        return SarvamServerError(f"Sarvam realtime handshake failed (HTTP {status}).", upstream_status=status)
    return SarvamRequestError(f"Sarvam realtime handshake rejected (HTTP {status}).", upstream_status=status)


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, SarvamStreamClosedError):
        return exc.retryable
    return isinstance(exc, (SarvamTimeoutError, SarvamConnectionError, SarvamServerError))


def _close_info(exc: ConnectionClosed) -> tuple[int | None, str]:
    frame = exc.rcvd
    return (frame.code, frame.reason) if frame is not None else (None, "")


# --- client ----------------------------------------------------------------------------------


class SarvamRealtimeSttClient:
    def __init__(
        self,
        config: RealtimeSttConfig,
        api_key: SecretStr | str,
        *,
        on_event: EventHandler | None = None,
        auto_reconnect: bool = False,
        connect_fn: ConnectFn | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config.validated()
        self._key = api_key if isinstance(api_key, SecretStr) else SecretStr(api_key)
        self._on_event = on_event
        self._auto_reconnect = auto_reconnect
        self._connect_fn = connect_fn or ws_connect
        self._sleep = sleep
        self._clock = clock
        self._chunker = PcmChunker(config.chunk_bytes, config.bytes_per_sample)
        self._ws: Any = None
        self._send_lock = asyncio.Lock()  # one frame at a time on the socket
        self._audio_lock = asyncio.Lock()  # chunk + send as one step, so audio order is preserved
        self._fatal_error: SarvamError | None = None
        self._shut = False
        self._receiver: asyncio.Task[None] | None = None
        self._keepalive: asyncio.Task[None] | None = None
        self._reconnector: asyncio.Task[None] | None = None
        self._session_ended = asyncio.Event()
        self._closed = asyncio.Event()
        self._last_sent = 0.0
        self.state = IDLE
        self.request_id: str | None = None
        self.last_error: SarvamError | None = None
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self.stats: dict[str, Any] = {
            "connect_attempts": 0, "reconnects": 0, "chunks_sent": 0, "bytes_sent": 0, "pings_sent": 0,
            "pongs": 0, "partials": 0, "finals": 0, "errors": 0, "protocol_errors": 0, "unknown_events": 0,
            "handler_errors": 0, "audio_duration_s": None,
        }

    # --- properties ---------------------------------------------------------------------------

    @property
    def config(self) -> RealtimeSttConfig:
        return self._config

    @property
    def is_open(self) -> bool:
        return self.state == OPEN

    @property
    def _ids(self) -> dict[str, Any]:
        return {"request_id": self.request_id, "model": self._config.model}

    # --- connect ------------------------------------------------------------------------------

    async def connect(self) -> SttEvent:
        """Open the socket and wait for session.begin, retrying transient failures (bounded)."""
        if not self._key.get_secret_value():
            raise SarvamConfigError("SARVAM_API_KEY is not set.")
        if self._shut:
            raise SarvamConnectionError("client is closed; create a new one")
        if self.state not in (IDLE, FAILED):
            raise SarvamConnectionError(f"connect() called in state '{self.state}'")
        self.state = CONNECTING
        try:
            return await self._connect_with_retry()
        except BaseException:
            if self.state == CONNECTING:
                self.state = FAILED
            raise

    async def _connect_with_retry(self) -> SttEvent:
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._open_once()
            except asyncio.CancelledError:
                raise
            except SarvamError as exc:
                self.last_error = exc
                retries_left = self._config.max_reconnect_attempts - (attempt - 1)
                if not is_retryable(exc) or retries_left <= 0:
                    logger.error(
                        "sarvam_stt_connect_failed",
                        extra={**self._ids, "error": exc.code, "attempts": attempt, "retryable": is_retryable(exc),
                               "upstream_status": exc.upstream_status, "upstream_code": exc.upstream_code},
                    )
                    raise
                delay = self._config.backoff_delay(attempt)
                logger.warning(
                    "sarvam_stt_connect_retry",
                    extra={**self._ids, "error": exc.code, "attempt": attempt, "retry_in_s": delay},
                )
                await self._sleep(delay)

    async def _open_once(self) -> SttEvent:
        cfg = self._config
        self.stats["connect_attempts"] += 1
        logger.info("sarvam_stt_connecting", extra={**cfg.query_params(), "attempt": self.stats["connect_attempts"]})
        try:
            ws = await asyncio.wait_for(
                self._connect_fn(
                    cfg.build_url(),
                    additional_headers={c.AUTH_HEADER: self._key.get_secret_value()},
                    open_timeout=cfg.connect_timeout_s,
                    max_size=cfg.max_message_bytes,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ),
                cfg.connect_timeout_s + 1,
            )
        except InvalidStatus as exc:
            raise error_from_handshake_status(exc.response.status_code) from None
        except (asyncio.TimeoutError, TimeoutError):
            raise SarvamTimeoutError("Timed out opening the Sarvam realtime WebSocket.") from None
        except (InvalidHandshake, OSError) as exc:
            raise SarvamConnectionError(f"Could not connect to Sarvam realtime STT ({type(exc).__name__}).") from None

        try:
            begin = await asyncio.wait_for(self._await_session_begin(ws), cfg.connect_timeout_s)
        except (asyncio.TimeoutError, TimeoutError):
            await self._safe_close_ws(ws)
            raise SarvamTimeoutError("Sarvam did not send session.begin in time.") from None
        except BaseException:
            await self._safe_close_ws(ws)
            raise

        self._ws = ws
        self.request_id = begin.request_id
        self._session_ended.clear()
        self._closed.clear()
        self._last_sent = self._clock()
        self.state = OPEN
        self._receiver = asyncio.create_task(self._receive_loop(ws), name="sarvam-stt-receiver")
        self._keepalive = asyncio.create_task(self._keepalive_loop(ws), name="sarvam-stt-keepalive")
        echoed = begin.session_config or {}
        logger.info(
            "sarvam_stt_session_begin",
            extra={"request_id": begin.request_id,
                   "server_config": {k: echoed.get(k) for k in (
                       "model", "language_code", "encoding", "sample_rate", "stream_type", "mode",
                       "turn_detection", "return_timestamps") if k in echoed}},
        )
        await self._dispatch(begin)
        return begin

    async def _await_session_begin(self, ws: Any) -> SttEvent:
        """The first meaningful server message must be session.begin; an error event is mapped."""
        while True:
            try:
                raw = await ws.recv()
            except ConnectionClosed as exc:
                code, reason = _close_info(exc)
                self.close_code, self.close_reason = code, reason
                raise error_from_close(code, reason) from None
            try:
                event = parse_server_message(raw)
            except SarvamProtocolError as exc:
                raise SarvamConnectionError(f"Unexpected first message from Sarvam ({exc.reason}).") from None
            if event.type == SESSION_BEGIN:
                return event
            if event.type == ERROR and event.error is not None:
                self._log_error_event(event)
                raise error_from_event(event.error)
            # Anything else before session.begin is unexpected but harmless; keep waiting (bounded by timeout).
            logger.warning("sarvam_stt_unexpected_event", extra={"event_type": event.type, "phase": "before_session_begin"})

    # --- sending ------------------------------------------------------------------------------

    async def send_audio(self, pcm: bytes) -> int:
        """Queue raw PCM; full chunks are sent immediately, in order. Returns chunks sent."""
        async with self._audio_lock:
            self._require_open()
            chunks = self._chunker.push(pcm)  # raises on misaligned input; bytes never altered
            for chunk in chunks:
                await self._send_text(build_audio_input(chunk), audio_bytes=len(chunk))
            return len(chunks)

    async def flush_audio(self) -> bool:
        """Send any buffered partial chunk (call before end() or at an utterance boundary)."""
        async with self._audio_lock:
            self._require_open()
            rest = self._chunker.flush()
            if rest:
                await self._send_text(build_audio_input(rest), audio_bytes=len(rest))
                return True
            return False

    async def ping(self) -> None:
        self._require_open()
        await self._send_text(build_ping(), audio_bytes=0, is_ping=True)

    def _require_open(self) -> None:
        if self.state == RECONNECTING:
            raise SarvamConnectionError("Sarvam realtime stream is reconnecting; audio was not sent.")
        if self.state != OPEN or self._ws is None:
            raise SarvamConnectionError(f"Sarvam realtime stream is not open (state '{self.state}').")

    async def _send_text(self, text: str, *, audio_bytes: int, is_ping: bool = False) -> None:
        async with self._send_lock:
            ws = self._ws
            if ws is None or self.state not in (OPEN, CLOSING):
                raise SarvamConnectionError("Sarvam realtime stream closed while sending.")
            try:
                await ws.send(text)
            except ConnectionClosed as exc:
                code, reason = _close_info(exc)
                raise error_from_close(code, reason) from None
            self._last_sent = self._clock()
            if is_ping:
                self.stats["pings_sent"] += 1
            elif audio_bytes:
                self.stats["chunks_sent"] += 1
                self.stats["bytes_sent"] += audio_bytes

    # --- receiving ----------------------------------------------------------------------------

    async def _receive_loop(self, ws: Any) -> None:
        try:
            while True:
                raw = await ws.recv()
                try:
                    event = parse_server_message(raw, request_id=self.request_id)
                except SarvamProtocolError as exc:
                    self.stats["protocol_errors"] += 1
                    if self.stats["protocol_errors"] <= 3 or self.stats["protocol_errors"] % 50 == 0:
                        logger.warning("sarvam_stt_malformed_message",
                                       extra={**self._ids, "reason": exc.reason, "count": self.stats["protocol_errors"]})
                    if self.stats["protocol_errors"] >= MAX_PROTOCOL_ERRORS:
                        self._fatal_error = exc  # not retryable: the peer is not speaking the protocol
                        await self._safe_close_ws(ws)
                        await self._on_socket_closed(ws, None, "too_many_malformed_messages")
                        return
                    continue
                await self._handle_event(event)
        except ConnectionClosed as exc:
            code, reason = _close_info(exc)
            await self._on_socket_closed(ws, code, reason)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let the receiver die silently
            logger.error("sarvam_stt_receiver_error", extra={**self._ids, "error": type(exc).__name__})
            self._fatal_error = SarvamConnectionError(f"receiver failed ({type(exc).__name__})")
            await self._safe_close_ws(ws)
            await self._on_socket_closed(ws, None, "")

    async def _handle_event(self, event: SttEvent) -> None:
        if event.type == TRANSCRIPT_PARTIAL:
            self.stats["partials"] += 1
        elif event.type == TRANSCRIPT_FINAL:
            self.stats["finals"] += 1
            logger.info(
                "sarvam_stt_final",
                extra={**self._ids, "utterance_idx": event.utterance_idx, "language": event.language,
                       "text_length": len(event.text or "")},  # length only: text is customer speech
            )
        elif event.type == PONG:
            self.stats["pongs"] += 1
        elif event.type == ERROR and event.error is not None:
            self.stats["errors"] += 1
            self._log_error_event(event)
            if event.error.is_fatal:  # the close frame that follows is less specific than this
                self._fatal_error = error_from_event(event.error)
        elif event.type == SESSION_END:
            self.stats["audio_duration_s"] = event.audio_duration_s
            logger.info(
                "sarvam_stt_session_end",
                extra={**self._ids, "audio_duration_s": event.audio_duration_s,
                       "total_utterances": event.total_utterances, "total_duration_s": event.total_duration_s},
            )
            self._session_ended.set()
        elif event.type == UNKNOWN:
            self.stats["unknown_events"] += 1
            logger.warning("sarvam_stt_unexpected_event", extra={**self._ids, "event_type": event.raw_event})
        await self._dispatch(event)

    async def _dispatch(self, event: SttEvent) -> None:
        if self._on_event is None:
            return
        try:
            result = self._on_event(event)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats["handler_errors"] += 1
            logger.warning("sarvam_stt_handler_error", extra={**self._ids, "event_type": event.type,
                                                               "error": type(exc).__name__})

    def _log_error_event(self, event: SttEvent) -> None:
        err = event.error
        assert err is not None
        logger.log(
            logging.ERROR if err.is_fatal else logging.WARNING,
            "sarvam_stt_error",
            extra={**self._ids, "code": err.code, "is_fatal": err.is_fatal, "status_code": err.status_code,
                   "error_message": err.message},
        )

    async def _on_socket_closed(self, ws: Any, code: int | None, reason: str) -> None:
        if ws is not self._ws:
            return  # a superseded socket
        self.close_code, self.close_reason = code, (reason or "")[:123]
        self._session_ended.set()
        if self.state in (CLOSING, CLOSED):
            return
        error = self._fatal_error or error_from_close(code, reason)
        self._fatal_error = None
        self.last_error = error
        logger.warning("sarvam_stt_disconnected",
                       extra={**self._ids, "close_code": code, "error": error.code, "retryable": is_retryable(error)})
        if (
            self._auto_reconnect
            and is_retryable(error)
            and self.stats["reconnects"] < self._config.max_reconnect_attempts  # lifetime cap
        ):
            self.state = RECONNECTING
            self._reconnector = asyncio.create_task(self._reconnect(), name="sarvam-stt-reconnect")
        else:
            self.state = FAILED
            self._stop_keepalive()
            self._closed.set()

    async def _reconnect(self) -> None:
        old = self._ws
        self._ws = None
        self._stop_keepalive()
        await self._safe_close_ws(old)
        self.stats["reconnects"] += 1
        try:
            await self._sleep(self._config.backoff_delay(1))
            self.state = CONNECTING
            await self._connect_with_retry()  # bounded; on success state becomes OPEN
            logger.info("sarvam_stt_reconnected", extra={**self._ids, "reconnects": self.stats["reconnects"]})
        except asyncio.CancelledError:
            raise
        except SarvamError as exc:
            self.last_error = exc
            self.state = FAILED
            self._closed.set()

    # --- keepalive ----------------------------------------------------------------------------

    async def _keepalive_loop(self, ws: Any) -> None:
        interval = self._config.keepalive_interval_s
        try:
            while ws is self._ws and self.state == OPEN:
                await asyncio.sleep(interval)  # a real timer; the injectable sleep is for backoff only
                if ws is not self._ws or self.state != OPEN:
                    return
                if self._clock() - self._last_sent >= interval:
                    try:
                        await self._send_text(build_ping(), audio_bytes=0, is_ping=True)
                    except SarvamError:
                        return  # the receiver reports the disconnect
        except asyncio.CancelledError:
            raise

    def _stop_keepalive(self) -> None:
        if self._keepalive is not None and not self._keepalive.done() and self._keepalive is not asyncio.current_task():
            self._keepalive.cancel()

    # --- shutdown -----------------------------------------------------------------------------

    async def end(self) -> SttEvent | None:
        """Graceful end: flush audio, send {"event":"end"}, wait for session.end (bounded), close."""
        if self.state == OPEN:
            try:
                await self.flush_audio()
                self.state = CLOSING
                await self._send_text(build_end(), audio_bytes=0)
                with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                    await asyncio.wait_for(self._session_ended.wait(), self._config.end_timeout_s)
            except SarvamError as exc:
                self.last_error = exc
        await self.close()
        return None

    async def close(self) -> None:
        """Cancel and await all tasks, close the socket. Idempotent and cancellation-safe.

        To abort a connect() in progress, cancel the task running connect().
        """
        if self._shut:
            return
        self._shut = True
        previous = self.state
        self.state = CLOSING
        current = asyncio.current_task()
        for task in (self._reconnector, self._keepalive, self._receiver):
            if task is not None and task is not current and not task.done():
                task.cancel()
        for task in (self._reconnector, self._keepalive, self._receiver):
            if task is not None and task is not current:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        ws, self._ws = self._ws, None
        await self._safe_close_ws(ws)
        self.state = CLOSED if previous != FAILED else FAILED
        self._closed.set()
        self._session_ended.set()
        logger.info("sarvam_stt_closed", extra={**self._ids, "close_code": self.close_code, "final_state": self.state,
                                                **{k: v for k, v in self.stats.items()}})

    async def wait_closed(self) -> None:
        await self._closed.wait()

    @staticmethod
    async def _safe_close_ws(ws: Any) -> None:
        if ws is None:
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.close(), 5)

    async def __aenter__(self) -> "SarvamRealtimeSttClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
