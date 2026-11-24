"""Inbound audio queue consumer (M3).

Unit tests drive InboundAudioConsumer directly on a real StreamSession; integration tests run it
behind the real WebSocket endpoint with a simulated Vobiz client. Sinks here are TEST-ONLY
collectors: production registers no sink until M4. Nothing here proves real-time behaviour on a
real Vobiz call.
"""

from __future__ import annotations

import asyncio
import logging
import os
import struct
import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.vobiz import media_config
from app.integrations.vobiz.stream_sender import VobizStreamSender
from app.main import create_app
from app.services.audio.audio_buffer import AudioFrame, BoundedFrameQueue
from app.services.audio.formats import ENC_L16, ENC_MULAW, AudioFormat
from app.services.audio.synthetic import tone
from app.services.voice.audio_consumer import (
    FAILED,
    RUNNING,
    STOPPED,
    UNAVAILABLE,
    AudioFrameSink,
    InboundAudioConsumer,
)
from app.services.voice.call_manager import CallStateManager, StreamSession
from app.services.voice.playback import PlaybackController
from tests.test_vobiz_websocket import (
    CALL,
    HEADERS,
    STREAM,
    make_settings,
    media_msg,
    open_stream,
    start_msg,
    url,
    wait_until,
)

L16_16K = AudioFormat(ENC_L16, 16000)
MULAW_8K = AudioFormat(ENC_MULAW, 8000)


# --- test-only sinks -------------------------------------------------------------------------


class CollectingSink:
    """Records every frame it is given. Test-only."""

    def __init__(self) -> None:
        self.frames: list[AudioFrame] = []
        self.closed_with: str | None = None
        self.close_calls = 0

    async def handle_frame(self, frame: AudioFrame) -> None:
        self.frames.append(frame)

    async def close(self, reason: str) -> None:
        self.close_calls += 1
        self.closed_with = reason


class GatedSink(CollectingSink):
    """Blocks in handle_frame until released, to model a slow downstream (e.g. STT)."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()

    async def handle_frame(self, frame: AudioFrame) -> None:
        self.entered.set()
        await self.gate.wait()
        await super().handle_frame(frame)


class FailingSink(CollectingSink):
    def __init__(self, fail_chunks: set[int] | None = None, always: bool = False) -> None:
        super().__init__()
        self.fail_chunks = fail_chunks or set()
        self.always = always
        self.calls = 0

    async def handle_frame(self, frame: AudioFrame) -> None:
        self.calls += 1
        if self.always or frame.chunk in self.fail_chunks:
            raise RuntimeError("downstream failure (test)")
        await super().handle_frame(frame)


# --- helpers ---------------------------------------------------------------------------------


async def _noop_send(_: str) -> None:
    return None


def make_session(call_id: str = "call-1", stream_id: str = "stream-1", capacity: int = 500,
                 fmt: AudioFormat = L16_16K) -> StreamSession:
    sender = VobizStreamSender(stream_id, _noop_send)
    return StreamSession(
        call_id=call_id, stream_id=stream_id, account_id="1", tracks=("inbound",), audio_format=fmt,
        queue=BoundedFrameQueue(capacity), sender=sender, playback=PlaybackController(sender),
    )


def frame_for(session: StreamSession, n: int, data: bytes | None = None) -> AudioFrame:
    fmt = session.audio_format
    return AudioFrame(
        data=data if data is not None else bytes([n % 256]) * fmt.bytes_for_ms(20),
        format=fmt, track="inbound", timestamp_ms=n * 20, chunk=n, sequence=n,
        stream_id=session.stream_id, received_at=time.monotonic(),
    )


def produce(session: StreamSession, n: int, start: int = 1) -> None:
    for i in range(start, start + n):
        session.queue.put(frame_for(session, i))
        session.stats.frames_received += 1


async def settle(predicate, timeout: float = 2.0) -> None:
    """Yield to the loop until `predicate()` holds. Event-driven test sync, not a production poll."""
    async def _wait() -> None:
        while not predicate():
            await asyncio.sleep(0)
    await asyncio.wait_for(_wait(), timeout)


def consumer_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t.get_name().startswith("vobiz-audio-consumer:")]


# =============================================================================================
# unit tests
# =============================================================================================


async def test_consumer_starts_with_an_active_stream() -> None:
    session = make_session()
    sink = CollectingSink()
    consumer = session.start_consumer(lambda s: sink)
    await settle(lambda: consumer.state == RUNNING)
    assert consumer.active and consumer.sink_name == "CollectingSink"
    assert consumer.task.get_name() == "vobiz-audio-consumer:stream-1"
    await session.close("test")


async def test_async_sink_factory_is_supported_and_receives_the_session() -> None:
    session = make_session()
    seen: list[StreamSession] = []

    async def factory(s: StreamSession) -> AudioFrameSink:
        seen.append(s)
        return CollectingSink()

    consumer = session.start_consumer(factory)
    await settle(lambda: consumer.state == RUNNING)
    assert seen == [session]
    await session.close("test")


async def test_frames_processed_in_order_and_all_processed_when_sink_keeps_pace() -> None:
    session = make_session()
    sink = CollectingSink()
    consumer = session.start_consumer(lambda s: sink)
    produce(session, 200)
    await settle(lambda: consumer.frames_processed == 200)

    assert [f.chunk for f in sink.frames] == list(range(1, 201))
    m = consumer.metrics()
    assert m["frames_enqueued"] == 200 and m["frames_processed"] == 200
    assert m["frames_dropped"] == 0 and m["processing_errors"] == 0 and m["queue_depth"] == 0
    assert m["consumer_latency_ms"] is not None and m["consumer_latency_max_ms"] >= 0
    await session.close("test")


async def test_queue_depth_decreases_as_frames_are_consumed() -> None:
    session = make_session()
    sink = GatedSink()
    consumer = session.start_consumer(lambda s: sink)
    produce(session, 10)
    await asyncio.wait_for(sink.entered.wait(), 1)
    depth_while_blocked = len(session.queue)
    assert depth_while_blocked == 9  # one frame in flight, nine waiting

    sink.gate.set()
    await settle(lambda: consumer.frames_processed == 10)
    assert len(session.queue) == 0 < depth_while_blocked
    await session.close("test")


async def test_slow_sink_backpressure_is_bounded_and_drops_are_counted() -> None:
    session = make_session(capacity=20)
    sink = GatedSink()
    consumer = session.start_consumer(lambda s: sink)
    produce(session, 1)
    await asyncio.wait_for(sink.entered.wait(), 1)  # consumer is stuck on frame 1

    produce(session, 1000, start=2)  # producer keeps going at full speed
    assert len(session.queue) == 20  # memory bounded by capacity, no secondary buffer
    assert session.queue.dropped == 1000 - 20

    sink.gate.set()
    await settle(lambda: consumer.frames_processed == 21)
    m = consumer.metrics()
    assert m["frames_processed"] == 21  # in-flight frame + the 20 newest
    assert [f.chunk for f in sink.frames] == [1, *range(982, 1002)]  # order kept; oldest dropped
    # every enqueued frame is accounted for: processed + dropped (+ buffered/discarded, here 0)
    assert m["frames_enqueued"] == m["frames_processed"] + m["frames_dropped"] + m["queue_depth"]
    await session.close("test")


async def test_lagging_warning_is_logged_once(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    session = make_session(capacity=10)
    sink = GatedSink()
    session.start_consumer(lambda s: sink)
    produce(session, 1)
    await asyncio.wait_for(sink.entered.wait(), 1)
    produce(session, 9, start=2)
    sink.gate.set()
    await settle(lambda: session.consumer.frames_processed == 10)
    lagging = [r for r in caplog.records if r.getMessage() == "vobiz_audio_consumer_lagging"]
    assert len(lagging) == 1 and lagging[0].queue_capacity == 10
    await session.close("test")


async def test_handler_exceptions_are_recorded_not_retried_and_not_fatal(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    session = make_session()
    sink = FailingSink(fail_chunks={3, 7})
    consumer = session.start_consumer(lambda s: sink)
    produce(session, 10)
    await settle(lambda: sink.calls == 10)

    assert consumer.processing_errors == 2 and consumer.frames_processed == 8
    assert sink.calls == 10  # each frame tried exactly once: no retry loop
    assert [f.chunk for f in sink.frames] == [1, 2, 4, 5, 6, 8, 9, 10]
    assert consumer.state == RUNNING and consumer.active  # still consuming
    errors = [r for r in caplog.records if r.getMessage() == "vobiz_audio_consumer_error"]
    assert errors and errors[0].error == "RuntimeError" and errors[0].phase == "handle_frame"
    assert "downstream failure" not in str(errors[0].__dict__)  # exception text is not logged
    await session.close("test")


async def test_persistent_failure_stops_the_consumer_instead_of_spinning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    session = make_session()
    sink = FailingSink(always=True)
    consumer = InboundAudioConsumer(session, lambda s: sink, max_consecutive_errors=5)
    session.consumer = consumer
    consumer.start()
    produce(session, 50)
    await settle(lambda: not consumer.active)

    assert consumer.state == FAILED and consumer.failure_reason == "too_many_consecutive_errors"
    assert sink.calls == 5 and consumer.frames_processed == 0
    assert len(session.queue) == 45  # remaining frames are NOT consumed or reported processed
    assert any(getattr(r, "phase", None) == "giving_up" for r in caplog.records)
    await session.close("test")
    assert consumer.state == FAILED and sink.close_calls == 1


async def test_failing_sink_factory_marks_consumer_failed(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    session = make_session()

    def broken(_: StreamSession) -> AudioFrameSink:
        raise ConnectionError("cannot reach STT (test)")

    consumer = session.start_consumer(broken)
    produce(session, 5)
    await settle(lambda: not consumer.active)
    assert consumer.state == FAILED and consumer.failure_reason == "sink_factory_failed"
    assert consumer.frames_processed == 0 and len(session.queue) == 5
    record = next(r for r in caplog.records if r.getMessage() == "vobiz_audio_consumer_unavailable")
    assert record.reason == "sink_factory_failed" and record.error == "ConnectionError"
    await session.close("test")


async def test_factory_returning_a_non_sink_is_rejected() -> None:
    session = make_session()
    consumer = session.start_consumer(lambda s: object())
    await settle(lambda: not consumer.active)
    assert consumer.state == FAILED
    await session.close("test")


async def test_missing_handler_is_explicit_and_nothing_is_reported_processed(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    session = make_session()
    consumer = session.start_consumer(None)
    produce(session, 600)

    assert consumer.state == UNAVAILABLE and not consumer.active and consumer.task is None
    m = session.snapshot()["consumer"]
    assert m["consumer_state"] == "unavailable" and m["consumer_active"] is False
    assert m["frames_processed"] == 0 and m["sink"] is None
    assert m["frames_dropped"] == 100 and m["queue_depth"] == 500  # previous diagnostic behaviour kept
    (record,) = [r for r in caplog.records if r.getMessage() == "vobiz_audio_consumer_unavailable"]
    assert record.reason == "no_sink_registered"
    assert consumer_tasks() == []
    await session.close("test")
    assert not any(r.getMessage() == "vobiz_audio_consumer_stopped" for r in caplog.records)


async def test_stream_close_stops_and_awaits_consumer_and_closes_sink(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    session = make_session()
    sink = CollectingSink()
    consumer = session.start_consumer(lambda s: sink)
    produce(session, 3)
    await settle(lambda: consumer.frames_processed == 3)

    await session.close("socket_closed")

    assert consumer.task.done() and not consumer.active and consumer.state == STOPPED
    assert sink.closed_with == "socket_closed" and sink.close_calls == 1
    assert consumer_tasks() == []
    (stopped,) = [r for r in caplog.records if r.getMessage() == "vobiz_audio_consumer_stopped"]
    assert stopped.reason == "socket_closed" and stopped.frames_processed == 3


async def test_cancellation_is_awaited_even_mid_handler() -> None:
    session = make_session()
    sink = GatedSink()  # never released: the consumer is stuck inside handle_frame
    consumer = session.start_consumer(lambda s: sink)
    produce(session, 5)
    await asyncio.wait_for(sink.entered.wait(), 1)

    await asyncio.wait_for(session.close("hangup"), 2)

    assert consumer.task.cancelled() or consumer.task.done()
    assert consumer_tasks() == [] and consumer.frames_processed == 0
    assert session.queue.discarded_on_close == 4  # buffered frames discarded, and counted
    assert sink.close_calls == 1


async def test_stop_is_idempotent_and_safe_before_start() -> None:
    session = make_session()
    consumer = InboundAudioConsumer(session, lambda s: CollectingSink())
    await consumer.stop("early")  # never started
    await consumer.stop("again")
    consumer.start()  # a stopped consumer never starts
    assert consumer.task is None and consumer_tasks() == []


async def test_start_consumer_twice_returns_the_same_single_consumer() -> None:
    session = make_session()
    first = session.start_consumer(lambda s: CollectingSink())
    second = session.start_consumer(lambda s: CollectingSink())
    assert first is second
    await settle(lambda: first.state == RUNNING)
    assert len(consumer_tasks()) == 1
    await session.close("test")


async def test_slow_sink_close_does_not_block_cleanup_forever() -> None:
    class StuckClose(CollectingSink):
        async def close(self, reason: str) -> None:
            await asyncio.Event().wait()

    session = make_session()
    consumer = InboundAudioConsumer(session, lambda s: StuckClose(), sink_close_timeout_s=0.05)
    session.consumer = consumer
    consumer.start()
    await settle(lambda: consumer.state == RUNNING)
    await asyncio.wait_for(session.close("test"), 1)
    assert consumer_tasks() == []


async def test_reconnect_replaces_consumer_without_duplicates() -> None:
    manager = CallStateManager()
    sinks: dict[str, CollectingSink] = {}

    def factory(s: StreamSession) -> CollectingSink:
        sinks[s.stream_id] = CollectingSink()
        return sinks[s.stream_id]

    old = make_session(stream_id="old")
    await manager.attach_stream(old)
    old.start_consumer(factory)
    await settle(lambda: old.consumer.state == RUNNING)

    new = make_session(stream_id="new")
    await manager.attach_stream(new)  # reconnect: same call, new streamId
    new.start_consumer(factory)
    await settle(lambda: new.consumer.state == RUNNING)

    assert old.consumer.state == STOPPED and old.consumer.task.done()
    assert sinks["old"].closed_with == "superseded"
    assert [t.get_name() for t in consumer_tasks()] == ["vobiz-audio-consumer:new"]
    produce(new, 3)
    await settle(lambda: new.consumer.frames_processed == 3)
    assert len(sinks["old"].frames) == 0 and len(sinks["new"].frames) == 3
    await manager.on_hangup("call-1")
    assert consumer_tasks() == []


async def test_simultaneous_calls_are_isolated() -> None:
    sessions = [make_session(f"call-{i}", f"stream-{i}") for i in range(3)]
    sinks = {s.call_id: CollectingSink() for s in sessions}
    for s in sessions:
        s.start_consumer(lambda sess, _sinks=sinks: _sinks[sess.call_id])
    for i, s in enumerate(sessions):
        for n in range(1, 21):
            s.queue.put(frame_for(s, n, data=bytes([i]) * 640))
    await settle(lambda: all(s.consumer.frames_processed == 20 for s in sessions))

    for i, s in enumerate(sessions):
        frames = sinks[s.call_id].frames
        assert all(f.stream_id == s.stream_id and f.data == bytes([i]) * 640 for f in frames)
        assert [f.chunk for f in frames] == list(range(1, 21))
    for s in sessions:
        await s.close("test")
    assert consumer_tasks() == []


async def test_close_all_on_shutdown_leaves_no_tasks() -> None:
    manager = CallStateManager()
    for i in range(3):
        s = make_session(f"c{i}", f"s{i}")
        await manager.attach_stream(s)
        s.start_consumer(lambda sess: GatedSink())  # all stuck mid-frame or idle
        produce(s, 2)
    await settle(lambda: len(consumer_tasks()) == 3)
    assert await manager.close_all("shutdown") == 3
    assert consumer_tasks() == []


async def test_frame_metadata_and_raw_bytes_are_preserved() -> None:
    session = make_session(fmt=L16_16K)
    sink = CollectingSink()
    session.start_consumer(lambda s: sink)
    raw = os.urandom(640)
    session.queue.put(frame_for(session, 1, data=raw))
    await settle(lambda: len(sink.frames) == 1)
    got = sink.frames[0]
    assert got.data == raw  # untouched, not byte-swapped
    assert got.format == AudioFormat("audio/x-l16", 16000) and got.format.label == "audio/x-l16;rate=16000"
    assert (got.track, got.chunk, got.timestamp_ms, got.stream_id) == ("inbound", 1, 20, "stream-1")
    await session.close("test")


# --- centralized media configuration ---------------------------------------------------------


def test_verified_vobiz_media_config() -> None:
    assert media_config.VOBIZ_INBOUND_FORMAT == AudioFormat("audio/x-l16", 16000)
    assert media_config.VOBIZ_INBOUND_CONTENT_TYPE == "audio/x-l16;rate=16000"
    assert media_config.VOBIZ_OUTBOUND_L16_FORMAT == AudioFormat("audio/x-l16", 16000)
    assert media_config.VOBIZ_OUTBOUND_MULAW_FORMAT == AudioFormat("audio/x-mulaw", 8000)
    assert media_config.VOBIZ_INBOUND_L16_BYTE_ORDER == "little"  # verified in M4.2
    assert Settings().vobiz_inbound_content_type == media_config.VOBIZ_INBOUND_CONTENT_TYPE
    assert media_config.VOBIZ_INBOUND_FORMAT.bytes_for_ms(20) == 640  # verified frame size


def test_outbound_l16_is_explicitly_little_endian() -> None:
    assert media_config.VOBIZ_OUTBOUND_L16_BYTE_ORDER == "little"
    assert media_config.outbound_byte_order(media_config.VOBIZ_OUTBOUND_L16_FORMAT) == "little"
    assert media_config.outbound_byte_order(media_config.VOBIZ_OUTBOUND_MULAW_FORMAT) is None
    # The byte order comes from config, not from the host: compare against explicit '<h' packing.
    pcm = tone(media_config.VOBIZ_OUTBOUND_L16_FORMAT, 20, byte_order=media_config.VOBIZ_OUTBOUND_L16_BYTE_ORDER)
    samples = struct.unpack("<320h", pcm)
    assert samples[0] == 0 and samples[1] > 0 and max(samples) > 9000
    assert pcm != tone(media_config.VOBIZ_OUTBOUND_L16_FORMAT, 20, byte_order="big")


def test_test_tone_default_byte_order_comes_from_media_config() -> None:
    from app.api.routes.vobiz_ws import ToneRequest

    assert ToneRequest().byte_order == media_config.VOBIZ_OUTBOUND_L16_BYTE_ORDER


# =============================================================================================
# integration: real WebSocket endpoint, simulated Vobiz client
# =============================================================================================


def sink_app(factory) -> TestClient:
    return TestClient(create_app(make_settings(), audio_sink_factory=factory))


def test_endpoint_consumes_frames_continuously_in_order() -> None:
    sinks: list[CollectingSink] = []

    def factory(s: StreamSession) -> CollectingSink:
        sinks.append(CollectingSink())
        return sinks[-1]

    with sink_app(factory) as client:
        with open_stream(client) as stream:
            session = stream.session
            frames = [os.urandom(640) for _ in range(800)]  # more than the 500-frame queue capacity
            for i, raw in enumerate(frames, start=1):
                stream.ws.send_json(media_msg(raw, chunk=i, seq=i))
            wait_until(lambda: session.consumer.frames_processed == 800)

            assert [f.data for f in sinks[0].frames] == frames  # every frame, in order, raw bytes intact
            listing = client.get("/api/v1/vobiz/streams", headers=HEADERS).json()
            m = listing["calls"][0]["current_stream"]["consumer"]
            assert m["consumer_state"] == "running" and m["consumer_active"] is True
            assert m["frames_processed"] == 800 and m["frames_dropped"] == 0 and m["queue_depth"] == 0
            assert m["sink"] == "CollectingSink"
        wait_until(lambda: session.consumer.state == STOPPED)
        assert session.consumer.task.done() and sinks[0].closed_with == "socket_closed"


def test_endpoint_without_sink_reports_unavailable_and_keeps_old_behaviour() -> None:
    with TestClient(create_app(make_settings())) as client:
        with open_stream(client) as stream:
            for i in range(1, 11):
                stream.ws.send_json(media_msg(b"\x00" * 640, chunk=i, seq=i))
            wait_until(lambda: stream.session.stats.frames_received == 10)
            m = client.get("/api/v1/vobiz/streams", headers=HEADERS).json()["calls"][0]["current_stream"]
            assert m["consumer"]["consumer_state"] == "unavailable"
            assert m["consumer"]["frames_processed"] == 0
            assert m["queue"]["buffered"] == 10  # frames still accumulate, as before


def test_endpoint_disconnect_stops_consumer_and_stream_ended_reports_it(caplog: pytest.LogCaptureFixture) -> None:
    sink = CollectingSink()
    with sink_app(lambda s: sink) as client:
        logging.getLogger().addHandler(caplog.handler)
        caplog.set_level(logging.DEBUG)
        with open_stream(client) as stream:
            session = stream.session
            for i in range(1, 6):
                stream.ws.send_json(media_msg(b"\x01\x00" * 320, chunk=i, seq=i))
            wait_until(lambda: session.consumer.frames_processed == 5)
        wait_until(lambda: any(r.getMessage() == "vobiz_stream_ended" for r in caplog.records))
        assert session.consumer.task.done() and session.consumer.state == STOPPED

    ended = next(r for r in caplog.records if r.getMessage() == "vobiz_stream_ended")
    assert ended.consumer_state == "stopped" and ended.frames_processed == 5 and ended.processing_errors == 0
    started = next(r for r in caplog.records if r.getMessage() == "vobiz_audio_consumer_started")
    assert started.call_id == CALL and started.stream_id == STREAM and started.sink == "CollectingSink"
    assert started.audio_format == "audio/x-l16;rate=16000"


def test_endpoint_hangup_cancels_consumer_stuck_in_sink() -> None:
    class Stuck(CollectingSink):
        async def handle_frame(self, frame: AudioFrame) -> None:
            await asyncio.Event().wait()

    with sink_app(lambda s: Stuck()) as client:
        with open_stream(client) as stream:
            session = stream.session
            stream.ws.send_json(media_msg(b"\x00" * 640))
            wait_until(lambda: session.consumer.state == RUNNING and len(session.queue) == 0)
            form = {"CallUUID": CALL, "Direction": "outbound", "Event": "Hangup"}
            assert client.post("/api/v1/vobiz/hangup", data=form).status_code == 200
            assert session.consumer.task.done() and session.consumer.state == STOPPED


def test_endpoint_sink_errors_do_not_break_the_websocket() -> None:
    sink = FailingSink(fail_chunks={2})
    with sink_app(lambda s: sink) as client:
        with open_stream(client) as stream:
            for i in range(1, 6):
                stream.ws.send_json(media_msg(b"\x00" * 640, chunk=i, seq=i))
            wait_until(lambda: sink.calls == 5)
            assert stream.session.consumer.processing_errors == 1
            assert stream.session.consumer.frames_processed == 4
            # the socket still works for control traffic
            assert client.post(f"/api/v1/vobiz/streams/{CALL}/test-tone", json={"duration_ms": 100},
                               headers=HEADERS).status_code == 200


def test_endpoint_two_calls_have_isolated_consumers() -> None:
    sinks: dict[str, CollectingSink] = {}

    def factory(s: StreamSession) -> CollectingSink:
        sinks[s.call_id] = CollectingSink()
        return sinks[s.call_id]

    with sink_app(factory) as client:
        with open_stream(client, "call-a", "stream-a") as a, open_stream(client, "call-b", "stream-b") as b:
            for i in range(1, 11):
                a.ws.send_json(media_msg(b"\xaa\xaa" * 320, stream="stream-a", chunk=i, seq=i))
                b.ws.send_json(media_msg(b"\xbb\xbb" * 320, stream="stream-b", chunk=i, seq=i))
            wait_until(lambda: a.session.consumer.frames_processed == 10 and b.session.consumer.frames_processed == 10)
    assert all(f.data[:1] == b"\xaa" and f.stream_id == "stream-a" for f in sinks["call-a"].frames)
    assert all(f.data[:1] == b"\xbb" and f.stream_id == "stream-b" for f in sinks["call-b"].frames)


def test_app_shutdown_closes_live_streams_and_consumers() -> None:
    sink = CollectingSink()
    client = sink_app(lambda s: sink)
    client.__enter__()
    ws_cm = client.websocket_connect(url())
    ws = ws_cm.__enter__()
    ws.send_json(start_msg())
    session = wait_until(lambda: client.app.state.call_manager.current_stream(CALL))
    wait_until(lambda: session.consumer.state == RUNNING)
    ws_cm.__exit__(None, None, None)
    client.__exit__(None, None, None)  # lifespan shutdown runs close_all
    assert session.closed and session.consumer.task.done() and sink.close_calls == 1
