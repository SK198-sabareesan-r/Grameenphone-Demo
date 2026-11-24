"""M4.4 internal transcript event pipeline. All automated: no real Sarvam or Vobiz involved."""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.publishers.pipeline import (
    CallClosedError, CallClosedEvent, PipelineCallListener, SubscriberLimitError, TranscriptEventPipeline,
)
from app.publishers.transcripts import (
    SESSION_END, TRANSCRIPT_FINAL, TRANSCRIPT_PARTIAL, VAD_SPEECH_START, TranscriptEvent,
)

SECRET_TEXT = "my secret sentence 12345"


def ev(call="c1", seq=0, type_=TRANSCRIPT_PARTIAL, stream="s1", text="hello", **kw) -> TranscriptEvent:
    return TranscriptEvent(
        call_id=call, stream_id=stream, sequence=seq, type=type_, received_at="2026-01-01T00:00:00+00:00",
        is_final=type_ == TRANSCRIPT_FINAL, text=text, **kw,
    )


def final(call="c1", seq=0, **kw) -> TranscriptEvent:
    return ev(call, seq, TRANSCRIPT_FINAL, **kw)


class Rec:
    """Subscriber that records events and the call.closed marker."""

    def __init__(self) -> None:
        self.events: list[TranscriptEvent] = []
        self.closed: list[CallClosedEvent] = []

    async def on_event(self, event: TranscriptEvent) -> None:
        self.events.append(event)

    async def on_call_closed(self, event: CallClosedEvent) -> None:
        self.closed.append(event)

    def seqs(self, call: str | None = None) -> list[int]:
        return [e.sequence for e in self.events if call is None or e.call_id == call]


class Gated(Rec):
    """Blocks every callback until released."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.started = asyncio.Event()

    async def on_event(self, event: TranscriptEvent) -> None:
        self.started.set()
        await self.gate.wait()
        await super().on_event(event)


async def settle(predicate, timeout: float = 2.0) -> None:
    async def _wait() -> None:
        while not predicate():
            await asyncio.sleep(0.002)
    await asyncio.wait_for(_wait(), timeout)


def pipe(**kw) -> TranscriptEventPipeline:
    kw.setdefault("drain_timeout_s", 0.5)
    kw.setdefault("subscriber_timeout_s", 0.5)
    kw.setdefault("idle_close_s", 0)
    return TranscriptEventPipeline(**kw)


def leftover_tasks() -> list[str]:
    return [t.get_name() for t in asyncio.all_tasks() if t.get_name().startswith("transcript-") and not t.done()]


# --- routing -----------------------------------------------------------------------------------


async def test_event_reaches_global_subscriber_with_text_unchanged() -> None:
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    event = final(seq=1, text="  exact   text, no changes ")
    await p.publish(event)
    await settle(lambda: len(r.events) == 1)
    assert r.events[0] is event and r.events[0].text == "  exact   text, no changes "
    await p.close_call("c1")


async def test_two_calls_are_isolated_and_each_keeps_its_own_order() -> None:
    p, r, only_a = pipe(), Rec(), Rec()
    p.add_global_subscriber(r, "all")
    p.open_call("a")
    p.subscribe("a", only_a, "only-a")
    for i in range(5):
        await p.publish(ev("a", i))
        await p.publish(ev("b", i))
    await settle(lambda: len(r.events) == 10)
    assert r.seqs("a") == list(range(5)) and r.seqs("b") == list(range(5))
    assert {e.call_id for e in only_a.events} == {"a"}
    await asyncio.gather(p.close_call("a"), p.close_call("b"))


async def test_every_event_type_is_delivered_in_order() -> None:
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    types = [VAD_SPEECH_START, TRANSCRIPT_PARTIAL, TRANSCRIPT_FINAL, SESSION_END]
    for i, t in enumerate(types):
        await p.publish(ev(seq=i, type_=t))
    await settle(lambda: len(r.events) == 4)
    assert [e.type for e in r.events] == types
    await p.close_call("c1")


async def test_multiple_subscribers_each_get_every_event_independently() -> None:
    p, a, b = pipe(), Rec(), Rec()
    p.add_global_subscriber(a, "a")
    p.add_global_subscriber(b, "b")
    for i in range(3):
        await p.publish(ev(seq=i))
    await settle(lambda: len(a.events) == 3 and len(b.events) == 3)
    assert a.seqs() == b.seqs() == [0, 1, 2]
    await p.close_call("c1")


async def test_plain_async_and_sync_callables_are_adapted() -> None:
    p, got_async, got_sync = pipe(), [], []

    async def a(e): got_async.append(e.sequence)
    def s(e): got_sync.append(e.sequence)

    p.add_global_subscriber(a, "a")
    p.add_global_subscriber(s, "s")
    await p.publish(ev(seq=7))
    await settle(lambda: got_async and got_sync)
    assert got_async == got_sync == [7]
    await p.close_call("c1")


async def test_subscriber_added_mid_call_gets_only_later_events_and_no_replay() -> None:
    p, early, late = pipe(), Rec(), Rec()
    p.add_global_subscriber(early, "early")
    await p.publish(ev(seq=0))
    await p.publish(ev(seq=1))
    p.subscribe("c1", late, "late")
    await p.publish(ev(seq=2))
    await settle(lambda: early.seqs() == [0, 1, 2] and late.seqs() == [2])
    await p.close_call("c1")


async def test_global_subscriber_added_later_does_not_touch_open_calls() -> None:
    p, late = pipe(), Rec()
    await p.publish(ev(seq=0))
    p.add_global_subscriber(late, "late")
    await p.publish(ev(seq=1))
    await p.publish(ev("c2", 0))
    await settle(lambda: late.seqs("c2") == [0])
    assert late.seqs("c1") == []
    await asyncio.gather(p.close_call("c1"), p.close_call("c2"))


async def test_subscriber_limit_per_call() -> None:
    p = pipe(max_subscribers_per_call=2)
    p.subscribe("c1", Rec(), "a")
    p.subscribe("c1", Rec(), "b")
    with pytest.raises(SubscriberLimitError):
        p.subscribe("c1", Rec(), "c")
    await p.close_call("c1")


async def test_snapshot_never_contains_transcript_text() -> None:
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    await p.publish(final(seq=0, text=SECRET_TEXT))
    await settle(lambda: r.events)
    assert SECRET_TEXT not in repr(p.snapshot())
    await p.close_call("c1")


# --- ordering ----------------------------------------------------------------------------------


async def test_existing_sequence_is_reused_not_regenerated() -> None:
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    for seq in (10, 11, 12):
        await p.publish(ev(seq=seq))
    await settle(lambda: len(r.events) == 3)
    assert r.seqs() == [10, 11, 12]
    assert p.get_session("c1").metrics()["sequence_anomalies"] == 0
    await p.close_call("c1")


async def test_duplicate_sequence_is_not_delivered_but_counted_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    await p.publish(ev(seq=0))
    await p.publish(final(seq=1))
    await p.publish(final(seq=1))  # retransmission
    await settle(lambda: len(r.events) == 2)
    m = p.get_session("c1").metrics()
    assert m["duplicates"] == 1 and r.seqs() == [0, 1]
    assert any(rec.message == "transcript_duplicate_sequence" for rec in caplog.records)
    await p.close_call("c1")


async def test_out_of_order_event_is_delivered_as_received_and_counted() -> None:
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    for seq in (0, 2, 1, 3):
        await p.publish(ev(seq=seq))
    await settle(lambda: len(r.events) == 4)
    assert r.seqs() == [0, 2, 1, 3]  # never reordered
    m = p.get_session("c1").metrics()
    assert m["out_of_order"] == 1 and m["sequence_gaps"] == 1
    await p.close_call("c1")


async def test_out_of_order_final_is_never_discarded() -> None:
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    await p.publish(ev(seq=5))
    await p.publish(final(seq=3))
    await settle(lambda: len(r.events) == 2)
    assert [e.type for e in r.events] == [TRANSCRIPT_PARTIAL, TRANSCRIPT_FINAL]
    await p.close_call("c1")


async def test_sequences_are_tracked_per_stream_so_reconnect_does_not_look_like_duplicate() -> None:
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    await p.publish(ev(seq=0, stream="s1"))
    await p.publish(ev(seq=1, stream="s1"))
    await p.publish(ev(seq=0, stream="s2"))  # reconnect: new stream restarts at 0
    await settle(lambda: len(r.events) == 3)
    m = p.get_session("c1").metrics()
    assert m["streams"] == 2 and m["sequence_anomalies"] == 0
    await p.close_call("c1")


async def test_late_event_after_close_is_rejected_counted_and_cannot_reopen() -> None:
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    await p.publish(ev(seq=0))
    await p.close_call("c1")
    await p.publish(final(seq=1))
    assert p.get_session("c1") is None and p.rejected_after_close == 1
    assert r.seqs() == [0]
    with pytest.raises(CallClosedError):
        p.subscribe("c1", Rec(), "x")


async def test_late_final_is_logged_at_error_without_text(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    p = pipe()
    await p.publish(ev(seq=0))
    await p.close_call("c1")
    await p.publish(final(seq=1, text=SECRET_TEXT))
    recs = [r for r in caplog.records if r.message == "transcript_event_after_close"]
    assert recs and recs[0].levelno == logging.ERROR
    assert SECRET_TEXT not in caplog.text


# --- backpressure ------------------------------------------------------------------------------


async def test_publish_never_blocks_on_a_stuck_subscriber() -> None:
    p, g = pipe(queue_size=4, final_reserve=2), Gated()
    p.add_global_subscriber(g, "stuck")
    await asyncio.wait_for(asyncio.gather(*(p.publish(ev(seq=i)) for i in range(500))), 1.0)
    g.gate.set()
    await p.close_call("c1")


async def test_full_queue_drops_oldest_partial_and_keeps_order_of_the_rest() -> None:
    p, g = pipe(queue_size=3, final_reserve=0, drain_timeout_s=1), Gated()
    p.add_global_subscriber(g, "g")
    await p.publish(ev(seq=0))
    await asyncio.wait_for(g.started.wait(), 1)  # seq 0 is in flight; the queue is empty
    for seq in (1, 2, 3, 4, 5):
        await p.publish(ev(seq=seq))
    m = p.get_session("c1").metrics()
    assert m["partials_dropped"] == 2 and m["queue_depth"] == 3 and m["queue_high_water"] == 3
    g.gate.set()
    await settle(lambda: len(g.events) == 4)
    assert g.seqs() == [0, 3, 4, 5]
    await p.close_call("c1")


async def test_finals_are_preserved_when_partials_flood_a_slow_subscriber() -> None:
    p, g = pipe(queue_size=4, final_reserve=2, drain_timeout_s=2), Gated()
    p.add_global_subscriber(g, "g")
    await p.publish(ev(seq=0))
    await asyncio.wait_for(g.started.wait(), 1)
    expected_finals = []
    seq = 1
    for _ in range(5):  # 5 finals fit: 4 slots + 2 reserve, as partials keep being evicted
        for _ in range(8):
            await p.publish(ev(seq=seq)); seq += 1
        await p.publish(final(seq=seq)); expected_finals.append(seq); seq += 1
    g.gate.set()
    await p.close_call("c1")
    delivered_finals = [e.sequence for e in g.events if e.type == TRANSCRIPT_FINAL]
    assert delivered_finals == expected_finals
    assert p.get_session("c1") is None and p.total_undelivered_finals == 0


async def test_finals_use_the_reserve_and_overflow_is_reported_when_even_that_is_full(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR)
    p, g = pipe(queue_size=2, final_reserve=1, drain_timeout_s=1), Gated()
    p.add_global_subscriber(g, "g")
    await p.publish(final(seq=0))
    await asyncio.wait_for(g.started.wait(), 1)
    for seq in (1, 2, 3, 4):  # 2 slots + 1 reserve = 3 accepted; the 4th cannot be stored
        await p.publish(final(seq=seq, text=SECRET_TEXT))
    m = p.get_session("c1").metrics()
    assert m["queue_depth"] == 3 and m["finals_undeliverable"] == 1 and m["overflow_count"] == 1
    errors = [r for r in caplog.records if r.message == "transcript_event_undeliverable"]
    assert len(errors) == 1 and errors[0].sequence == 4 and SECRET_TEXT not in caplog.text
    g.gate.set()
    await p.close_call("c1")


async def test_queued_partial_is_evicted_to_make_room_for_a_final() -> None:
    p, g = pipe(queue_size=2, final_reserve=0, drain_timeout_s=1), Gated()
    p.add_global_subscriber(g, "g")
    await p.publish(ev(seq=0))
    await asyncio.wait_for(g.started.wait(), 1)
    await p.publish(ev(seq=1))
    await p.publish(ev(seq=2))
    await p.publish(final(seq=3))
    g.gate.set()
    await p.close_call("c1")
    assert g.seqs() == [0, 2, 3]
    assert p.get_session("c1") is None


async def test_one_slow_subscriber_does_not_delay_a_fast_one() -> None:
    p, slow, fast = pipe(queue_size=2, final_reserve=0), Gated(), Rec()
    p.add_global_subscriber(slow, "slow")
    p.add_global_subscriber(fast, "fast")
    for i in range(20):
        await p.publish(ev(seq=i))
        await asyncio.sleep(0.001)  # the fast subscriber keeps up between events
    await settle(lambda: len(fast.events) == 20)
    metrics = {s["name"]: s for s in p.get_session("c1").metrics()["subscribers"]}
    assert metrics["slow"]["partials_dropped"] > 0 and metrics["fast"]["partials_dropped"] == 0
    slow.gate.set()
    await p.close_call("c1")


async def test_task_count_is_bounded_by_subscribers_not_by_events() -> None:
    p, g = pipe(queue_size=8, final_reserve=2), Gated()
    p.add_global_subscriber(g, "g")
    await p.publish(ev(seq=0))
    before = len(asyncio.all_tasks())
    for i in range(1, 2000):
        await p.publish(ev(seq=i))
    assert len(asyncio.all_tasks()) == before
    g.gate.set()
    await p.close_call("c1")


# --- failure isolation -------------------------------------------------------------------------


async def test_failing_subscriber_is_counted_and_does_not_affect_others_or_its_own_next_events(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    p, ok = pipe(), Rec()

    class Bad:
        calls = 0
        async def on_event(self, e):
            Bad.calls += 1
            raise RuntimeError(SECRET_TEXT)

    p.add_global_subscriber(Bad(), "bad")
    p.add_global_subscriber(ok, "ok")
    for i in range(3):
        await p.publish(ev(seq=i))
    await settle(lambda: len(ok.events) == 3 and Bad.calls == 3)
    subs = {s["name"]: s for s in p.get_session("c1").metrics()["subscribers"]}
    assert subs["bad"]["failures"] == 3 and subs["ok"]["failures"] == 0
    assert SECRET_TEXT not in caplog.text
    assert p.get_session("c1").state == "open"  # a failing subscriber never closes the session
    await p.close_call("c1")


async def test_subscriber_timeout_is_counted_and_delivery_continues() -> None:
    p = pipe(subscriber_timeout_s=0.05)
    got: list[int] = []

    async def slow_then_fast(e):
        if e.sequence == 0:
            await asyncio.sleep(10)
        got.append(e.sequence)

    p.add_global_subscriber(slow_then_fast, "s")
    await p.publish(ev(seq=0))
    await p.publish(ev(seq=1))
    await settle(lambda: got == [1])
    sub = p.get_session("c1").metrics()["subscribers"][0]
    assert sub["timeouts"] == 1 and sub["failures"] == 1 and sub["delivered"] == 1
    await p.close_call("c1")


async def test_unsubscribe_during_delivery_lets_inflight_finish_and_stops_the_rest() -> None:
    p, g, other = pipe(), Gated(), Rec()
    p.add_global_subscriber(other, "other")
    sub = p.subscribe("c1", g, "g")
    await p.publish(ev(seq=0))
    await asyncio.wait_for(g.started.wait(), 1)
    await p.publish(ev(seq=1))
    closer = asyncio.create_task(sub.close())
    await asyncio.sleep(0.01)
    g.gate.set()
    await closer
    assert g.seqs() == [0]  # in-flight finished, queued event discarded
    assert sub.undelivered == 1 and sub.state == "closed"
    await p.publish(ev(seq=2))
    await settle(lambda: other.seqs() == [0, 1, 2])
    assert g.seqs() == [0]
    await p.close_call("c1")


async def test_unsubscribe_is_idempotent() -> None:
    p = pipe()
    sub = p.subscribe("c1", Rec(), "x")
    await sub.close()
    await sub.close()
    await p.close_call("c1")
    assert leftover_tasks() == []


async def test_subscriber_unsubscribing_itself_from_its_callback_is_safe() -> None:
    p = pipe()
    holder: dict[str, object] = {}
    seen: list[int] = []

    async def cb(e):
        seen.append(e.sequence)
        asyncio.ensure_future(holder["sub"].close())

    holder["sub"] = p.subscribe("c1", cb, "self")
    await p.publish(ev(seq=0))
    await p.publish(ev(seq=1))
    await settle(lambda: holder["sub"].state == "closed")
    assert seen[0] == 0
    await p.close_call("c1")


# --- lifecycle ---------------------------------------------------------------------------------


async def test_session_is_created_once_per_call() -> None:
    p = pipe()
    a = p.open_call("c1")
    assert p.open_call("c1") is a and p.calls_opened == 1
    await p.publish(ev(seq=0))
    assert p.calls_opened == 1
    await p.close_call("c1")


async def test_close_drains_queued_events_then_sends_call_closed_last() -> None:
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    for i in range(5):
        await p.publish(ev(seq=i))
    await p.publish(final(seq=5))
    report = await p.close_call("c1", "hangup")
    assert r.seqs() == [0, 1, 2, 3, 4, 5]
    assert len(r.closed) == 1 and r.closed[0].reason == "hangup" and r.closed[0].finals_accepted == 1
    assert report.undelivered_events == 0 and not report.drain_timed_out
    assert report.drain_duration_s >= 0 and p.get_session("c1") is None
    assert leftover_tasks() == []


async def test_close_with_stuck_subscriber_is_bounded_and_records_undelivered() -> None:
    p, g = pipe(drain_timeout_s=0.1, queue_size=10), Gated()
    p.add_global_subscriber(g, "stuck")
    await p.publish(ev(seq=0))
    await asyncio.wait_for(g.started.wait(), 1)
    await p.publish(final(seq=1))
    await p.publish(ev(seq=2))
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    report = await p.close_call("c1")
    assert loop.time() - t0 < 1.5
    assert report.drain_timed_out
    assert report.undelivered_events == 3 and report.undelivered_finals == 1  # in-flight + 2 queued
    assert leftover_tasks() == []


async def test_duplicate_close_returns_the_same_result_and_closes_once() -> None:
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    await p.publish(ev(seq=0))
    a, b, c = await asyncio.gather(p.close_call("c1"), p.close_call("c1"), p.close_call("c1"))
    assert a is b is c and len(r.closed) == 1 and p.calls_closed == 1
    assert await p.close_call("c1") is None  # already gone: harmless


async def test_closing_unknown_call_is_harmless() -> None:
    assert await pipe().close_call("never-seen") is None


async def test_subscriber_failure_in_call_closed_hook_does_not_break_close() -> None:
    p = pipe()

    class Bad:
        async def on_event(self, e): ...
        async def on_call_closed(self, e): raise RuntimeError("x")

    p.add_global_subscriber(Bad(), "bad")
    await p.publish(ev(seq=0))
    report = await p.close_call("c1")
    assert report is not None and leftover_tasks() == []


async def test_reconnect_keeps_one_session_and_does_not_close_it() -> None:
    p, r = pipe(idle_close_s=0.05), Rec()
    p.add_global_subscriber(r, "r")
    listener = PipelineCallListener(p)

    class S:
        call_id = "c1"

    await listener.on_stream_start(S())
    await p.publish(ev(seq=0, stream="s1"))
    await listener.on_stream_end(S(), "websocket_closed")  # schedules the idle close
    await listener.on_stream_start(S())  # reconnect cancels it
    await asyncio.sleep(0.12)
    assert p.get_session("c1") is not None and p.calls_opened == 1
    await p.publish(ev(seq=0, stream="s2"))
    await settle(lambda: len(r.events) == 2)
    await p.close_call("c1")


async def test_idle_close_covers_a_lost_hangup() -> None:
    p, r = pipe(idle_close_s=0.05), Rec()
    p.add_global_subscriber(r, "r")
    listener = PipelineCallListener(p)

    class S:
        call_id = "c1"

    await listener.on_stream_start(S())
    await p.publish(final(seq=0))
    await listener.on_stream_end(S(), "websocket_closed")
    await settle(lambda: p.get_session("c1") is None)
    assert r.seqs() == [0] and r.closed[0].reason == "idle"
    await asyncio.sleep(0.02)
    assert leftover_tasks() == []


async def test_hangup_after_idle_close_is_a_noop() -> None:
    p = pipe(idle_close_s=0.02)
    listener = PipelineCallListener(p)

    class S:
        call_id = "c1"

    await listener.on_stream_start(S())
    await listener.on_stream_end(S(), "x")
    await settle(lambda: p.get_session("c1") is None)
    await listener.on_call_ended("c1", "hangup")  # duplicate hangup
    assert p.calls_closed == 1


async def test_shutdown_closes_all_calls_within_budget_and_leaves_no_tasks() -> None:
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    for c in ("a", "b", "c"):
        await p.publish(final(c, 0))
    assert await p.shutdown(2.0) == 3
    assert sorted(e.call_id for e in r.events) == ["a", "b", "c"]
    assert p.snapshot()["calls_active"] == 0 and leftover_tasks() == []
    await p.publish(ev("d", 0))  # after shutdown nothing reopens
    assert p.get_session("d") is None


async def test_shutdown_timeout_force_releases_stuck_subscribers() -> None:
    p, g = pipe(drain_timeout_s=30, subscriber_timeout_s=30), Gated()
    p.add_global_subscriber(g, "stuck")
    await p.publish(final(seq=0))
    await asyncio.wait_for(g.started.wait(), 1)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await p.shutdown(0.1)
    assert loop.time() - t0 < 3
    assert p.snapshot()["calls_active"] == 0
    assert p.total_undelivered_finals == 1  # the in-flight final that was force-cancelled is recorded
    assert leftover_tasks() == []


async def test_cancelled_close_caller_does_not_abandon_the_close() -> None:
    p, g = pipe(drain_timeout_s=0.3), Gated()
    p.add_global_subscriber(g, "g")
    await p.publish(ev(seq=0))
    await asyncio.wait_for(g.started.wait(), 1)
    closer = asyncio.create_task(p.close_call("c1"))
    await asyncio.sleep(0.02)
    closer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closer
    report = await p.close_call("c1")  # joins the same, still running, close
    assert report is not None and p.get_session("c1") is None
    assert leftover_tasks() == []


async def test_metrics_cover_the_required_fields() -> None:
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    await p.publish(ev(seq=0))
    await p.publish(final(seq=1))
    await settle(lambda: len(r.events) == 2)
    m = p.get_session("c1").metrics()
    for key in (
        "events_received", "partials", "finals", "events_delivered", "rejected_after_close", "sequence_anomalies",
        "active_subscribers", "state", "queue_depth", "queue_high_water", "overflow_count", "drain_duration_s",
        "undelivered_events",
    ):
        assert key in m
    sub = m["subscribers"][0]
    for key in ("delivered", "failures", "callback_last_ms", "queue_depth", "queue_high_water", "state"):
        assert key in sub
    assert m["events_received"] == 2 and m["partials"] == 1 and m["finals"] == 1 and m["events_delivered"] == 2
    await p.close_call("c1")
    assert p.snapshot()["calls_closed"] == 1


async def test_logs_never_contain_transcript_text(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    p, g = pipe(queue_size=1, final_reserve=0), Gated()
    p.add_global_subscriber(g, "g")
    for i in range(5):
        await p.publish(ev(seq=i, text=SECRET_TEXT))
    await p.publish(ev(seq=2, text=SECRET_TEXT))  # duplicate
    g.gate.set()
    await p.close_call("c1")
    await p.publish(final(seq=9, text=SECRET_TEXT))
    assert SECRET_TEXT not in caplog.text
