"""M5 failure isolation for the track router and the multi-stream call manager.

A failure in one track, one stream, one subscriber or one cleanup must stay there, and a monitoring
failure must never reach the customer-agent call (no Vobiz hangup/transfer request is ever made).
Synthetic and in-process; nothing here is evidence about real provider behaviour.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.integrations.vobiz.client import VobizClient
from app.main import create_app
from app.publishers.pipeline import PipelineCallListener
from app.services.voice.audio_consumer import FAILED, UNAVAILABLE
from app.services.voice.call_manager import CallStateManager
from app.services.voice.track_router import TRACK_INBOUND, TRACK_OUTBOUND, TrackRoutingSinkFactory
from tests.test_m5_track_router import FakeFactory, FakeTrackSink, consumer_metrics, make_router, mk
from tests.test_m5_track_router_lifecycle import HANGUP, monitor_frame, no_leaks, open_monitor
from tests.test_transcript_pipeline import Rec, ev, pipe
from tests.test_transcript_pipeline import settle as pipeline_settle
from tests.test_vobiz_sarvam_stt import make_session, settle
from tests.test_vobiz_websocket import CALL, calls_of, make_settings, wait_until


async def test_f01_sink_initialization_failure_is_isolated_to_its_track() -> None:
    outbound = FakeTrackSink()
    factory = FakeFactory({TRACK_INBOUND: RuntimeError("cannot start"), TRACK_OUTBOUND: outbound})
    session, router = make_router(factory)
    await router.handle_frame(mk(session, "inbound", 0))
    await router.handle_frame(mk(session, "outbound", 0))
    await settle(lambda: router.worker(TRACK_INBOUND).consumer.state == FAILED and len(outbound.frames) == 1)

    assert consumer_metrics(router, TRACK_INBOUND)["failure_reason"] == "sink_factory_failed"
    for n in range(1, 4):
        await router.handle_frame(mk(session, "outbound", n))  # the healthy track keeps going
    await settle(lambda: len(outbound.frames) == 4)
    await router.close("test")
    await no_leaks()


async def test_f01b_a_factory_that_declines_a_track_marks_it_unavailable_only() -> None:
    outbound = FakeTrackSink()
    session, router = make_router(FakeFactory({TRACK_INBOUND: None, TRACK_OUTBOUND: outbound}))  # None = declined
    await router.handle_frame(mk(session, "inbound", 0))
    await router.handle_frame(mk(session, "outbound", 0))
    await settle(lambda: router.worker(TRACK_INBOUND).consumer.state == UNAVAILABLE and len(outbound.frames) == 1)
    await router.close("test")


async def test_f02_each_track_has_its_own_consecutive_error_budget() -> None:
    inbound, outbound = FakeTrackSink(raises=RuntimeError("x")), FakeTrackSink()
    session, router = make_router(FakeFactory({TRACK_INBOUND: inbound, TRACK_OUTBOUND: outbound}),
                                  max_consecutive_errors=3)
    for n in range(6):
        await router.handle_frame(mk(session, "inbound", n))
        await router.handle_frame(mk(session, "outbound", n))
    await settle(lambda: router.worker(TRACK_INBOUND).consumer.state == FAILED and len(outbound.frames) == 6)
    assert router.worker(TRACK_OUTBOUND).consumer.state != FAILED
    assert consumer_metrics(router, TRACK_OUTBOUND)["processing_errors"] == 0
    await router.close("test")


async def test_f03_router_handle_frame_never_raises_for_track_faults() -> None:
    session, router = make_router(FakeFactory({TRACK_INBOUND: FakeTrackSink(raises=ValueError("x"))}),
                                  max_consecutive_errors=2)
    for n in range(10):
        await router.handle_frame(mk(session, "inbound", n))
        await asyncio.sleep(0)
    await settle(lambda: router.worker(TRACK_INBOUND).consumer.state == FAILED)
    await router.handle_frame(mk(session, "inbound", 99))  # rejected and counted, not raised
    assert router.metrics()["frames_rejected"]["track_failed"] >= 1
    await router.close("test")


async def test_f04_a_close_that_raises_in_one_track_does_not_stop_the_other_track_closing() -> None:
    class BadClose(FakeTrackSink):
        async def close(self, reason: str) -> None:
            await super().close(reason)
            raise RuntimeError("close failed")

    bad, good = BadClose(), FakeTrackSink()
    session, router = make_router(FakeFactory({TRACK_INBOUND: bad, TRACK_OUTBOUND: good}))
    await router.handle_frame(mk(session, "inbound", 0))
    await router.handle_frame(mk(session, "outbound", 0))
    await settle(lambda: len(bad.frames) == 1 and len(good.frames) == 1)
    await router.close("test")  # the consumer logs the sink-close error; nothing propagates
    assert bad.close_calls == 1 and good.close_calls == 1
    await no_leaks()


async def test_f05_stream_cleanup_failure_does_not_block_other_streams() -> None:
    mgr = CallStateManager()
    sessions = [make_session("call-1", f"s-{i}") for i in range(3)]
    for s in sessions[1:]:
        s.kind = "monitor"
    for s in sessions:
        await mgr.attach_stream(s)

    async def boom(*args, **kwargs):
        raise RuntimeError("cleanup failed")

    sessions[1].close = boom  # one monitored stream's cleanup raises
    assert await mgr.on_hangup("call-1") is True
    assert sessions[0].closed and sessions[2].closed  # the others were still closed
    assert mgr.metrics()["cleanup_failures"] == 1


async def test_f05b_close_all_closes_every_stream_even_if_one_cleanup_fails() -> None:
    mgr = CallStateManager()
    a, b, c = make_session("call-a", "a"), make_session("call-b", "b"), make_session("call-b", "c")
    c.kind = "monitor"
    for s in (a, b, c):
        await mgr.attach_stream(s)

    async def boom(*args, **kwargs):
        raise RuntimeError("cleanup failed")

    b.close = boom
    assert await mgr.close_all("shutdown") == 3
    assert a.closed and c.closed and mgr.metrics()["cleanup_failures"] == 1


async def test_f06_a_failing_transcript_subscriber_does_not_affect_other_tracks_events() -> None:
    p, ok = pipe(), Rec()

    class Bad:
        async def on_event(self, event) -> None:
            raise RuntimeError("subscriber failed")

    p.add_global_subscriber(Bad(), "bad")
    p.add_global_subscriber(ok, "ok")
    for seq in (1, 2):
        await p.publish(ev("c1", seq, stream="m-1", track=TRACK_INBOUND))
        await p.publish(ev("c1", seq, stream="m-1", track=TRACK_OUTBOUND))
    await pipeline_settle(lambda: len(ok.events) == 4)
    assert p.get_session("c1").state == "open" and p.get_session("c1").metrics()["duplicates"] == 0
    await p.close_call("c1")


async def test_f07_a_stalled_track_does_not_delay_shutdown_beyond_its_budget() -> None:
    gate = asyncio.Event()
    stuck = FakeTrackSink(gate=gate, close_delay=5.0)  # both processing and finalizing would hang
    healthy = FakeTrackSink()
    session, router = make_router(FakeFactory({TRACK_INBOUND: stuck, TRACK_OUTBOUND: healthy}),
                                  child_close_timeout_s=0.3)
    await router.handle_frame(mk(session, "inbound", 0))
    await router.handle_frame(mk(session, "outbound", 0))
    await settle(lambda: len(healthy.frames) == 1)
    loop = asyncio.get_running_loop()
    started = loop.time()
    # the owning consumer would apply router.close_timeout_s; mirror that bound here
    await asyncio.wait_for(router.close("shutdown"), router.close_timeout_s + 1.0)
    assert healthy.close_calls == 1
    assert loop.time() - started < router.close_timeout_s + 1.0
    await asyncio.sleep(0)


# --- monitoring failure must never touch the customer-agent call ----------------------------------


def test_f08_monitor_failure_never_issues_a_vobiz_transfer_or_hangup(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[tuple] = []

    async def record(self, method, path, **kwargs):  # any Vobiz REST request would land here
        requests.append((method, path))
        return {}

    monkeypatch.setattr(VobizClient, "request", record)
    failing = FakeFactory({TRACK_INBOUND: RuntimeError("sink down"), TRACK_OUTBOUND: FakeTrackSink(raises=RuntimeError("x"))})
    routing = TrackRoutingSinkFactory(failing, queue_frames=20, drain_timeout_s=0.1, child_close_timeout_s=0.5,
                                      max_consecutive_errors=2)
    app = create_app(make_settings(), monitor_sink_factory=routing)
    with TestClient(app) as client:
        with open_monitor(client) as mon:
            for n in range(1, 6):
                mon.ws.send_json(monitor_frame("inbound", n, "mon-1"))
                mon.ws.send_json(monitor_frame("outbound", n, "mon-1"))
            wait_until(lambda: mon.session.consumer is not None and mon.session.consumer.frames_processed >= 10)
            assert not mon.session.closed  # the monitored stream itself stays up; tracks fail on their own
        wait_until(lambda: calls_of(client).live_stream_count(CALL) == 0)
        assert client.post(HANGUP, data={"CallUUID": CALL, "Direction": "outbound", "Event": "Hangup"}).status_code == 200
    assert requests == []  # no hangup, no transfer, no other Vobiz call was ever made


def test_f09_router_metrics_expose_the_diagnostic_fields() -> None:
    async def run() -> dict:
        session, router = make_router(FakeFactory())
        await router.handle_frame(mk(session, "inbound", 0))
        await router.handle_frame(mk(session, "weird", 0))
        await settle(lambda: router.worker(TRACK_INBOUND) is not None and len(router.worker(TRACK_INBOUND).consumer._queue) == 0)
        metrics = router.metrics()
        await router.close("test")
        return metrics

    metrics = asyncio.run(run())
    assert {"router_state", "active_tracks", "tracks_started", "frames_routed", "frames_rejected",
            "frames_rejected_total", "track_close_failures", "tracks"} <= set(metrics)
    consumer = metrics["tracks"][TRACK_INBOUND]["consumer"]
    for key in ("consumer_state", "queue_depth", "queue_high_water", "frames_received", "frames_processed",
                "frames_dropped", "frames_discarded_on_close", "processing_errors", "queue_capacity"):
        assert key in consumer
    assert set(metrics["frames_rejected"]) >= {"unknown_track", "missing_track", "empty_track", "track_failed",
                                               "track_ended", "router_closed"}
