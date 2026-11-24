"""Internal transcript event pipeline (M4.4).

`SarvamSttSink` publishes `TranscriptEvent`s to a `TranscriptPublisher`. `TranscriptEventPipeline` is
that publisher. It fans each call's events out to any number of internal subscribers, with per-call
ordering, per-call isolation, bounded queues and explicit backpressure. It is in-process only: no
broker, no database, no public endpoint.

Guarantees and rules (each is covered by tests/test_transcript_pipeline.py):

* `publish()` never awaits anything. It only appends to bounded in-memory queues, so it cannot block
  the Sarvam receive loop. Subscriber callbacks run in their own worker tasks, never in that loop.
* Ordering: events reach each subscriber in the order they were accepted for that call. The pipeline
  does NOT create a sequence number. It reuses `TranscriptEvent.sequence` (per stream, assigned by the
  sink) to detect anomalies, and it never reorders events:
    - duplicate (same stream and sequence seen before): not delivered, counted, logged (metadata);
      it is a retransmission, not a new event;
    - out of order (lower than one already seen, not a duplicate): delivered as received, counted;
    - gap (sequence jumps forward): delivered, counted.
  Duplicate detection looks at a sliding window of the last 1024 sequences per stream.
* Overflow (a subscriber's queue is full), in order of preference:
    1. drop the OLDEST queued partial (partials are superseded by the next partial or final);
    2. a non-partial event (final, VAD, session, error) may use `final_reserve` extra slots;
    3. a partial arriving when no queued partial can be evicted is dropped;
    4. a non-partial event that still does not fit is rejected, counted in `finals_undeliverable` /
       `others_undeliverable`, and logged at ERROR (metadata only, never text).
  Partials are therefore dropped, not merged: no coalescing is done, text is never altered.
* A slow or failing subscriber only affects its own queue. Exceptions and timeouts are counted.
* Late events (after a call's session closed) are rejected, counted and logged. Closed calls are
  remembered (bounded, with TTL) so a late event cannot reopen a session.
* No replay: a subscriber added mid-call only receives events accepted after it subscribed.
* Closing a call stops acceptance, appends a `call.closed` marker to every subscriber queue, waits up
  to `drain_timeout_s`, then cancels workers still running and records what was left undelivered.

Subscriber callbacks must be quick. Sync callables are called directly on the event loop.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from app.publishers.transcripts import TRANSCRIPT_FINAL, TranscriptEvent, TranscriptPublisher

logger = logging.getLogger(__name__)

CALL_CLOSED = "call.closed"
SEQUENCE_WINDOW = 1024

# Subscription states
ACTIVE, CLOSING, CLOSED = "active", "closing", "closed"
# Session states
OPEN = "open"


class PipelineError(RuntimeError):
    pass


class CallClosedError(PipelineError):
    """The call's transcript session is closed (or closing); no new subscriptions are possible."""


class SubscriberLimitError(PipelineError):
    pass


@dataclass(frozen=True)
class CallClosedEvent:
    """Lifecycle marker delivered last to every subscriber of a call (type `call.closed`)."""

    call_id: str
    reason: str
    closed_at: str
    events_accepted: int
    finals_accepted: int
    type: str = CALL_CLOSED


@dataclass(frozen=True)
class CallCloseReport:
    call_id: str
    reason: str
    drain_duration_s: float
    drain_timed_out: bool
    subscribers: int
    undelivered_events: int
    undelivered_finals: int


@runtime_checkable
class TranscriptSubscriber(Protocol):
    async def on_event(self, event: TranscriptEvent) -> None: ...


# `on_call_closed(CallClosedEvent)` is optional on a subscriber.
SubscriberCallable = Callable[[TranscriptEvent], "Awaitable[None] | None"]


class _CallableSubscriber:
    def __init__(self, fn: SubscriberCallable) -> None:
        self._fn = fn

    async def on_event(self, event: TranscriptEvent) -> None:
        result = self._fn(event)
        if inspect.isawaitable(result):
            await result


class PublisherSubscriber:
    """Adapts an existing `TranscriptPublisher` (e.g. LoggingTranscriptPublisher) to a subscriber."""

    def __init__(self, publisher: TranscriptPublisher) -> None:
        self._publisher = publisher

    async def on_event(self, event: TranscriptEvent) -> None:
        await self._publisher.publish(event)


def _as_subscriber(obj: Any) -> Any:
    if hasattr(obj, "on_event"):
        return obj
    if callable(obj):
        return _CallableSubscriber(obj)
    raise TypeError("subscriber must have on_event() or be callable")


def _is_final(item: Any) -> bool:
    return isinstance(item, TranscriptEvent) and item.type == TRANSCRIPT_FINAL


class TranscriptSubscription:
    """One subscriber's bounded queue and worker task for one call. Also the caller's handle."""

    def __init__(
        self, *, call_id: str, name: str, subscriber: Any, capacity: int, reserve: int,
        timeout_s: float, on_close: Callable[["TranscriptSubscription"], None] | None = None,
    ) -> None:
        self.call_id = call_id
        self.name = name
        self._subscriber = subscriber
        self._capacity = max(1, capacity)
        self._reserve = max(0, reserve)
        self._timeout_s = timeout_s
        self._on_close = on_close
        self._queue: deque[Any] = deque()
        self._wake = asyncio.Event()
        self._inflight: Any = None
        self._interrupted: Any = None
        self.state = ACTIVE
        self.delivered = 0
        self.failures = 0
        self.timeouts = 0
        self.partials_dropped = 0
        self.finals_undeliverable = 0
        self.others_undeliverable = 0
        self.undelivered = 0
        self.undelivered_finals = 0
        self.high_water = 0
        self._cb_last_ms: float | None = None
        self._cb_max_ms = 0.0
        self._cb_total_ms = 0.0
        self._cb_count = 0
        self.task: asyncio.Task[None] = asyncio.create_task(self._run(), name=f"transcript-sub:{name}:{call_id}")

    # --- producer side (synchronous, never blocks) --------------------------------------------

    def offer(self, event: TranscriptEvent) -> bool:
        """Queue `event` if the overflow policy allows it. Returns True if it was queued."""
        if self.state != ACTIVE:
            return False
        q = self._queue
        if len(q) >= self._capacity:
            if self._evict_oldest_partial():
                pass
            elif event.is_partial:
                self._count_partial_drop()
                return False
            elif len(q) >= self._capacity + self._reserve:
                self._reject(event)
                return False
        q.append(event)
        self.high_water = max(self.high_water, len(q))
        self._wake.set()
        return True

    def _evict_oldest_partial(self) -> bool:
        for i, item in enumerate(self._queue):
            if isinstance(item, TranscriptEvent) and item.is_partial:
                del self._queue[i]
                self._count_partial_drop()
                return True
        return False

    def _count_partial_drop(self) -> None:
        self.partials_dropped += 1
        if self.partials_dropped == 1 or self.partials_dropped % 100 == 0:
            logger.warning("transcript_partial_dropped", extra={
                "call_id": self.call_id, "subscriber": self.name, "partials_dropped": self.partials_dropped,
                "queue_depth": len(self._queue),
            })

    def _reject(self, event: TranscriptEvent) -> None:
        if event.type == TRANSCRIPT_FINAL:
            self.finals_undeliverable += 1
        else:
            self.others_undeliverable += 1
        logger.error("transcript_event_undeliverable", extra={
            **event.log_fields(), "subscriber": self.name, "queue_depth": len(self._queue),
            "finals_undeliverable": self.finals_undeliverable,
        })

    # --- worker --------------------------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            while not self._queue:
                if self.state != ACTIVE:
                    return
                self._wake.clear()
                await self._wake.wait()
            item = self._queue.popleft()
            self._inflight = item
            try:
                await self._deliver(item)
            except asyncio.CancelledError:
                self._interrupted = item  # cancelled mid-callback: abort() counts it as undelivered
                raise
            finally:
                self._inflight = None
            if isinstance(item, CallClosedEvent):
                return

    async def _deliver(self, item: Any) -> None:
        started = time.monotonic()
        try:
            if isinstance(item, CallClosedEvent):
                hook = getattr(self._subscriber, "on_call_closed", None)
                if hook is not None:
                    result = hook(item)
                    if inspect.isawaitable(result):
                        await asyncio.wait_for(result, self._timeout_s)
            else:
                await asyncio.wait_for(self._subscriber.on_event(item), self._timeout_s)
                self.delivered += 1
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self.failures += 1
            self.timeouts += 1
            logger.warning("transcript_subscriber_timeout", extra={
                "call_id": self.call_id, "subscriber": self.name, "timeout_s": self._timeout_s,
                **({"event_type": item.type} if hasattr(item, "type") else {}),
            })
        except Exception as exc:
            self.failures += 1
            logger.warning("transcript_subscriber_failed", extra={
                "call_id": self.call_id, "subscriber": self.name, "error": type(exc).__name__,
                **({"event_type": item.type} if hasattr(item, "type") else {}),
            })
        finally:
            ms = (time.monotonic() - started) * 1000.0
            self._cb_last_ms = ms
            self._cb_max_ms = max(self._cb_max_ms, ms)
            self._cb_total_ms += ms
            self._cb_count += 1

    # --- shutdown ------------------------------------------------------------------------------

    def finish(self, marker: CallClosedEvent) -> None:
        """Call is closing: no more offers; the marker is queued last (it ignores capacity)."""
        if self.state == ACTIVE:
            self.state = CLOSING
            self._queue.append(marker)
            self._wake.set()

    async def abort(self, grace_s: float = 1.0) -> None:
        """Stop the worker (cancel if still running) and record whatever was never delivered."""
        if not self.task.done():
            self.task.cancel()
            await asyncio.wait([self.task], timeout=grace_s)
        leftover = [i for i in self._queue if isinstance(i, TranscriptEvent)]
        if isinstance(self._interrupted, TranscriptEvent):
            leftover.append(self._interrupted)
        self._interrupted = None
        self.undelivered += len(leftover)
        self.undelivered_finals += sum(1 for i in leftover if _is_final(i))
        self._queue.clear()
        self.state = CLOSED

    async def close(self) -> None:
        """Unsubscribe. Safe during delivery: the in-flight callback finishes (bounded by its timeout),
        queued events are counted as undelivered, and the worker task is released. Idempotent."""
        if self.state == CLOSED:
            return
        if self.state == ACTIVE:
            self.state = CLOSING
            leftover = [i for i in self._queue if isinstance(i, TranscriptEvent)]
            self.undelivered += len(leftover)
            self.undelivered_finals += sum(1 for i in leftover if _is_final(i))
            self._queue.clear()
            self._wake.set()
        if self._on_close is not None:
            self._on_close(self)
        if not self.task.done():
            await asyncio.wait([self.task], timeout=self._timeout_s + 0.5)
        await self.abort()

    def metrics(self) -> dict[str, object]:
        return {
            "name": self.name, "state": self.state, "delivered": self.delivered,
            "failures": self.failures, "timeouts": self.timeouts,
            "callback_last_ms": self._cb_last_ms, "callback_max_ms": round(self._cb_max_ms, 3),
            "callback_avg_ms": round(self._cb_total_ms / self._cb_count, 3) if self._cb_count else None,
            "queue_depth": len(self._queue), "queue_capacity": self._capacity,
            "final_reserve": self._reserve, "queue_high_water": self.high_water,
            "partials_dropped": self.partials_dropped, "finals_undeliverable": self.finals_undeliverable,
            "others_undeliverable": self.others_undeliverable, "undelivered": self.undelivered,
            "undelivered_finals": self.undelivered_finals,
        }


class _StreamSequence:
    """Sequence anomaly detector for one stream. It observes; it never reorders or generates numbers."""

    def __init__(self) -> None:
        self.max: int | None = None
        self.seen: set[int] = set()

    def check(self, seq: int) -> str:
        if self.max is None:
            self.max = seq
            self.seen.add(seq)
            return "ok"
        if seq in self.seen:
            return "duplicate"
        if seq < self.max:
            self.seen.add(seq)
            return "out_of_order"
        gap = seq > self.max + 1
        self.max = seq
        self.seen.add(seq)
        if len(self.seen) > 2 * SEQUENCE_WINDOW:
            floor = seq - SEQUENCE_WINDOW
            self.seen = {s for s in self.seen if s > floor}
        return "gap" if gap else "ok"


@dataclass
class _Counters:
    received: int = 0
    accepted: int = 0
    partial: int = 0
    final: int = 0
    rejected_after_close: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    gaps: int = 0
    wrong_call: int = 0


class CallTranscriptSession:
    """All transcript state for one call (spans reconnects: several streams may feed one session)."""

    def __init__(self, call_id: str, *, queue_size: int, final_reserve: int, subscriber_timeout_s: float,
                 drain_timeout_s: float, max_subscribers: int) -> None:
        self.call_id = call_id
        self.state = OPEN  # open -> closing -> closed
        self.opened_at = time.time()
        self.counters = _Counters()
        # Sequence state per namespace: (physical stream id, track). A tuple, never a concatenated string,
        # so a legacy stream id can never collide with a tracked one. Internal only: never published.
        self._streams: dict[tuple[str, str | None], _StreamSequence] = {}
        self._subs: list[TranscriptSubscription] = []
        self._all_subs: list[TranscriptSubscription] = []  # kept for final metrics
        self._queue_size = queue_size
        self._reserve = final_reserve
        self._timeout_s = subscriber_timeout_s
        self._drain_timeout_s = drain_timeout_s
        self._max_subs = max_subscribers
        self._close_task: asyncio.Task[CallCloseReport] | None = None
        self.report: CallCloseReport | None = None

    # --- subscribers ---------------------------------------------------------------------------

    def subscribe(self, subscriber: Any, name: str) -> TranscriptSubscription:
        if self.state != OPEN:
            raise CallClosedError(self.call_id)
        if len(self._subs) >= self._max_subs:
            raise SubscriberLimitError(f"max {self._max_subs} subscribers per call")
        sub = TranscriptSubscription(
            call_id=self.call_id, name=name, subscriber=_as_subscriber(subscriber), capacity=self._queue_size,
            reserve=self._reserve, timeout_s=self._timeout_s, on_close=self._forget,
        )
        self._subs.append(sub)
        self._all_subs.append(sub)
        return sub

    def _forget(self, sub: TranscriptSubscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    # --- events --------------------------------------------------------------------------------

    def accept(self, event: TranscriptEvent) -> str:
        """Synchronous and non-blocking. Returns what happened to the event."""
        c = self.counters
        c.received += 1
        if self.state != OPEN:
            c.rejected_after_close += 1
            level = logging.ERROR if event.type == TRANSCRIPT_FINAL else logging.WARNING
            logger.log(level, "transcript_event_after_close", extra={**event.log_fields(), "state": self.state})
            return "rejected_after_close"
        verdict = self._streams.setdefault((event.stream_id, event.track), _StreamSequence()).check(event.sequence)
        if verdict == "duplicate":
            c.duplicates += 1
            logger.warning("transcript_duplicate_sequence", extra=event.log_fields())
            return "duplicate"
        if verdict == "out_of_order":
            c.out_of_order += 1
            logger.warning("transcript_out_of_order", extra=event.log_fields())
        elif verdict == "gap":
            c.gaps += 1
            logger.warning("transcript_sequence_gap", extra=event.log_fields())
        c.accepted += 1
        if event.is_partial:
            c.partial += 1
        elif event.type == TRANSCRIPT_FINAL:
            c.final += 1
        for sub in list(self._subs):  # copy: a subscription may be removed while we iterate
            sub.offer(event)
        return "accepted" if verdict == "ok" else verdict

    # --- close ---------------------------------------------------------------------------------

    @property
    def closing_or_closed(self) -> bool:
        return self.state != OPEN

    def close(self, reason: str) -> "asyncio.Task[CallCloseReport]":
        """Start (once) and return the close task. Further calls return the same task."""
        if self._close_task is None:
            self.state = "closing"
            self._close_task = asyncio.create_task(self._do_close(reason), name=f"transcript-close:{self.call_id}")
        return self._close_task

    async def _do_close(self, reason: str) -> CallCloseReport:
        started = time.monotonic()
        subs = list(self._subs)
        marker = CallClosedEvent(
            call_id=self.call_id, reason=reason, closed_at=datetime.now(timezone.utc).isoformat(),
            events_accepted=self.counters.accepted, finals_accepted=self.counters.final,
        )
        for sub in subs:
            sub.finish(marker)
        tasks = [s.task for s in subs if not s.task.done()]
        timed_out = False
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self._drain_timeout_s)
            timed_out = bool(pending)
        for sub in subs:
            await sub.abort()
        duration = time.monotonic() - started
        self._subs.clear()
        self.state = "closed"
        undelivered = sum(s.undelivered for s in self._all_subs)
        undelivered_finals = sum(s.undelivered_finals for s in self._all_subs)
        self.report = CallCloseReport(
            call_id=self.call_id, reason=reason, drain_duration_s=round(duration, 4), drain_timed_out=timed_out,
            subscribers=len(subs), undelivered_events=undelivered, undelivered_finals=undelivered_finals,
        )
        level = logging.ERROR if undelivered_finals else logging.INFO
        logger.log(level, "transcript_session_closed", extra={
            "call_id": self.call_id, "reason": reason, "drain_duration_s": self.report.drain_duration_s,
            "drain_timed_out": timed_out, "undelivered_events": undelivered, "undelivered_finals": undelivered_finals,
            "events_accepted": self.counters.accepted, "finals_accepted": self.counters.final,
        })
        return self.report

    async def wait_closed(self) -> CallCloseReport | None:
        if self._close_task is None:
            return None
        return await asyncio.shield(self._close_task)

    # --- diagnostics ---------------------------------------------------------------------------

    def metrics(self) -> dict[str, object]:
        c = self.counters
        subs = self._all_subs
        return {
            "call_id": self.call_id, "state": self.state,
            "streams": len({stream_id for stream_id, _ in self._streams}),  # distinct physical streams
            "sequence_namespaces": len(self._streams),  # distinct (physical stream, track) identities
            "events_received": c.received, "events_accepted": c.accepted, "partials": c.partial, "finals": c.final,
            "events_delivered": sum(s.delivered for s in subs), "rejected_after_close": c.rejected_after_close,
            "duplicates": c.duplicates, "out_of_order": c.out_of_order, "sequence_gaps": c.gaps,
            "sequence_anomalies": c.duplicates + c.out_of_order + c.gaps,
            "active_subscribers": len(self._subs),
            "queue_depth": sum(len(s._queue) for s in self._subs),
            "queue_high_water": max((s.high_water for s in subs), default=0),
            "overflow_count": sum(s.partials_dropped + s.finals_undeliverable + s.others_undeliverable for s in subs),
            "partials_dropped": sum(s.partials_dropped for s in subs),
            "finals_undeliverable": sum(s.finals_undeliverable for s in subs),
            "drain_duration_s": self.report.drain_duration_s if self.report else None,
            "undelivered_events": sum(s.undelivered for s in subs),
            "undelivered_finals": sum(s.undelivered_finals for s in subs),
            "subscribers": [s.metrics() for s in subs],
        }


class TranscriptEventPipeline:
    """Registry of per-call transcript sessions. Implements `TranscriptPublisher`."""

    def __init__(
        self, *, queue_size: int = 256, final_reserve: int = 64, subscriber_timeout_s: float = 5.0,
        drain_timeout_s: float = 2.0, idle_close_s: float = 30.0, max_subscribers_per_call: int = 16,
        max_closed: int = 10_000, closed_ttl_s: float = 3600.0, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._opts = dict(
            queue_size=queue_size, final_reserve=final_reserve, subscriber_timeout_s=subscriber_timeout_s,
            drain_timeout_s=drain_timeout_s, max_subscribers=max_subscribers_per_call,
        )
        self._drain_timeout_s = drain_timeout_s
        self._idle_close_s = idle_close_s
        self._sessions: dict[str, CallTranscriptSession] = {}
        self._global: list[tuple[str, Any]] = []
        self._closed: OrderedDict[str, float] = OrderedDict()
        self._max_closed = max_closed
        self._closed_ttl_s = closed_ttl_s
        self._clock = clock
        self._idle: dict[str, asyncio.Task[None]] = {}
        self._shutting_down = False
        self.rejected_after_close = 0  # events for calls with no session (already closed / shutdown)
        self.calls_opened = 0
        self.calls_closed = 0
        self.total_undelivered = 0
        self.total_undelivered_finals = 0

    @classmethod
    def from_settings(cls, settings: Any) -> "TranscriptEventPipeline":
        return cls(
            queue_size=settings.transcript_subscriber_queue_size, final_reserve=settings.transcript_final_reserve,
            subscriber_timeout_s=settings.transcript_subscriber_timeout_s,
            drain_timeout_s=settings.transcript_drain_timeout_s, idle_close_s=settings.transcript_call_idle_close_s,
            max_subscribers_per_call=settings.transcript_max_subscribers_per_call,
        )

    # --- subscribers ---------------------------------------------------------------------------

    def add_global_subscriber(self, subscriber: Any, name: str) -> None:
        """Attach `subscriber` to every call opened from now on (each call gets its own queue and
        worker). Calls already open are not affected. The same object may serve many calls at once,
        so it must use `event.call_id` to separate them."""
        _as_subscriber(subscriber)  # validate now
        self._global.append((name, subscriber))

    def subscribe(self, call_id: str, subscriber: Any, name: str) -> TranscriptSubscription:
        """Subscribe to one call (opens its session if needed). Receives only events accepted after
        this point. Raises CallClosedError if the call's session is closed."""
        session = self.open_call(call_id)
        if session is None:
            raise CallClosedError(call_id)
        return session.subscribe(subscriber, name)

    # --- sessions ------------------------------------------------------------------------------

    def open_call(self, call_id: str) -> CallTranscriptSession | None:
        """Return the call's session, creating it once. None if the call was closed or we are shutting down."""
        session = self._sessions.get(call_id)
        if session is not None:
            return None if session.closing_or_closed else session
        if self._shutting_down or self._is_closed(call_id):
            return None
        session = CallTranscriptSession(call_id, **self._opts)  # type: ignore[arg-type]
        self._sessions[call_id] = session
        self.calls_opened += 1
        for name, subscriber in self._global:
            session.subscribe(subscriber, name)
        # DEBUG: this runs on the stream-start path, which should stay as short as possible.
        logger.debug("transcript_session_opened", extra={"call_id": call_id, "subscribers": len(self._global)})
        return session

    def get_session(self, call_id: str) -> CallTranscriptSession | None:
        return self._sessions.get(call_id)

    async def publish(self, event: TranscriptEvent) -> None:
        """TranscriptPublisher entry point. Synchronous inside: it never waits on a subscriber."""
        session = self._sessions.get(event.call_id) or self.open_call(event.call_id)
        if session is None:
            self.rejected_after_close += 1
            level = logging.ERROR if event.type == TRANSCRIPT_FINAL else logging.WARNING
            logger.log(level, "transcript_event_after_close", extra={**event.log_fields(), "state": "closed"})
            return
        session.accept(event)

    async def close_call(self, call_id: str, reason: str = "call_ended") -> CallCloseReport | None:
        """Close the call's session: stop accepting, drain bounded, notify subscribers, release tasks.
        Idempotent; concurrent callers share one close. Returns None if there is no such session."""
        idle = self._idle.pop(call_id, None)
        if idle is not None and idle is not asyncio.current_task():
            idle.cancel()
        session = self._sessions.get(call_id)
        if session is None:
            return None
        task = session.close(reason)
        try:
            report = await asyncio.shield(task)
        finally:
            if task.done() and not task.cancelled():
                self._finalize(call_id, session)
        return report

    def _finalize(self, call_id: str, session: CallTranscriptSession) -> None:
        if self._sessions.get(call_id) is session:
            del self._sessions[call_id]
            self.calls_closed += 1
            self.total_undelivered += sum(s.undelivered for s in session._all_subs)
            self.total_undelivered_finals += sum(s.undelivered_finals for s in session._all_subs)
            self._closed[call_id] = self._clock()
            self._closed.move_to_end(call_id)
            self._prune_closed()

    def _is_closed(self, call_id: str) -> bool:
        self._prune_closed()
        return call_id in self._closed

    def _prune_closed(self) -> None:
        now = self._clock()
        while self._closed:
            oldest, at = next(iter(self._closed.items()))
            if now - at > self._closed_ttl_s or len(self._closed) > self._max_closed:
                del self._closed[oldest]
            else:
                break

    # --- idle close (covers a lost hangup webhook) ---------------------------------------------

    def schedule_idle_close(self, call_id: str) -> None:
        if self._idle_close_s <= 0 or call_id not in self._sessions or call_id in self._idle:
            return
        self._idle[call_id] = asyncio.create_task(self._idle_close(call_id), name=f"transcript-idle:{call_id}")

    def cancel_idle_close(self, call_id: str) -> None:
        task = self._idle.pop(call_id, None)
        if task is not None:
            task.cancel()

    async def _idle_close(self, call_id: str) -> None:
        await asyncio.sleep(self._idle_close_s)
        await self.close_call(call_id, "idle")

    # --- shutdown ------------------------------------------------------------------------------

    async def shutdown(self, timeout_s: float | None = None) -> int:
        """Close every session (concurrently) within a bounded time. Returns how many were closed."""
        self._shutting_down = True
        for task in list(self._idle.values()):
            task.cancel()
        self._idle.clear()
        sessions = list(self._sessions.items())
        if not sessions:
            return 0
        budget = timeout_s if timeout_s is not None else self._drain_timeout_s + 1.0
        tasks = [asyncio.ensure_future(self.close_call(cid, "shutdown")) for cid, _ in sessions]
        _, pending = await asyncio.wait(tasks, timeout=budget)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.wait(pending, timeout=1.0)
            logger.error("transcript_shutdown_timeout", extra={"sessions_pending": len(pending)})
        # a session whose close task was cancelled is force-released here
        for cid, session in sessions:
            if self._sessions.get(cid) is session:
                close_task = session._close_task
                if close_task is not None and not close_task.done():
                    close_task.cancel()
                    await asyncio.wait([close_task], timeout=1.0)
                for sub in list(session._subs):
                    await sub.abort(grace_s=0.5)
                session.state = "closed"
                self._finalize(cid, session)
        return len(sessions)

    # --- diagnostics ---------------------------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        """Counters and per-call metrics. Never contains transcript text."""
        return {
            "calls_active": len(self._sessions), "calls_opened": self.calls_opened,
            "calls_closed": self.calls_closed, "rejected_after_close": self.rejected_after_close,
            "undelivered_events": self.total_undelivered, "undelivered_finals": self.total_undelivered_finals,
            "global_subscribers": [n for n, _ in self._global], "shutting_down": self._shutting_down,
            "calls": [s.metrics() for s in self._sessions.values()],
        }


class PipelineCallListener:
    """Hooks the pipeline into CallStateManager: opens a session when a stream starts, closes it when
    the call ends (hangup), and closes it after `idle_close_s` if every stream ended and no hangup came."""

    def __init__(
        self, pipeline: TranscriptEventPipeline, live_stream_count: Callable[[str], int] | None = None,
    ) -> None:
        # `live_stream_count(call_id)` (M5) tells how many physical streams of the call are still live.
        # Without it (legacy construction) every stream end schedules the idle close, as before.
        self._pipeline = pipeline
        self._live_stream_count = live_stream_count

    async def on_stream_start(self, session: Any) -> None:
        self._pipeline.cancel_idle_close(session.call_id)
        self._pipeline.open_call(session.call_id)

    async def on_stream_end(self, session: Any, reason: str) -> None:
        if reason == "superseded":  # a reconnect is already attached to the same call
            return
        if self._live_stream_count is not None:
            try:
                other_streams_live = self._live_stream_count(session.call_id) > 0
            except Exception:  # a broken counter must not leave a lost-hangup call open forever
                other_streams_live = False
            if other_streams_live:  # another physical stream still feeds this call's transcript session
                return
        self._pipeline.schedule_idle_close(session.call_id)

    async def on_dtmf(self, session: Any, event: Any) -> None:
        return None

    async def on_call_ended(self, call_id: str, reason: str) -> None:
        await self._pipeline.close_call(call_id, reason)
