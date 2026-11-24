"""M5: per-track routing for a MONITORED physical Vobiz stream.

A monitored stream may carry two provider tracks, tagged `inbound` and `outbound` on each media
frame. This module splits them into independent processing paths:

    physical stream queue -> InboundAudioConsumer -> TrackRouterSink.handle_frame   (one per stream)
        -> classify track (inbound/outbound only)
        -> per-track BoundedFrameQueue -> per-track InboundAudioConsumer -> per-track sink
                                                                         (e.g. its own Sarvam session)

Design decisions (see the M5 ADRs):

* One router per physical stream; a call with several monitored streams has one router each, so one
  stream's lifecycle never touches another's. The call-level sharing lives in the transcript session.
* `handle_frame` NEVER awaits track processing: it classifies the frame and puts it on a bounded
  per-track queue. A stalled or failing track therefore cannot delay the other track, the stream's
  own consumer, or the WebSocket receive loop.
* Workers reuse `InboundAudioConsumer` through `TrackSessionView` (it only needs call_id, stream_id,
  audio_format, queue and stats from its "session"). This keeps the proven drain, error-budget,
  cancellation and metrics logic instead of duplicating it, and gives every track its own queue,
  task, sink, error budget, state and cleanup.
* Workers are created lazily when the first VALID frame of a track arrives.
* Labels are neutral: provider `inbound` -> `track_inbound`, `outbound` -> `track_outbound`. Nothing
  here says which participant a track carries (unverified provider behaviour).
* Nothing is dropped silently. Every lost frame is counted: queue overflow (`frames_dropped`),
  rejected tracks (by reason), frames offered to a failed/ended/closed track, and frames discarded at
  shutdown (`frames_discarded_on_close`).
* Logs and metrics carry ids, states, counters and error types only: never audio or transcript text.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.services.audio.audio_buffer import AudioFrame, BoundedFrameQueue
from app.services.audio.formats import AudioFormat
from app.services.voice.audio_consumer import (
    DEFAULT_MAX_CONSECUTIVE_ERRORS,
    FAILED,
    UNAVAILABLE,
    AudioFrameSink,
    InboundAudioConsumer,
)
from app.services.voice.call_manager import spawn_tracked

logger = logging.getLogger(__name__)

TRACK_INBOUND = "track_inbound"
TRACK_OUTBOUND = "track_outbound"
# The ONLY provider tag values that are recognized, mapped to neutral internal labels.
PROVIDER_TRACK_LABELS: dict[str, str] = {"inbound": TRACK_INBOUND, "outbound": TRACK_OUTBOUND}
RECOGNIZED_TRACKS = frozenset(PROVIDER_TRACK_LABELS.values())

# Rejection reasons (also the keys of the `frames_rejected` metric).
REJECT_UNKNOWN = "unknown_track"
REJECT_MISSING = "missing_track"
REJECT_EMPTY = "empty_track"
REJECT_TRACK_FAILED = "track_failed"  # the track's sink never started or gave up
REJECT_TRACK_ENDED = "track_ended"
REJECT_ROUTER_CLOSED = "router_closed"
REJECT_REASONS = (
    REJECT_UNKNOWN, REJECT_MISSING, REJECT_EMPTY, REJECT_TRACK_FAILED, REJECT_TRACK_ENDED, REJECT_ROUTER_CLOSED,
)

CLOSE_MARGIN_S = 1.0  # time on top of the children's own budgets for the router's own bookkeeping
LOG_REJECT_EVERY = 100  # log the 1st rejection of each reason, then every 100th (bounded log volume)

# Creates the sink for one track. May be sync or async; returning None means "no sink for this track".
TrackSinkFactory = Callable[["TrackSessionView", str], "AudioFrameSink | None | Awaitable[AudioFrameSink | None]"]


def classify_track(raw: object) -> tuple[str | None, str | None]:
    """Return (neutral_label, None) for a recognized provider tag, else (None, rejection_reason).

    Exact, case-sensitive match on the documented tags; no normalization or guessing.
    """
    if raw is None or not isinstance(raw, str):
        return None, REJECT_MISSING
    if not raw.strip():
        return None, REJECT_EMPTY
    label = PROVIDER_TRACK_LABELS.get(raw)
    return (label, None) if label is not None else (None, REJECT_UNKNOWN)


@dataclass
class _TrackStats:
    frames_received: int = 0


@dataclass(eq=False)
class TrackSessionView:
    """What a per-track consumer and sink see instead of a StreamSession.

    `stream_id` is always the PHYSICAL Vobiz stream id; `track` is the neutral label. Sinks must not
    derive a stream id from the track.
    """

    call_id: str
    stream_id: str
    account_id: str | None
    audio_format: AudioFormat
    track: str
    queue: BoundedFrameQueue
    stats: _TrackStats = field(default_factory=_TrackStats)
    tracks: tuple[str, ...] = ()


class TrackWorker:
    """Independent queue + consumer task + sink for one track of one physical stream."""

    def __init__(
        self,
        parent: Any,
        track: str,
        sink_factory: TrackSinkFactory,
        *,
        queue_frames: int,
        drain_timeout_s: float,
        max_consecutive_errors: int,
    ) -> None:
        self.track = track
        self.view = TrackSessionView(
            call_id=parent.call_id, stream_id=parent.stream_id, account_id=getattr(parent, "account_id", None),
            audio_format=parent.audio_format, track=track, queue=BoundedFrameQueue(queue_frames), tracks=(track,),
        )
        self.consumer = InboundAudioConsumer(
            self.view,
            lambda view: sink_factory(view, track),
            drain_timeout_s=drain_timeout_s,
            max_consecutive_errors=max_consecutive_errors,
        )
        self.ended = False
        self.end_reason: str | None = None
        self.close_ms: float | None = None
        self._close_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.consumer.start()

    @property
    def unavailable(self) -> bool:
        return self.consumer.state in (FAILED, UNAVAILABLE)

    def offer(self, frame: AudioFrame) -> bool:
        """Queue a frame. Never blocks. Overflow drops the oldest frame and counts it in the queue."""
        self.view.stats.frames_received += 1
        return self.view.queue.put(frame)

    async def close(self, reason: str) -> None:
        """Seal the queue (buffered frames get the consumer's bounded drain), stop the consumer,
        finalize the sink, release the queue. Idempotent; runs in a tracked task so cancelling the
        caller does not abandon the cleanup."""
        if self._close_task is None:
            self.ended, self.end_reason = True, reason
            self._close_task = spawn_tracked(self._do_close(reason), f"track-worker-close:{self.view.stream_id}:{self.track}")
        await asyncio.shield(self._close_task)

    async def _do_close(self, reason: str) -> None:
        started = time.monotonic()
        self.view.queue.seal()
        try:
            await self.consumer.stop(reason)
        finally:
            self.view.queue.close()  # discards (and counts) whatever the drain could not deliver
            self.close_ms = round((time.monotonic() - started) * 1000, 1)

    def metrics(self) -> dict[str, object]:
        m = self.consumer.metrics()
        return {
            "track": self.track,
            "ended": self.ended,
            "end_reason": self.end_reason,
            "close_ms": self.close_ms,
            "consumer": m,
            "sink": self.consumer.sink_metrics(),
        }


class TrackRouterSink:
    """AudioFrameSink for ONE monitored physical stream; dispatches frames to per-track workers."""

    def __init__(
        self,
        session: Any,
        sink_factory: TrackSinkFactory,
        *,
        queue_frames: int,
        drain_timeout_s: float = 0.5,
        child_close_timeout_s: float = 3.0,
        max_consecutive_errors: int = DEFAULT_MAX_CONSECUTIVE_ERRORS,
    ) -> None:
        if queue_frames < 1:
            raise ValueError("queue_frames must be >= 1")
        self._session = session
        self._factory = sink_factory
        self._queue_frames = queue_frames
        self._drain_timeout_s = drain_timeout_s
        self._max_errors = max_consecutive_errors
        self._workers: dict[str, TrackWorker] = {}
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self.state = "open"
        self.frames_routed: dict[str, int] = {}
        self.frames_rejected: dict[str, int] = {reason: 0 for reason in REJECT_REASONS}
        self.track_close_failures = 0
        self.close_ms: float | None = None
        # The consumer that owns this sink waits at most this long for close(): children close
        # concurrently, so the budget is ONE child's drain + finalize, not their sum.
        self.close_timeout_s = drain_timeout_s + child_close_timeout_s + CLOSE_MARGIN_S

    @property
    def _ids(self) -> dict[str, object]:
        return {"call_id": self._session.call_id, "stream_id": self._session.stream_id}

    # --- AudioFrameSink -----------------------------------------------------------------------

    async def handle_frame(self, frame: AudioFrame) -> None:
        """Dispatch one frame. Never awaits any track's processing; never raises for track faults."""
        if self._closed:
            self._reject(REJECT_ROUTER_CLOSED, frame)
            return
        label, reason = classify_track(frame.track)
        if label is None:
            self._reject(reason or REJECT_UNKNOWN, frame)
            return
        worker = self._workers.get(label) or self._start_worker(label)
        if worker.ended:
            self._reject(REJECT_TRACK_ENDED, frame, label)
            return
        if worker.unavailable:
            self._reject(REJECT_TRACK_FAILED, frame, label)
            return
        worker.offer(frame)  # overflow is counted by the track queue itself
        self.frames_routed[label] = self.frames_routed.get(label, 0) + 1

    async def close(self, reason: str) -> None:
        """Close every track concurrently. Idempotent; survives cancellation of the caller."""
        if self._close_task is None:
            self._closed = True
            self.state = "closing"
            self._close_task = spawn_tracked(self._do_close(reason), f"track-router-close:{self._session.stream_id}")
        await asyncio.shield(self._close_task)

    # --- per-track control --------------------------------------------------------------------

    def worker(self, track: str) -> TrackWorker | None:
        return self._workers.get(track)

    async def end_track(self, track: str, reason: str = "track_ended") -> bool:
        """End ONE track (finalize its sink) without touching the other track or the stream.
        Idempotent. Returns False if the track has no worker. Later frames for it are rejected+counted."""
        worker = self._workers.get(track)
        if worker is None:
            return False
        await worker.close(reason)
        return True

    def _start_worker(self, label: str) -> TrackWorker:
        worker = TrackWorker(
            self._session, label, self._factory, queue_frames=self._queue_frames,
            drain_timeout_s=self._drain_timeout_s, max_consecutive_errors=self._max_errors,
        )
        self._workers[label] = worker  # registered before start: a repeat registration reuses it
        worker.start()
        logger.info("vobiz_track_worker_started", extra={**self._ids, "track": label,
                                                         "queue_capacity": self._queue_frames})
        return worker

    def _reject(self, reason: str, frame: AudioFrame, track: str | None = None) -> None:
        self.frames_rejected[reason] += 1
        n = self.frames_rejected[reason]
        if n == 1 or n % LOG_REJECT_EVERY == 0:
            raw = frame.track if isinstance(frame.track, str) else None
            logger.warning(
                "vobiz_track_frame_rejected",
                extra={**self._ids, "reason": reason, "track": track,
                       "provider_track": raw[:32] if raw is not None else None, "count": n},
            )

    async def _do_close(self, reason: str) -> None:
        started = time.monotonic()
        workers = list(self._workers.values())
        results = await asyncio.gather(*(w.close(reason) for w in workers), return_exceptions=True)
        for worker, result in zip(workers, results):
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                self.track_close_failures += 1
                logger.error(
                    "vobiz_track_worker_close_failed",
                    extra={**self._ids, "track": worker.track, "error": type(result).__name__},
                )
        self.close_ms = round((time.monotonic() - started) * 1000, 1)
        self.state = "closed"
        logger.info("vobiz_track_router_closed", extra={**self._ids, "reason": reason, **self._summary()})

    # --- observability ------------------------------------------------------------------------

    def _summary(self) -> dict[str, object]:
        return {
            "active_tracks": sum(1 for w in self._workers.values() if not w.ended and w.consumer.active),
            "tracks_started": len(self._workers),
            "frames_rejected_total": sum(self.frames_rejected.values()),
            "track_close_failures": self.track_close_failures,
        }

    def metrics(self) -> dict[str, object]:
        """Counters and states only: no audio, no transcript text, no ids beyond call/stream."""
        return {
            "router_state": self.state,
            **self._summary(),
            "frames_routed": dict(self.frames_routed),
            "frames_rejected": dict(self.frames_rejected),
            "close_ms": self.close_ms,
            "tracks": {label: w.metrics() for label, w in self._workers.items()},
        }


class TrackRoutingSinkFactory:
    """AudioSinkFactory for monitored streams: returns one `TrackRouterSink` per physical stream.

    `track_sink_factory(view, track)` builds the per-track sink (e.g. `SarvamSttSinkFactory
    .create_for_track`). The legacy single-sink factory is NOT touched by this class.
    """

    def __init__(
        self,
        track_sink_factory: TrackSinkFactory,
        *,
        queue_frames: int,
        drain_timeout_s: float = 0.5,
        child_close_timeout_s: float = 3.0,
        max_consecutive_errors: int = DEFAULT_MAX_CONSECUTIVE_ERRORS,
    ) -> None:
        self._track_sink_factory = track_sink_factory
        self._queue_frames = queue_frames
        self._drain_timeout_s = drain_timeout_s
        self._child_close_timeout_s = child_close_timeout_s
        self._max_errors = max_consecutive_errors

    def __call__(self, session: Any) -> TrackRouterSink:
        return TrackRouterSink(
            session, self._track_sink_factory, queue_frames=self._queue_frames,
            drain_timeout_s=self._drain_timeout_s, child_close_timeout_s=self._child_close_timeout_s,
            max_consecutive_errors=self._max_errors,
        )
