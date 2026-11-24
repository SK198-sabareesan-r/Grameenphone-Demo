"""M5 track router: routing, validation, queue and consumer isolation.

Everything is in-process and synthetic: fake sinks, synthetic frames. No provider is involved, so
these tests prove OUR routing logic against the documented tag values (`inbound` / `outbound`); they
do not show which participant either track carries or how Vobiz behaves.

Importing helpers from the existing M4.3 test module pulls in `app.main`; run under the approved
external isolation guard (see the task notes) so no real settings or `.env` are loaded.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from app.services.audio.audio_buffer import AudioFrame
from app.services.voice.audio_consumer import FAILED, AudioFrameSink
from app.services.voice.track_router import (
    PROVIDER_TRACK_LABELS, REJECT_EMPTY, REJECT_MISSING, REJECT_ROUTER_CLOSED, REJECT_TRACK_ENDED,
    REJECT_TRACK_FAILED, REJECT_UNKNOWN, TRACK_INBOUND, TRACK_OUTBOUND, TrackRouterSink, classify_track,
)
from tests.test_vobiz_sarvam_stt import make_session, settle


# --- fakes and builders (shared with the other M5 router test files) ---------------------------------


class FakeTrackSink:
    """A per-track sink that records frames; can be gated (stalled), made to raise, or close slowly."""

    def __init__(self, *, gate: asyncio.Event | None = None, raises: BaseException | None = None,
                 close_delay: float = 0.0) -> None:
        self.frames: list[AudioFrame] = []
        self.gate, self.raises, self.close_delay = gate, raises, close_delay
        self.started = asyncio.Event()  # set when the first frame reaches handle_frame
        self.close_calls = 0
        self.close_reason: str | None = None

    async def handle_frame(self, frame: AudioFrame) -> None:
        self.started.set()
        if self.raises is not None:
            raise self.raises
        if self.gate is not None:
            await self.gate.wait()
        self.frames.append(frame)

    async def close(self, reason: str) -> None:
        self.close_calls += 1
        self.close_reason = reason
        if self.close_delay:
            await asyncio.sleep(self.close_delay)

    def metrics(self) -> dict[str, object]:
        return {"frames": len(self.frames)}


class FakeFactory:
    """TrackSinkFactory: hands out the sink (or raises / returns None) configured for each track."""

    def __init__(self, outcomes: dict[str, object] | None = None) -> None:
        self.outcomes = dict(outcomes or {})
        self.calls: list[tuple[object, str]] = []

    def __call__(self, view, track: str):
        self.calls.append((view, track))
        outcome = self.outcomes.get(track)
        if outcome is None and track not in self.outcomes:
            outcome = self.outcomes[track] = FakeTrackSink()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def tracks_created(self) -> list[str]:
        return [track for _, track in self.calls]


def mk(session, provider_track, n: int, *, sig: int | None = None) -> AudioFrame:
    """A synthetic 20 ms L16/16 kHz frame (640 bytes) whose first byte is a per-track signature."""
    data = bytes([sig if sig is not None else n % 250 + 1, 1]) * 320
    return AudioFrame(data=data, format=session.audio_format, track=provider_track, timestamp_ms=n * 20,
                      chunk=n, sequence=n, stream_id=session.stream_id, received_at=time.monotonic())


def make_router(factory, *, queue_frames: int = 50, session=None, drain_timeout_s: float = 0.1,
                child_close_timeout_s: float = 0.5, **kw):
    session = session or make_session("call-1", "stream-1")
    router = TrackRouterSink(session, factory, queue_frames=queue_frames, drain_timeout_s=drain_timeout_s,
                             child_close_timeout_s=child_close_timeout_s, **kw)
    return session, router


def rejected(router, reason: str) -> int:
    return router.metrics()["frames_rejected"][reason]


def consumer_metrics(router, track: str) -> dict[str, object]:
    return router.metrics()["tracks"][track]["consumer"]


# =============================================================================================
# R: routing and validation
# =============================================================================================


async def test_r01_inbound_frames_reach_only_the_inbound_sink() -> None:
    factory = FakeFactory()
    session, router = make_router(factory)
    for n in range(5):
        await router.handle_frame(mk(session, "inbound", n))
    await settle(lambda: TRACK_INBOUND in factory.outcomes and len(factory.outcomes[TRACK_INBOUND].frames) == 5)
    assert factory.tracks_created() == [TRACK_INBOUND]  # no outbound sink was ever created
    assert [f.chunk for f in factory.outcomes[TRACK_INBOUND].frames] == [0, 1, 2, 3, 4]
    await router.close("test")


async def test_r02_outbound_frames_reach_only_the_outbound_sink() -> None:
    factory = FakeFactory()
    session, router = make_router(factory)
    for n in range(5):
        await router.handle_frame(mk(session, "outbound", n))
    await settle(lambda: TRACK_OUTBOUND in factory.outcomes and len(factory.outcomes[TRACK_OUTBOUND].frames) == 5)
    assert factory.tracks_created() == [TRACK_OUTBOUND]
    await router.close("test")


@pytest.mark.parametrize(("raw", "reason"), [
    ("weird", REJECT_UNKNOWN), ("Inbound", REJECT_UNKNOWN), ("track_inbound", REJECT_UNKNOWN), ("both", REJECT_UNKNOWN),
])
async def test_r03_unknown_track_is_rejected_and_counted(raw: str, reason: str) -> None:
    factory = FakeFactory()
    session, router = make_router(factory)
    await router.handle_frame(mk(session, raw, 1))  # must not raise
    assert rejected(router, reason) == 1 and factory.calls == []  # never reaches a track-processing path
    assert router.metrics()["tracks"] == {}
    await router.close("test")


async def test_r04_missing_track_is_rejected_and_counted() -> None:
    factory = FakeFactory()
    session, router = make_router(factory)
    await router.handle_frame(mk(session, None, 1))  # a frame built without a track
    assert rejected(router, REJECT_MISSING) == 1 and factory.calls == []
    await router.close("test")


@pytest.mark.parametrize("raw", ["", "   "])
async def test_r05_empty_track_is_rejected_and_counted(raw: str) -> None:
    factory = FakeFactory()
    session, router = make_router(factory)
    await router.handle_frame(mk(session, raw, 1))
    assert rejected(router, REJECT_EMPTY) == 1 and factory.calls == []
    await router.close("test")


def test_r05b_classify_track_is_exact_and_neutral() -> None:
    assert classify_track("inbound") == (TRACK_INBOUND, None)
    assert classify_track("outbound") == (TRACK_OUTBOUND, None)
    assert classify_track(None)[1] == REJECT_MISSING and classify_track(5)[1] == REJECT_MISSING
    assert classify_track("")[1] == REJECT_EMPTY and classify_track("INBOUND")[1] == REJECT_UNKNOWN
    assert PROVIDER_TRACK_LABELS == {"inbound": "track_inbound", "outbound": "track_outbound"}
    assert not any(word in label for label in PROVIDER_TRACK_LABELS.values() for word in ("customer", "agent"))


async def test_r06_one_full_track_queue_does_not_block_the_other_track() -> None:
    gate = asyncio.Event()
    inbound, outbound = FakeTrackSink(gate=gate), FakeTrackSink()
    factory = FakeFactory({TRACK_INBOUND: inbound, TRACK_OUTBOUND: outbound})
    session, router = make_router(factory, queue_frames=3)

    await router.handle_frame(mk(session, "inbound", 0))
    await asyncio.wait_for(inbound.started.wait(), 2)  # frame 0 is now in flight, blocked in the sink
    for n in range(1, 10):
        await router.handle_frame(mk(session, "inbound", n))  # 9 offered, capacity 3
    for n in range(3):  # within the (small) capacity, so nothing of outbound should ever be lost
        await router.handle_frame(mk(session, "outbound", n))
    await settle(lambda: len(outbound.frames) == 3)  # outbound flowed while inbound was stuck

    m = consumer_metrics(router, TRACK_INBOUND)
    assert m["frames_dropped"] == 6 and m["queue_depth"] == 3  # whole frames, oldest first, all counted
    gate.set()
    await settle(lambda: len(inbound.frames) == 4)
    assert [f.chunk for f in inbound.frames] == [0, 7, 8, 9]
    assert consumer_metrics(router, TRACK_OUTBOUND)["frames_dropped"] == 0
    await router.close("test")


async def test_r07_one_sink_raising_does_not_stop_the_other_track() -> None:
    inbound, outbound = FakeTrackSink(raises=RuntimeError("boom")), FakeTrackSink()
    session, router = make_router(FakeFactory({TRACK_INBOUND: inbound, TRACK_OUTBOUND: outbound}))
    for n in range(5):
        await router.handle_frame(mk(session, "inbound", n))  # the router itself never raises
        await router.handle_frame(mk(session, "outbound", n))
    await settle(lambda: len(outbound.frames) == 5 and consumer_metrics(router, TRACK_INBOUND)["processing_errors"] == 5)
    assert consumer_metrics(router, TRACK_OUTBOUND)["processing_errors"] == 0
    await router.close("test")


async def test_r08_one_stalled_consumer_does_not_block_the_other_track_or_the_caller() -> None:
    gate = asyncio.Event()
    inbound, outbound = FakeTrackSink(gate=gate), FakeTrackSink()
    session, router = make_router(FakeFactory({TRACK_INBOUND: inbound, TRACK_OUTBOUND: outbound}), queue_frames=500)

    async def send_all() -> None:
        for n in range(200):
            await router.handle_frame(mk(session, "inbound", n))
            await router.handle_frame(mk(session, "outbound", n))

    await asyncio.wait_for(send_all(), 1.0)  # handle_frame returns promptly although inbound is stalled
    await settle(lambda: len(outbound.frames) == 200)
    assert len(inbound.frames) == 0
    gate.set()
    await router.close("test")


async def test_r09_per_track_order_is_preserved_with_interleaved_arrival() -> None:
    inbound, outbound = FakeTrackSink(), FakeTrackSink()
    session, router = make_router(FakeFactory({TRACK_INBOUND: inbound, TRACK_OUTBOUND: outbound}))
    for n in range(20):
        await router.handle_frame(mk(session, "outbound" if n % 3 == 0 else "inbound", n))
    expected_out = [n for n in range(20) if n % 3 == 0]
    expected_in = [n for n in range(20) if n % 3 != 0]
    await settle(lambda: len(outbound.frames) == len(expected_out) and len(inbound.frames) == len(expected_in))
    assert [f.chunk for f in outbound.frames] == expected_out and [f.chunk for f in inbound.frames] == expected_in
    await router.close("test")


async def test_r10_repeated_registration_reuses_one_worker_and_one_sink() -> None:
    factory = FakeFactory()
    session, router = make_router(factory)
    await router.handle_frame(mk(session, "inbound", 0))
    first = router.worker(TRACK_INBOUND)
    for batch in range(3):
        await router.handle_frame(mk(session, "inbound", batch + 1))
        assert router.worker(TRACK_INBOUND) is first
    await settle(lambda: TRACK_INBOUND in factory.outcomes and len(factory.outcomes[TRACK_INBOUND].frames) == 4)
    assert factory.tracks_created() == [TRACK_INBOUND]  # one sink, however many frames or repeat registrations
    await router.close("test")


async def test_r11_workers_see_the_physical_stream_id_and_a_neutral_track() -> None:
    factory = FakeFactory()
    session, router = make_router(factory)
    await router.handle_frame(mk(session, "inbound", 0))
    await router.handle_frame(mk(session, "outbound", 0))
    await settle(lambda: len(factory.calls) == 2)
    for view, track in factory.calls:
        assert view.track == track and track in (TRACK_INBOUND, TRACK_OUTBOUND)
        assert view.stream_id == "stream-1" and view.call_id == "call-1"  # physical id, never composite
    await router.close("test")


async def test_r12_single_track_stream_creates_exactly_one_worker() -> None:
    factory = FakeFactory()
    session, router = make_router(factory)
    for n in range(10):
        await router.handle_frame(mk(session, "outbound", n))
    await settle(lambda: TRACK_OUTBOUND in factory.outcomes and len(factory.outcomes[TRACK_OUTBOUND].frames) == 10)
    m = router.metrics()
    assert m["tracks_started"] == 1 and m["active_tracks"] == 1 and list(m["tracks"]) == [TRACK_OUTBOUND]
    await router.close("test")


async def test_r13_every_lost_frame_is_accounted_for() -> None:
    gate = asyncio.Event()
    inbound = FakeTrackSink(gate=gate)
    session, router = make_router(FakeFactory({TRACK_INBOUND: inbound}), queue_frames=2)
    await router.handle_frame(mk(session, "inbound", 0))
    await asyncio.wait_for(inbound.started.wait(), 2)
    for n in range(1, 7):
        await router.handle_frame(mk(session, "inbound", n))  # 6 offered into capacity 2 -> 4 dropped
    for raw in ("weird", None, ""):
        await router.handle_frame(mk(session, raw, 99))
    gate.set()
    await settle(lambda: consumer_metrics(router, TRACK_INBOUND)["frames_processed"] == 3)  # in flight + 2 kept
    m = router.metrics()
    c = consumer_metrics(router, TRACK_INBOUND)
    assert c["frames_dropped"] == 4
    assert sum(m["frames_rejected"].values()) == 3 and m["frames_routed"][TRACK_INBOUND] == 7
    assert c["frames_processed"] + c["frames_dropped"] == m["frames_routed"][TRACK_INBOUND]  # nothing vanished
    await router.close("test")


async def test_r14_handle_frame_never_waits_for_either_track() -> None:
    gate = asyncio.Event()
    a, b = FakeTrackSink(gate=gate), FakeTrackSink(gate=gate)
    session, router = make_router(FakeFactory({TRACK_INBOUND: a, TRACK_OUTBOUND: b}), queue_frames=500)

    async def flood() -> None:
        for n in range(1000):
            await router.handle_frame(mk(session, "inbound", n))
            await router.handle_frame(mk(session, "outbound", n))

    await asyncio.wait_for(flood(), 2.0)  # both tracks stalled; the caller is never held up
    gate.set()
    await router.close("test")


async def test_r15_router_is_an_audio_frame_sink_and_rejects_after_close() -> None:
    factory = FakeFactory()
    session, router = make_router(factory)
    assert isinstance(router, AudioFrameSink)
    await router.handle_frame(mk(session, "inbound", 0))
    await router.close("test")
    await router.handle_frame(mk(session, "inbound", 1))  # no new worker, counted, no raise
    assert rejected(router, REJECT_ROUTER_CLOSED) == 1 and factory.tracks_created() == [TRACK_INBOUND]


async def test_r16_rejection_logs_carry_no_audio(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    session, router = make_router(FakeFactory())
    frame = mk(session, "weird", 1, sig=0x5A)
    await router.handle_frame(frame)
    text = caplog.text
    assert "vobiz_track_frame_rejected" in text and repr(frame.data) not in text
    assert frame.data.hex() not in text
    await router.close("test")


async def test_r17_track_that_never_started_or_ended_rejects_later_frames() -> None:
    inbound = FakeTrackSink()
    session, router = make_router(FakeFactory({TRACK_INBOUND: inbound, TRACK_OUTBOUND: RuntimeError("no sink")}))
    await router.handle_frame(mk(session, "inbound", 0))
    await router.handle_frame(mk(session, "outbound", 0))
    await settle(lambda: router.worker(TRACK_OUTBOUND).consumer.state == FAILED)
    await router.handle_frame(mk(session, "outbound", 1))  # the failed track refuses, the router does not raise
    assert rejected(router, REJECT_TRACK_FAILED) == 1
    assert await router.end_track(TRACK_INBOUND, "done") is True
    await router.handle_frame(mk(session, "inbound", 1))
    assert rejected(router, REJECT_TRACK_ENDED) == 1
    await router.close("test")
