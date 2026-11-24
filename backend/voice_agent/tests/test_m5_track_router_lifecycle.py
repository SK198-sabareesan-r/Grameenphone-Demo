"""M5 lifecycle: track end, physical stream end, call end, and the shared transcript session.

Track lifecycle, physical-stream lifecycle and call lifecycle are separate:
  * ending a track finalizes only that track's sink;
  * ending a physical stream closes its router (and so its tracks) but no other stream;
  * call end (hangup webhook / shutdown) closes every stream, router, track and the transcript session.

Synthetic and in-process. Vobiz stream coexistence, track availability and hangup ordering are
UNVERIFIED provider behaviour; nothing here is evidence about them.
"""

from __future__ import annotations

import asyncio
import copy
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.publishers.pipeline import PipelineCallListener
from app.services.audio.audio_buffer import AudioFrame
from app.services.voice.audio_consumer import RUNNING
from app.services.voice.call_manager import CallStateManager, wait_for_pending_cleanups
from app.services.voice.track_router import TRACK_INBOUND, TRACK_OUTBOUND, TrackRoutingSinkFactory
from tests.test_m5_track_router import FakeFactory, FakeTrackSink, make_router, mk
from tests.test_transcript_pipeline import Rec, ev, leftover_tasks, pipe
from tests.test_transcript_pipeline import settle as pipeline_settle
from tests.test_vobiz_sarvam_stt import make_session, settle
from tests.test_vobiz_websocket import CALL, calls_of, make_settings, media_msg, open_stream, start_msg, token_for, wait_until

HANGUP = "/api/vobiz/hangup"
MON = "/ws/vobiz/monitor"


async def monitored(call: str, stream: str, factory: FakeFactory, *, queue_frames: int = 50):
    """A monitored StreamSession with its consumer running a TrackRouterSink. Returns (session, router)."""
    session = make_session(call, stream)
    session.kind = "monitor"
    routing = TrackRoutingSinkFactory(factory, queue_frames=queue_frames, drain_timeout_s=0.1, child_close_timeout_s=0.5)
    consumer = session.start_consumer(routing, drain_timeout_s=0.1)
    await settle(lambda: consumer.state == RUNNING)
    return session, consumer._sink


def feed(session, provider_track: str, n: int, *, count: int = 1, sig: int | None = None) -> None:
    for i in range(count):
        session.queue.put(mk(session, provider_track, n + i, sig=sig))
        session.stats.frames_received += 1


async def no_leaks() -> None:
    await asyncio.sleep(0.05)
    live = [t.get_name() for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
            and t.get_name().startswith(("vobiz-audio-consumer", "track-", "vobiz-stream-cleanup"))]
    assert live == []


# =============================================================================================
# track end
# =============================================================================================


async def test_l01_ending_one_track_leaves_the_other_track_running() -> None:
    factory = FakeFactory()
    session, router = await monitored("call-1", "mon-1", factory)
    feed(session, "inbound", 0, count=3)
    feed(session, "outbound", 0, count=3)
    await settle(lambda: len(factory.outcomes.get(TRACK_OUTBOUND, FakeTrackSink()).frames) == 3
                 and len(factory.outcomes.get(TRACK_INBOUND, FakeTrackSink()).frames) == 3)

    assert await router.end_track(TRACK_INBOUND, "track_done") is True
    inbound, outbound = factory.outcomes[TRACK_INBOUND], factory.outcomes[TRACK_OUTBOUND]
    assert inbound.close_calls == 1 and inbound.close_reason == "track_done"
    assert outbound.close_calls == 0 and not session.closed  # the other track and the stream are untouched

    feed(session, "outbound", 10, count=2)
    await settle(lambda: len(outbound.frames) == 5)
    assert router.metrics()["active_tracks"] == 1
    await session.close("socket_closed")
    await no_leaks()


# =============================================================================================
# physical stream end
# =============================================================================================


async def test_l02_physical_stream_end_closes_every_track_of_that_stream() -> None:
    factory = FakeFactory()
    session, router = await monitored("call-1", "mon-1", factory)
    feed(session, "inbound", 0, count=2)
    feed(session, "outbound", 0, count=2)
    await settle(lambda: TRACK_INBOUND in factory.outcomes and TRACK_OUTBOUND in factory.outcomes)
    await session.close("socket_closed")
    for sink in factory.outcomes.values():
        assert sink.close_calls == 1 and sink.close_reason == "socket_closed"
    assert router.metrics()["router_state"] == "closed"
    await no_leaks()


async def test_l03_ending_one_monitored_stream_leaves_other_streams_on_the_call_running() -> None:
    mgr = CallStateManager()
    primary = make_session("call-1", "p-1")
    m1, m2 = make_session("call-1", "m-1"), make_session("call-1", "m-2")
    m1.kind = m2.kind = "monitor"
    for s in (primary, m1, m2):
        await mgr.attach_stream(s)
    assert mgr.live_stream_count("call-1") == 3 and mgr.current_stream("call-1") is primary

    await mgr.detach_stream(m1, "socket_closed")
    assert m1.closed and not m2.closed and not primary.closed
    assert [s.stream_id for s in mgr.live_streams("call-1")] == ["p-1", "m-2"]
    snap = mgr.snapshot()[0]
    assert snap["active_streams"] == 2 and [s["stream_id"] for s in snap["monitor_streams"]] == ["m-2"]
    assert snap["streams_seen"] == 1 and snap["reconnects"] == 0  # legacy counters ignore monitored streams
    await mgr.on_hangup("call-1")
    assert m2.closed and primary.closed


async def test_l03b_a_monitored_stream_never_supersedes_the_primary_stream() -> None:
    mgr = CallStateManager()
    primary, monitor = make_session("call-1", "p-1"), make_session("call-1", "m-1")
    monitor.kind = "monitor"
    await mgr.attach_stream(primary)
    await mgr.attach_stream(monitor)
    assert not primary.closed and mgr.current_stream("call-1") is primary
    await mgr.on_hangup("call-1")


async def test_l03c_legacy_supersede_between_primary_streams_is_unchanged_and_spares_monitors() -> None:
    mgr = CallStateManager()
    old, new, monitor = make_session("call-1", "p-old"), make_session("call-1", "p-new"), make_session("call-1", "m-1")
    monitor.kind = "monitor"
    await mgr.attach_stream(old)
    await mgr.attach_stream(monitor)
    await mgr.attach_stream(new)
    assert old.closed and old.end_reason == "superseded"
    assert not monitor.closed and not new.closed and mgr.current_stream("call-1") is new
    await mgr.on_hangup("call-1")


async def test_l03d_repeat_attach_of_the_same_monitored_stream_id_replaces_the_older_one_safely() -> None:
    mgr = CallStateManager()
    first, second = make_session("call-1", "m-1"), make_session("call-1", "m-1")
    first.kind = second.kind = "monitor"
    await mgr.attach_stream(first)
    await mgr.attach_stream(second)
    assert first.closed and first.end_reason == "superseded" and not second.closed
    await mgr.detach_stream(first, "socket_closed")  # the old socket's late detach must not remove the new one
    assert mgr.live_stream_count("call-1") == 1
    await mgr.on_hangup("call-1")


# =============================================================================================
# call end
# =============================================================================================


async def test_l04_call_closure_closes_every_stream_router_and_track() -> None:
    mgr = CallStateManager()
    primary = make_session("call-1", "p-1")
    factories = [FakeFactory(), FakeFactory()]
    mon1, router1 = await monitored("call-1", "m-1", factories[0])
    mon2, router2 = await monitored("call-1", "m-2", factories[1])
    await mgr.attach_stream(primary)
    await mgr.attach_stream(mon1)
    await mgr.attach_stream(mon2)
    for s in (mon1, mon2):
        feed(s, "inbound", 0, count=2)
        feed(s, "outbound", 0, count=2)
    await settle(lambda: all(TRACK_INBOUND in f.outcomes and TRACK_OUTBOUND in f.outcomes for f in factories))

    assert await mgr.on_hangup("call-1") is True
    assert all(s.closed and s.end_reason == "hangup" for s in (primary, mon1, mon2))
    assert router1.metrics()["router_state"] == router2.metrics()["router_state"] == "closed"
    assert all(sink.close_calls == 1 for f in factories for sink in f.outcomes.values())
    assert mgr.is_ended("call-1") and mgr.live_stream_count("call-1") == 0
    await no_leaks()


async def test_l05_duplicate_lifecycle_events_are_idempotent() -> None:
    mgr = CallStateManager()
    factory = FakeFactory()
    mon, router = await monitored("call-1", "m-1", factory)
    await mgr.attach_stream(mon)
    feed(mon, "inbound", 0, count=2)
    await settle(lambda: TRACK_INBOUND in factory.outcomes)

    assert await router.end_track(TRACK_INBOUND, "x") is True and await router.end_track(TRACK_INBOUND, "x") is True
    await asyncio.gather(router.close("a"), router.close("b"), router.close("c"))
    assert await mgr.on_hangup("call-1") is True and await mgr.on_hangup("call-1") is False
    await mgr.detach_stream(mon, "socket_closed")
    await mgr.detach_stream(mon, "socket_closed")
    assert factory.outcomes[TRACK_INBOUND].close_calls == 1  # finalized exactly once
    await no_leaks()


async def test_l06a_buffered_frames_are_delivered_by_the_bounded_drain_on_stream_end() -> None:
    factory = FakeFactory()
    session, router = await monitored("call-1", "m-1", factory)
    feed(session, "inbound", 0, count=20)
    feed(session, "outbound", 100, count=20)
    await session.close("socket_closed")  # closed immediately, frames still buffered
    assert len(factory.outcomes[TRACK_INBOUND].frames) == 20 and len(factory.outcomes[TRACK_OUTBOUND].frames) == 20
    await no_leaks()


async def test_l06b_frames_that_cannot_drain_in_time_are_discarded_and_counted() -> None:
    gate = asyncio.Event()
    stuck = FakeTrackSink(gate=gate)
    session, router = make_router(FakeFactory({TRACK_INBOUND: stuck}), drain_timeout_s=0.1)
    await router.handle_frame(mk(session, "inbound", 0))
    await asyncio.wait_for(stuck.started.wait(), 2)
    for n in range(1, 6):
        await router.handle_frame(mk(session, "inbound", n))  # five more wait in the track queue
    loop = asyncio.get_running_loop()
    started = loop.time()
    await router.close("hangup")
    assert loop.time() - started < 2.0  # bounded: the stalled sink cannot hold shutdown hostage
    assert consumer_discarded(router) == 5 and stuck.close_calls == 1
    await no_leaks()


def consumer_discarded(router) -> int:
    return router.metrics()["tracks"][TRACK_INBOUND]["consumer"]["frames_discarded_on_close"]


async def test_l07_cleanup_survives_cancellation_of_the_caller() -> None:
    a, b = FakeTrackSink(close_delay=0.05), FakeTrackSink(close_delay=0.05)
    session, router = make_router(FakeFactory({TRACK_INBOUND: a, TRACK_OUTBOUND: b}))
    await router.handle_frame(mk(session, "inbound", 0))
    await router.handle_frame(mk(session, "outbound", 0))
    await settle(lambda: len(a.frames) == 1 and len(b.frames) == 1)
    closer = asyncio.create_task(router.close("shutdown"))
    await asyncio.sleep(0)
    closer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closer
    await wait_for_pending_cleanups(3)
    assert a.close_calls == 1 and b.close_calls == 1  # the cleanup still ran to completion
    await no_leaks()


async def test_l08_close_time_is_bounded_by_the_slowest_track_not_their_sum() -> None:
    a, b = FakeTrackSink(close_delay=0.3), FakeTrackSink(close_delay=0.3)
    session, router = make_router(FakeFactory({TRACK_INBOUND: a, TRACK_OUTBOUND: b}), child_close_timeout_s=1.0)
    await router.handle_frame(mk(session, "inbound", 0))
    await router.handle_frame(mk(session, "outbound", 0))
    await settle(lambda: len(a.frames) == 1 and len(b.frames) == 1)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await router.close("hangup")
    assert loop.time() - started < 0.55  # concurrent: ~0.3 s, not 0.6 s
    assert router.metrics()["close_ms"] is not None
    await no_leaks()


async def test_l09_hangup_before_any_frame_closes_cleanly() -> None:
    mgr = CallStateManager()
    session, router = await monitored("call-1", "m-1", FakeFactory())
    await mgr.attach_stream(session)
    await mgr.on_hangup("call-1")
    assert session.closed and router.metrics()["tracks"] == {} and router.metrics()["router_state"] == "closed"
    await no_leaks()


# =============================================================================================
# shared transcript session across physical streams
# =============================================================================================


class _Call:
    def __init__(self, call_id: str, stream_id: str) -> None:
        self.call_id, self.stream_id = call_id, stream_id


async def test_l10_one_stream_ending_does_not_idle_close_a_session_another_stream_still_feeds() -> None:
    p, r = pipe(idle_close_s=0.05), Rec()
    p.add_global_subscriber(r, "r")
    mgr = CallStateManager()
    mgr.add_listener(PipelineCallListener(p, live_stream_count=mgr.live_stream_count))
    primary, monitor = make_session("c1", "p-1"), make_session("c1", "m-1")
    monitor.kind = "monitor"
    await mgr.attach_stream(primary)
    await mgr.attach_stream(monitor)
    await p.publish(ev("c1", 1, stream="m-1", track=TRACK_INBOUND))

    await mgr.detach_stream(primary, "socket_closed")  # the AI stream ends; the monitored stream lives on
    await asyncio.sleep(0.2)  # four times idle_close_s
    assert p.get_session("c1") is not None and r.closed == []
    await p.publish(ev("c1", 2, stream="m-1", track=TRACK_INBOUND))
    await pipeline_settle(lambda: len(r.events) == 2)

    await mgr.detach_stream(monitor, "socket_closed")  # last live stream gone: the idle close now applies
    await pipeline_settle(lambda: p.get_session("c1") is None)
    assert [c.reason for c in r.closed] == ["idle"]
    await asyncio.sleep(0.02)
    assert leftover_tasks() == []


async def test_l10b_without_the_live_stream_check_behaviour_is_the_legacy_one() -> None:
    p = pipe(idle_close_s=0.03)
    listener = PipelineCallListener(p)  # legacy construction
    await listener.on_stream_start(_Call("c1", "s1"))
    await listener.on_stream_end(_Call("c1", "s1"), "socket_closed")
    await pipeline_settle(lambda: p.get_session("c1") is None)


async def test_l11_hangup_closes_the_shared_transcript_session_with_all_streams() -> None:
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    mgr = CallStateManager()
    mgr.add_listener(PipelineCallListener(p, live_stream_count=mgr.live_stream_count))
    primary, monitor = make_session("c1", "p-1"), make_session("c1", "m-1")
    monitor.kind = "monitor"
    await mgr.attach_stream(primary)
    await mgr.attach_stream(monitor)
    await p.publish(ev("c1", 1, stream="m-1", track=TRACK_OUTBOUND))
    await mgr.on_hangup("c1")
    assert p.get_session("c1") is None and [c.reason for c in r.closed] == ["hangup"]
    await p.publish(ev("c1", 2, stream="m-1", track=TRACK_OUTBOUND))  # late event cannot reopen the call
    assert p.get_session("c1") is None and p.rejected_after_close == 1


# =============================================================================================
# end to end through the WebSocket handler (synthetic Vobiz client)
# =============================================================================================


class CollectingSink:
    def __init__(self) -> None:
        self.frames: list[AudioFrame] = []
        self.closed = False

    async def handle_frame(self, frame: AudioFrame) -> None:
        self.frames.append(frame)

    async def close(self, reason: str) -> None:
        self.closed = True


def monitor_frame(track: str, n: int, stream: str) -> dict:
    msg = copy.deepcopy(media_msg(bytes([n % 250 + 1, 1]) * 320, stream=stream, chunk=n, seq=n))
    msg["media"]["track"] = track
    return msg


class open_monitor:
    def __init__(self, client: TestClient, call: str = CALL, stream: str = "mon-1") -> None:
        self.client, self.call, self.stream = client, call, stream

    def __enter__(self):
        self._cm = self.client.websocket_connect(f"{MON}/{self.call}?token={token_for(self.call)}")
        self.ws = self._cm.__enter__()
        start = start_msg(self.call, self.stream)
        start["start"]["tracks"] = ["inbound", "outbound"]
        self.ws.send_json(start)
        self.session = wait_until(lambda: next(
            (s for s in calls_of(self.client).live_streams(self.call) if s.stream_id == self.stream), None))
        return self

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)


def test_l12_legacy_stream_keeps_its_single_sink_and_monitored_stream_is_routed() -> None:
    legacy = CollectingSink()
    factory = FakeFactory()
    routing = TrackRoutingSinkFactory(factory, queue_frames=50, drain_timeout_s=0.1, child_close_timeout_s=0.5)
    app = create_app(make_settings(), audio_sink_factory=lambda session: legacy, monitor_sink_factory=routing)
    with TestClient(app) as client:
        with open_stream(client) as primary, open_monitor(client, stream="mon-1") as mon:
            for n in range(1, 4):
                primary.ws.send_json(media_msg(bytes([n, 0]) * 320, chunk=n, seq=n))  # provider track "inbound"
                mon.ws.send_json(monitor_frame("inbound", n, "mon-1"))
                mon.ws.send_json(monitor_frame("outbound", n, "mon-1"))
            wait_until(lambda: len(legacy.frames) == 3 and TRACK_INBOUND in factory.outcomes
                       and TRACK_OUTBOUND in factory.outcomes
                       and len(factory.outcomes[TRACK_INBOUND].frames) == 3
                       and len(factory.outcomes[TRACK_OUTBOUND].frames) == 3)
            assert mon.session.kind == "monitor" and primary.session.kind == "primary"
            assert {f.track for f in legacy.frames} == {"inbound"}  # the legacy path is the plain single sink
            assert [view.stream_id for view, _ in factory.calls] == ["mon-1", "mon-1"]  # physical id
            assert calls_of(client).live_stream_count(CALL) == 2

            assert client.post(HANGUP, data={"CallUUID": CALL, "Direction": "outbound", "Event": "Hangup"}).status_code == 200
            assert primary.session.closed and mon.session.closed
            assert all(sink.close_calls == 1 for sink in factory.outcomes.values()) and legacy.closed
