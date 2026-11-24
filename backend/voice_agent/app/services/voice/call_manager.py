"""Per-call and per-stream state.

State is keyed by Vobiz `callId`. A call has at most one CURRENT stream; a reconnect arrives as a
new socket with a new `streamId` for the same callId and replaces the current stream (the old one
is closed and its buffers and checkpoints are released).

The WebSocket close is the end of a STREAM. The hangup webhook is the end of the CALL. Either may
arrive first; every cleanup path here is idempotent.

SINGLE PROCESS ONLY. State lives in this process's memory, so with several uvicorn workers a
call's WebSocket, its hangup webhook and its control requests could land on different workers.
Multi-worker deployment needs shared state (e.g. Redis) and sticky routing for the sockets.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from app.integrations.vobiz.stream_events import DtmfEvent
from app.integrations.vobiz.stream_sender import VobizStreamSender
from app.services.audio.audio_buffer import BoundedFrameQueue
from app.services.audio.formats import AudioFormat
from app.services.voice.audio_consumer import AudioSinkFactory, InboundAudioConsumer
from app.services.voice.playback import PlaybackController

logger = logging.getLogger(__name__)

SocketCloser = Callable[[int, str], Awaitable[None]]

# Stream cleanups still running (strong references so they are never garbage-collected mid-way).
_PENDING_CLEANUPS: set[asyncio.Task[None]] = set()


async def wait_for_pending_cleanups(timeout_s: float) -> int:
    """Wait (bounded) for stream cleanups that are still finishing. Returns how many were pending."""
    pending = [t for t in _PENDING_CLEANUPS if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout_s)
    return len(pending)


def spawn_tracked(coro: Awaitable[None], name: str) -> "asyncio.Task[None]":
    """Run `coro` as a task that survives cancellation of its caller (await it through asyncio.shield)
    and is waited for by close_all()/wait_for_pending_cleanups()."""
    task = asyncio.ensure_future(coro)
    task.set_name(name)
    _PENDING_CLEANUPS.add(task)
    task.add_done_callback(_PENDING_CLEANUPS.discard)
    return task


class CallEndedError(RuntimeError):
    """A stream tried to attach to a call whose hangup was already received."""


class StreamListener(Protocol):
    """Integration point for later milestones (e.g. a speech-to-text consumer)."""

    async def on_stream_start(self, session: "StreamSession") -> None: ...
    async def on_stream_end(self, session: "StreamSession", reason: str) -> None: ...
    async def on_dtmf(self, session: "StreamSession", event: DtmfEvent) -> None: ...


@dataclass
class StreamStats:
    frames_received: int = 0
    bytes_received: int = 0
    malformed: int = 0
    unexpected_stream: int = 0
    dtmf_events: int = 0
    played_stream_unmatched: int = 0
    last_sequence: int | None = None


@dataclass(eq=False)
class StreamSession:
    """One WebSocket connection (one streamId) belonging to a call."""

    call_id: str
    stream_id: str
    account_id: str | None
    tracks: tuple[str, ...]
    audio_format: AudioFormat  # inbound format announced by the start event
    queue: BoundedFrameQueue
    sender: VobizStreamSender
    playback: PlaybackController
    socket_closer: SocketCloser | None = None
    stats: StreamStats = field(default_factory=StreamStats)
    started_at: float = field(default_factory=time.time)
    end_reason: str | None = None
    consumer: InboundAudioConsumer | None = None  # owned by this session; see audio_consumer.py
    # "primary" = the legacy stream type (a newer one supersedes the older one). "monitor" = M5 monitored
    # stream: several may be live for one call and they never supersede the primary stream.
    kind: str = "primary"
    _released: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def closed(self) -> bool:
        return self.end_reason is not None

    async def wait_closed(self) -> None:
        """Wait until the cleanup started by close() has finished (consumer drained, sink finalized).

        Returns at once if the session was never closed. Callers should bound this with a timeout.
        """
        if self.closed:
            await self._released.wait()

    def start_consumer(self, sink_factory: AudioSinkFactory | None, **options: object) -> InboundAudioConsumer:
        """Create and start this stream's single consumer. A second call returns the same one."""
        if self.consumer is None:
            self.consumer = InboundAudioConsumer(self, sink_factory, **options)  # type: ignore[arg-type]
            if not self.closed:
                self.consumer.start()
        return self.consumer

    def consumer_metrics(self) -> dict[str, object]:
        if self.consumer is not None:
            return self.consumer.metrics()
        q = self.queue
        return {  # no consumer was ever created for this session (e.g. closed before start)
            "consumer_state": "unavailable", "consumer_active": False, "sink": None,
            "frames_received": self.stats.frames_received, "frames_enqueued": q.enqueued,
            "frames_processed": 0, "frames_dropped": q.dropped,
            "frames_discarded_on_close": q.discarded_on_close, "processing_errors": 0,
            "queue_depth": len(q), "queue_capacity": q.capacity, "queue_high_water": q.high_water,
            "consumer_latency_ms": None, "consumer_latency_avg_ms": None, "consumer_latency_max_ms": 0.0,
            "failure_reason": None,
        }

    async def close(self, reason: str, *, code: int = 1000, close_socket: bool = True) -> bool:
        """Release buffers and checkpoints, and optionally close the socket. Idempotent.

        Returns True only for the call that actually closed the session.
        """
        if self.end_reason is not None:
            return False
        self.end_reason = reason
        self.playback.close()  # also marks the sender closed: nothing is sent after this
        self.queue.seal()  # no new frames; already-buffered frames stay available for draining
        if close_socket and self.socket_closer is not None:  # close first: nothing more to receive
            try:
                await self.socket_closer(code, reason[:100])
            except Exception:  # socket already gone
                pass
        # The bounded cleanup runs in its own tracked task and is awaited through shield(): if the
        # caller is cancelled (server shutdown, a dropped handler), the cleanup still finishes, so
        # buffered audio is drained and the sink (e.g. STT final transcript) is finalized.
        cleanup = asyncio.create_task(self._release(reason), name=f"vobiz-stream-cleanup:{self.stream_id}")
        _PENDING_CLEANUPS.add(cleanup)
        cleanup.add_done_callback(_PENDING_CLEANUPS.discard)
        await asyncio.shield(cleanup)
        return True

    async def _release(self, reason: str) -> None:
        try:
            if self.consumer is not None:
                # bounded drain of buffered frames, then cancel + await the task, then close the sink
                # (for STT: flush, end, wait for the final transcript within its own budget)
                await self.consumer.stop(reason)
        finally:
            self.queue.close()  # discards (and counts) anything the drain could not deliver
            self._released.set()

    def snapshot(self) -> dict[str, object]:
        return {
            "call_id": self.call_id,
            "stream_id": self.stream_id,
            "kind": self.kind,
            "audio_format": self.audio_format.label,
            "tracks": list(self.tracks),
            "playback_state": self.playback.state.value,
            "pending_checkpoints": len(self.playback.pending_checkpoints),
            "closed": self.closed,
            "frames_received": self.stats.frames_received,
            "bytes_received": self.stats.bytes_received,
            "queue": self.queue.stats(),
            "consumer": self.consumer_metrics(),
            "stt": self.consumer.sink_metrics() if self.consumer is not None else None,
            "malformed": self.stats.malformed,
            "unexpected_stream": self.stats.unexpected_stream,
            "dtmf_events": self.stats.dtmf_events,
        }


@dataclass(eq=False)
class CallState:
    call_id: str
    created_at: float = field(default_factory=time.time)
    current: StreamSession | None = None
    streams_seen: int = 0  # PRIMARY streams only: `reconnects` keeps its legacy meaning
    last_activity: float = field(default_factory=time.monotonic)
    monitors: dict[str, StreamSession] = field(default_factory=dict)  # monitored streams by physical stream id
    monitors_seen: int = 0

    @property
    def reconnects(self) -> int:
        return max(0, self.streams_seen - 1)


class CallStateManager:
    def __init__(
        self,
        *,
        idle_ttl_s: float = 300.0,
        max_ended: int = 10_000,
        ended_ttl_s: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
        shutdown_cleanup_timeout_s: float = 10.0,
    ) -> None:
        self._calls: dict[str, CallState] = {}
        self._ended: OrderedDict[str, float] = OrderedDict()
        self._idle_ttl_s = idle_ttl_s
        self._max_ended = max_ended
        self._ended_ttl_s = ended_ttl_s
        self._clock = clock
        self._listeners: list[StreamListener] = []
        self._shutdown_cleanup_timeout_s = shutdown_cleanup_timeout_s
        self.cleanup_failures = 0  # stream cleanups that raised (each is isolated from the others)

    # --- queries ------------------------------------------------------------------------------

    def get(self, call_id: str) -> CallState | None:
        return self._calls.get(call_id)

    def current_stream(self, call_id: str) -> StreamSession | None:
        """The call's live PRIMARY stream (legacy meaning; monitored streams are not included)."""
        call = self._calls.get(call_id)
        return call.current if call and call.current and not call.current.closed else None

    def live_streams(self, call_id: str) -> list[StreamSession]:
        """Every live (not closed) physical stream of the call: the primary one, then monitored ones."""
        call = self._calls.get(call_id)
        if call is None:
            return []
        live = [call.current] if call.current is not None and not call.current.closed else []
        live.extend(s for s in call.monitors.values() if not s.closed)
        return live

    def live_stream_count(self, call_id: str) -> int:
        return len(self.live_streams(call_id))

    def metrics(self) -> dict[str, object]:
        """Counters only."""
        return {
            "calls": len(self._calls),
            "live_streams": sum(self.live_stream_count(c) for c in self._calls),
            "cleanup_failures": self.cleanup_failures,
        }

    def is_ended(self, call_id: str) -> bool:
        self._prune_ended()
        return call_id in self._ended

    def snapshot(self) -> list[dict[str, object]]:
        out = []
        for call in self._calls.values():
            stream = call.current.snapshot() if call.current else None
            out.append(
                {"call_id": call.call_id, "streams_seen": call.streams_seen,
                 "reconnects": call.reconnects, "current_stream": stream,
                 "active_streams": self.live_stream_count(call.call_id), "monitors_seen": call.monitors_seen,
                 "monitor_streams": [s.snapshot() for s in call.monitors.values()]}
            )
        return out

    def __len__(self) -> int:
        return len(self._calls)

    # --- listeners ----------------------------------------------------------------------------

    def add_listener(self, listener: StreamListener) -> None:
        self._listeners.append(listener)

    async def notify_dtmf(self, session: StreamSession, event: DtmfEvent) -> None:
        for listener in self._listeners:
            try:
                await listener.on_dtmf(session, event)
            except Exception as exc:
                logger.warning("vobiz_listener_failed", extra={"hook": "on_dtmf", "error": type(exc).__name__})

    async def _notify(self, hook: str, session: StreamSession, *args: object) -> None:
        for listener in self._listeners:
            try:
                await getattr(listener, hook)(session, *args)
            except Exception as exc:
                logger.warning("vobiz_listener_failed", extra={"hook": hook, "error": type(exc).__name__})

    # --- lifecycle ----------------------------------------------------------------------------

    async def attach_stream(self, session: StreamSession) -> CallState:
        """Make `session` the call's current stream, replacing (and closing) any previous one."""
        self._prune_ended()
        if session.call_id in self._ended:
            raise CallEndedError(session.call_id)
        await self._prune_idle()

        call = self._calls.get(session.call_id)  # no duplicate state: one CallState per callId
        if call is None:
            call = self._calls[session.call_id] = CallState(call_id=session.call_id)
        if session.kind == "monitor":
            return await self._attach_monitor(call, session)
        previous = call.current
        if previous is not None and previous is not session:
            # Retire the old stream BEFORE exposing the new one, so nothing ever sees two live streams.
            call.current = None
            logger.info(
                "vobiz_stream_superseded",
                extra={"call_id": call.call_id, "old_stream_id": previous.stream_id, "new_stream_id": session.stream_id},
            )
            await self._finish(previous, "superseded", code=1000)
            if session.call_id in self._ended:  # the hangup arrived while the old socket was closing
                raise CallEndedError(session.call_id)
        call.current = session
        call.streams_seen += 1
        call.last_activity = self._clock()
        await self._notify("on_stream_start", session)
        return call

    async def _attach_monitor(self, call: CallState, session: StreamSession) -> CallState:
        """Opt-in multi-stream: a monitored stream joins the call WITHOUT touching the primary stream or
        other monitored streams. Only a repeat attach of the same physical stream id replaces its older
        self (idempotent re-attach)."""
        previous = call.monitors.get(session.stream_id)
        if previous is not None and previous is not session:
            del call.monitors[session.stream_id]
            logger.info("vobiz_monitor_stream_superseded",
                        extra={"call_id": call.call_id, "stream_id": session.stream_id})
            await self._finish(previous, "superseded", code=1000)
            if session.call_id in self._ended:  # the hangup arrived while the old socket was closing
                raise CallEndedError(session.call_id)
        call.monitors[session.stream_id] = session
        call.monitors_seen += 1
        call.last_activity = self._clock()
        await self._notify("on_stream_start", session)
        return call

    async def detach_stream(self, session: StreamSession, reason: str) -> None:
        """The socket for `session` is gone (or being closed). Safe to call any number of times."""
        await self._finish(session, reason, close_socket=False)
        call = self._calls.get(session.call_id)
        if call is None:
            return
        if call.current is session:
            call.current = None
        elif call.monitors.get(session.stream_id) is session:
            del call.monitors[session.stream_id]
        else:
            return
        call.last_activity = self._clock()
        if self.is_ended(session.call_id) and call.current is None and not call.monitors:
            self._calls.pop(session.call_id, None)

    async def close_all(self, reason: str = "shutdown") -> int:
        """Graceful shutdown: close every live stream (primary and monitored). Sessions finalize
        concurrently (each one is bounded by its own drain and sink budgets), so shutdown time does not
        grow with call count, and one stream's failing cleanup never stops the others."""
        sessions = [s for c in self._calls for s in self.live_streams(c)]
        results = await asyncio.gather(
            *(self._finish(session, reason, code=1001) for session in sessions), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                self.cleanup_failures += 1
                logger.warning("vobiz_stream_shutdown_error", extra={"error": type(result).__name__})
        # Cleanups started elsewhere (e.g. by a handler that was cancelled) get a bounded chance to finish.
        await wait_for_pending_cleanups(self._shutdown_cleanup_timeout_s)
        return len(sessions)

    async def on_hangup(self, call_id: str) -> bool:
        """The call ended (hangup webhook). Closes its stream if the socket is still open.

        Returns True if there was active state to clean up. Safe if the socket already closed, if the
        call has no stream, if the call was never seen here, and if called repeatedly.
        """
        self._mark_ended(call_id)
        call = self._calls.pop(call_id, None)
        sessions: list[StreamSession] = []
        if call is not None:
            if call.current is not None:
                sessions.append(call.current)
                call.current = None
            sessions.extend(call.monitors.values())  # the call is over: every monitored stream goes too
            call.monitors = {}
        if sessions:
            # Concurrent and isolated: one stream's failing cleanup must not stop the others.
            results = await asyncio.gather(*(self._close_for_hangup(call_id, s) for s in sessions),
                                           return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    self.cleanup_failures += 1
                    logger.warning("vobiz_hangup_cleanup_failed",
                                   extra={"call_id": call_id, "error": type(result).__name__})
        await self._notify_call_ended(call_id, "hangup")
        return call is not None

    async def _close_for_hangup(self, call_id: str, session: StreamSession) -> None:
        await self._finish(session, "hangup", code=1000)
        # The stream's cleanup may already be running (socket closed first): wait for it, bounded,
        # so the final transcripts it produces are accepted before the call's transcripts close.
        try:
            await asyncio.wait_for(session.wait_closed(), self._shutdown_cleanup_timeout_s)
        except asyncio.TimeoutError:
            logger.warning("vobiz_hangup_cleanup_timeout", extra={"call_id": call_id})

    async def _notify_call_ended(self, call_id: str, reason: str) -> None:
        """Optional listener hook `on_call_ended(call_id, reason)`: the call is over (even if it never
        had a stream here)."""
        for listener in self._listeners:
            hook = getattr(listener, "on_call_ended", None)
            if hook is None:
                continue
            try:
                await hook(call_id, reason)
            except Exception as exc:
                logger.warning("vobiz_listener_failed", extra={"hook": "on_call_ended", "error": type(exc).__name__})

    # --- internals ----------------------------------------------------------------------------

    async def _finish(self, session: StreamSession, reason: str, *, code: int = 1000, close_socket: bool = True) -> None:
        closed_now = await session.close(reason, code=code, close_socket=close_socket)
        if closed_now:
            await self._notify("on_stream_end", session, reason)

    def _mark_ended(self, call_id: str) -> None:
        self._ended[call_id] = self._clock()
        self._ended.move_to_end(call_id)
        self._prune_ended()

    def _prune_ended(self) -> None:
        now = self._clock()
        while self._ended:
            oldest, at = next(iter(self._ended.items()))
            if now - at > self._ended_ttl_s or len(self._ended) > self._max_ended:
                del self._ended[oldest]
            else:
                break

    async def _prune_idle(self) -> None:
        """Forget calls whose stream is gone and that saw no activity for a while (hangup never came)."""
        now = self._clock()
        for call_id, call in list(self._calls.items()):
            if (call.current is None and not any(not s.closed for s in call.monitors.values())
                    and now - call.last_activity > self._idle_ttl_s):
                del self._calls[call_id]
