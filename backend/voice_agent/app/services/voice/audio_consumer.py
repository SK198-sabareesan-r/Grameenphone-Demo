"""Continuous consumer for one Vobiz stream's inbound audio queue.

    WebSocket receive loop --put()--> BoundedFrameQueue (per stream) --get()--> consumer task --await--> sink

* One consumer per stream, owned by its StreamSession. It reads only that stream's queue, so one
  call can never consume another call's frames, and a reconnect (new stream) gets a fresh consumer.
* Frames are handed to the sink one at a time, in arrival order, and the sink is AWAITED: a slow
  sink slows the consumer, never the WebSocket receive loop.
* There is no second queue. The only buffer is the existing bounded per-stream queue.

Backpressure policy (see also BoundedFrameQueue):
* consumer keeps pace   -> queue depth stays near 0; latency = sink time.
* consumer slower       -> depth grows; `vobiz_audio_consumer_lagging` is logged at half capacity.
* queue full            -> the OLDEST frame is dropped (bounded latency, bounded memory); every
                           drop is counted (`frames_dropped`) and logged by the stream handler.
  The receive loop is never blocked: it must keep reading playedStream/clearedAudio/control events,
  and Vobiz documents no flow control for media.
* sink raises           -> that frame is counted as failed (not processed) and NOT retried; the next
                           frame is processed. After `max_consecutive_errors` failures in a row the
                           consumer stops (state "failed"); later frames then overflow visibly.
* stream ends           -> the queue is SEALED (no new frames); frames already buffered get up to
                           `drain_timeout_s` (VOBIZ_AUDIO_DRAIN_TIMEOUT_S, default 0.5 s) to reach
                           the sink; then the task is cancelled and awaited and the sink is closed
                           within its budget (a sink may declare a longer `close_timeout_s`, e.g. STT
                           finalization). Frames that could not drain are discarded and counted
                           (`discarded_on_close`). The whole cleanup is shielded from cancellation
                           of its caller (see StreamSession.close).

No sink registered: no task is started, the state is "unavailable", and nothing is ever reported as
processed. Frames stay buffered and overflow exactly as before (diagnostics unchanged).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from app.services.audio.audio_buffer import AudioFrame, BoundedFrameQueue

if TYPE_CHECKING:
    from app.services.voice.call_manager import StreamSession

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONSECUTIVE_ERRORS = 50  # 50 x 20 ms = 1 s of audio failing in a row
DEFAULT_SINK_CLOSE_TIMEOUT_S = 1.0  # sinks may declare a longer `close_timeout_s` (bounded)
DEFAULT_DRAIN_TIMEOUT_S = 0.5  # time already-buffered frames get to reach the sink at stream end
DEFAULT_SAMPLE_EVERY = 250  # one DEBUG progress line per 250 frames (5 s of 20 ms frames)
ERROR_LOG_EVERY = 100

# States
UNAVAILABLE = "unavailable"  # no sink registered
STARTING = "starting"  # task running, sink being created
RUNNING = "running"
FAILED = "failed"  # sink factory failed, or too many consecutive sink errors
STOPPED = "stopped"


@runtime_checkable
class AudioFrameSink(Protocol):
    """Receives one stream's inbound frames, in order. Implemented by M4 (e.g. Sarvam realtime STT).

    handle_frame() is awaited before the next frame is delivered; take as long as you need, but
    the per-stream queue is bounded, so a sink slower than real time will cause counted drops.
    Frames carry raw bytes exactly as received plus their format metadata.
    """

    async def handle_frame(self, frame: AudioFrame) -> None: ...

    async def close(self, reason: str) -> None: ...


# Creates one sink per stream. May be sync or async. Returning None means "no sink for this stream"
# (state "unavailable"); raising marks the consumer "failed".
AudioSinkFactory = Callable[["StreamSession"], "AudioFrameSink | Awaitable[AudioFrameSink]"]


class InboundAudioConsumer:
    def __init__(
        self,
        session: "StreamSession",
        sink_factory: AudioSinkFactory | None,
        *,
        max_consecutive_errors: int = DEFAULT_MAX_CONSECUTIVE_ERRORS,
        sink_close_timeout_s: float = DEFAULT_SINK_CLOSE_TIMEOUT_S,
        drain_timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S,
        sample_every: int = DEFAULT_SAMPLE_EVERY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_consecutive_errors < 1:
            raise ValueError("max_consecutive_errors must be >= 1")
        self._session = session
        self._queue: BoundedFrameQueue = session.queue
        self._factory = sink_factory
        self._max_consecutive_errors = max_consecutive_errors
        self._sink_close_timeout_s = sink_close_timeout_s
        self._drain_timeout_s = max(0.0, drain_timeout_s)
        self.drain_ms: float | None = None
        self.drained_completely: bool | None = None
        self.frames_buffered_at_stop: int | None = None
        self._sample_every = max(1, sample_every)
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._sink: AudioFrameSink | None = None
        self._stopped = False
        self.state = STARTING if sink_factory is not None else UNAVAILABLE
        self.failure_reason: str | None = None
        self.stop_reason: str | None = None
        # metrics
        self.frames_processed = 0
        self.processing_errors = 0
        self.consecutive_errors = 0
        self.latency_last_ms: float | None = None
        self.latency_max_ms: float = 0.0
        self._latency_sum_ms = 0.0
        self._lagging = False

    # --- identity / state ---------------------------------------------------------------------

    @property
    def _ids(self) -> dict[str, object]:
        ids: dict[str, object] = {"call_id": self._session.call_id, "stream_id": self._session.stream_id}
        track = getattr(self._session, "track", None)  # set only by the M5 per-track session view
        if track is not None:
            ids["track"] = track
        return ids

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    @property
    def sink_name(self) -> str | None:
        return type(self._sink).__name__ if self._sink is not None else None

    def metrics(self) -> dict[str, object]:
        q = self._queue
        avg = self._latency_sum_ms / self.frames_processed if self.frames_processed else None
        return {
            "consumer_state": self.state,
            "consumer_active": self.active,
            "sink": self.sink_name,
            "frames_received": self._session.stats.frames_received,
            "frames_enqueued": q.enqueued,
            "frames_processed": self.frames_processed,
            "frames_dropped": q.dropped,
            "frames_discarded_on_close": q.discarded_on_close,
            "processing_errors": self.processing_errors,
            "queue_depth": len(q),
            "queue_capacity": q.capacity,
            "queue_high_water": q.high_water,
            "consumer_latency_ms": round(self.latency_last_ms, 2) if self.latency_last_ms is not None else None,
            "consumer_latency_avg_ms": round(avg, 2) if avg is not None else None,
            "consumer_latency_max_ms": round(self.latency_max_ms, 2),
            "failure_reason": self.failure_reason,
            "drain_ms": self.drain_ms,
            "drained_completely": self.drained_completely,
            "frames_buffered_at_stop": self.frames_buffered_at_stop,
        }

    def sink_metrics(self) -> dict[str, object] | None:
        """The sink's own metrics, if it exposes metrics() (e.g. the Sarvam STT sink)."""
        metrics = getattr(self._sink, "metrics", None)
        if not callable(metrics):
            return None
        try:
            return metrics()
        except Exception:  # diagnostics must never fail
            return None

    # --- lifecycle ----------------------------------------------------------------------------

    def start(self) -> None:
        """Start the consumer task, or report that no sink is registered. Call once per stream."""
        if self._task is not None or self._stopped:
            return  # never more than one task per stream
        if self._factory is None:
            logger.warning(
                "vobiz_audio_consumer_unavailable",
                extra={**self._ids, "reason": "no_sink_registered", "queue_capacity": self._queue.capacity},
            )
            return
        self._task = asyncio.create_task(self._run(), name=f"vobiz-audio-consumer:{self._session.stream_id}")

    async def stop(self, reason: str) -> None:
        """Drain (bounded), cancel and await the task, then close the sink. Idempotent; never raises
        except to propagate cancellation of the caller (after cleanup has run).

        If the stream's queue has been sealed, frames already buffered get up to `drain_timeout_s`
        to reach the sink. Whatever is still buffered after that is discarded and counted by the
        queue (`discarded_on_close`).
        """
        if self._stopped:
            return
        self._stopped = True
        self.stop_reason = reason
        cancelled_while_stopping = False
        task = self._task
        if task is not None and task is not asyncio.current_task():
            if not task.done() and self._drain_timeout_s > 0 and self._queue.sealed and self.state == RUNNING:
                depth = len(self._queue)
                started = self._clock()
                try:
                    await asyncio.wait({task}, timeout=self._drain_timeout_s)
                except asyncio.CancelledError:
                    cancelled_while_stopping = True
                self.drain_ms = round((self._clock() - started) * 1000, 1)
                self.drained_completely = task.done()
                self.frames_buffered_at_stop = depth
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    cancelled_while_stopping = True
            except Exception:  # the task already handled and logged its own errors
                pass
        try:
            await self._close_sink(reason)
        except asyncio.CancelledError:
            cancelled_while_stopping = True
        if self.state not in (FAILED, UNAVAILABLE):
            self.state = STOPPED
        if self._factory is not None:
            logger.info("vobiz_audio_consumer_stopped", extra={**self._ids, "reason": reason, **self.metrics()})
        if cancelled_while_stopping:
            raise asyncio.CancelledError  # cleanup ran; now honour the caller's cancellation

    async def _close_sink(self, reason: str) -> None:
        sink = self._sink
        if sink is None:
            return
        # A sink that needs longer to finalize (e.g. STT waiting for its last transcript) declares
        # its own bounded budget; otherwise the consumer default applies.
        timeout = getattr(sink, "close_timeout_s", None) or self._sink_close_timeout_s
        try:
            await asyncio.wait_for(sink.close(reason), timeout)
        except asyncio.TimeoutError:
            logger.warning("vobiz_audio_consumer_error", extra={**self._ids, "phase": "sink_close", "error": "timeout"})
        except Exception as exc:
            logger.warning(
                "vobiz_audio_consumer_error",
                extra={**self._ids, "phase": "sink_close", "error": type(exc).__name__},
            )

    # --- task ---------------------------------------------------------------------------------

    async def _run(self) -> None:
        try:
            created = self._factory(self._session)  # type: ignore[misc]
            sink = await created if inspect.isawaitable(created) else created
            if sink is None:  # the factory declined this stream (e.g. not a registered test call)
                self.state = UNAVAILABLE
                logger.info("vobiz_audio_consumer_unavailable", extra={**self._ids, "reason": "no_sink_for_stream"})
                return
            if not isinstance(sink, AudioFrameSink):
                raise TypeError("sink factory did not return an AudioFrameSink")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.state, self.failure_reason = FAILED, "sink_factory_failed"
            logger.error(
                "vobiz_audio_consumer_unavailable",
                extra={**self._ids, "reason": "sink_factory_failed", "error": type(exc).__name__},
            )
            return

        self._sink = sink
        self.state = RUNNING
        logger.info(
            "vobiz_audio_consumer_started",
            extra={
                **self._ids,
                "sink": type(sink).__name__,
                "audio_format": self._session.audio_format.label,
                "queue_capacity": self._queue.capacity,
                "queue_depth": len(self._queue),
            },
        )

        while True:
            frame = await self._queue.get()  # event-driven; returns None once the queue is closed
            if frame is None:
                return
            await self._process(frame, sink)
            if self.consecutive_errors >= self._max_consecutive_errors:
                self.state, self.failure_reason = FAILED, "too_many_consecutive_errors"
                logger.error(
                    "vobiz_audio_consumer_error",
                    extra={
                        **self._ids,
                        "phase": "giving_up",
                        "consecutive_errors": self.consecutive_errors,
                        "processing_errors": self.processing_errors,
                    },
                )
                return

    async def _process(self, frame: AudioFrame, sink: AudioFrameSink) -> None:
        self._check_lag()
        try:
            await sink.handle_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # recorded, never retried, never fatal to the server
            self.processing_errors += 1
            self.consecutive_errors += 1
            if self.processing_errors == 1 or self.processing_errors % ERROR_LOG_EVERY == 0:
                logger.warning(
                    "vobiz_audio_consumer_error",
                    extra={
                        **self._ids,
                        "phase": "handle_frame",
                        "error": type(exc).__name__,  # type only: messages could contain data
                        "processing_errors": self.processing_errors,
                        "chunk": frame.chunk,
                    },
                )
            return

        self.consecutive_errors = 0
        self.frames_processed += 1
        if frame.received_at:
            latency = (self._clock() - frame.received_at) * 1000
            self.latency_last_ms = latency
            self.latency_max_ms = max(self.latency_max_ms, latency)
            self._latency_sum_ms += latency
        if self.frames_processed % self._sample_every == 0 and logger.isEnabledFor(logging.DEBUG):
            logger.debug("vobiz_audio_frame_processed", extra={**self._ids, **self.metrics()})

    def _check_lag(self) -> None:
        """Warn once when the backlog reaches half capacity; re-arm below a quarter (hysteresis)."""
        depth, capacity = len(self._queue), self._queue.capacity
        if not self._lagging and depth >= capacity // 2:
            self._lagging = True
            logger.warning(
                "vobiz_audio_consumer_lagging",
                extra={**self._ids, "queue_depth": depth, "queue_capacity": capacity,
                       "frames_dropped": self._queue.dropped},
            )
        elif self._lagging and depth < capacity // 4:
            self._lagging = False
