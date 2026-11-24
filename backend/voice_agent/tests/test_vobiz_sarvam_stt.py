"""M4.3: Vobiz inbound audio -> Sarvam realtime STT. MOCKED Sarvam (scripted fake WebSocket) and
simulated Vobiz streams. These tests do not prove live behaviour; see the real-call test steps.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.integrations.vobiz import media_config
from app.integrations.vobiz.stream_sender import VobizStreamSender
from app.main import create_app
from app.publishers.transcripts import LoggingTranscriptPublisher, TranscriptEvent
from app.services.audio.audio_buffer import AudioFrame, BoundedFrameQueue
from app.services.audio.byte_order import swap_l16
from app.services.audio.formats import ENC_L16, ENC_MULAW, AudioFormat
from app.services.sarvam.errors import SarvamConfigError
from app.services.sarvam.realtime_stt import SarvamRealtimeSttClient
from app.services.voice import sarvam_stt_sink as sink_module
from app.services.voice.audio_consumer import FAILED, RUNNING, STOPPED
from app.services.voice.call_manager import CallStateManager, StreamSession, wait_for_pending_cleanups
from app.services.voice.playback import PlaybackController
from app.services.voice.sarvam_stt_sink import SarvamSttSink, SarvamSttSinkFactory, SarvamSttUnavailable
from tests.test_sarvam_realtime_stt import FakeWS, Sleeps, invalid_status, session_begin
from tests.test_vobiz_websocket import CALL, HEADERS, STREAM, make_settings, media_msg, open_stream, wait_until

KEY = "sk-test-SECRET-sarvam-key"
L16_16K = AudioFormat(ENC_L16, 16000)
SPOKEN = "My internet connection is not working"


# --- fake Sarvam -----------------------------------------------------------------------------


class FakeSarvam:
    """One FakeWS per connection. Answers like Sarvam: VAD + partial after `partial_after` chunks,
    final + session.end + close on {"event":"end"}."""

    def __init__(self, *, partial_after: int = 1, final_on_end: bool = True, session_end: bool = True,
                 text: str = SPOKEN, outcomes: list | None = None) -> None:
        self.partial_after, self.final_on_end, self.session_end, self.text = partial_after, final_on_end, session_end, text
        self.outcomes = list(outcomes or [])  # exceptions to raise on the next connects
        self.sockets: list[FakeWS] = []
        self.connects = 0

    def connect(self, url, **kwargs):
        self.connects += 1
        outcome = self.outcomes.pop(0) if self.outcomes else None
        ws = None
        if outcome is None:
            ws = FakeWS([session_begin(f"req-{len(self.sockets) + 1}")], on_send=self._on_send)
            ws.request_id = f"req-{len(self.sockets) + 1}"
            ws.audio = []
            ws.url, ws.headers = url, kwargs.get("additional_headers")
            self.sockets.append(ws)

        async def _open():
            if isinstance(outcome, BaseException):
                raise outcome
            return ws

        return _open()

    def _on_send(self, ws: FakeWS, message: dict) -> None:
        if message["event"] == "audio_input":
            ws.audio.append(base64.b64decode(message["audio"]))
            if len(ws.audio) == self.partial_after:
                ws.push({"event": "vad.speech_start", "utterance_idx": 0})
                ws.push({"event": "transcript.partial", "utterance_idx": 0, "text": self.text[:8], "language": "en-IN"})
        elif message["event"] == "end":
            ws.push({"event": "vad.speech_end", "utterance_idx": 0})
            if self.final_on_end:
                ws.push({"event": "transcript.final", "utterance_idx": 0, "text": self.text, "language": "en-IN",
                         "language_confidence": "0.95", "start_s": "0.1", "end_s": "1.9"})
            if self.session_end:
                ws.push({"event": "session.end", "request_id": ws.request_id, "audio_duration_s": "1.0",
                         "total_utterances": 1})
                ws.push_close(1000, "")

    def client_factory(self, cfg, key, *, on_event):
        return SarvamRealtimeSttClient(cfg, key, on_event=on_event, connect_fn=self.connect, sleep=Sleeps())


class Collector:
    def __init__(self) -> None:
        self.events: list[TranscriptEvent] = []

    async def publish(self, event: TranscriptEvent) -> None:
        self.events.append(event)

    def of(self, call_id: str) -> list[TranscriptEvent]:
        return [e for e in self.events if e.call_id == call_id]


def stt_settings(**overrides) -> Settings:
    values = dict(sarvam_api_key=SecretStr(KEY), vobiz_sarvam_stt_finalize_timeout_s=0.5)
    values.update(overrides)
    return make_settings(**values)


def factory_for(fake: FakeSarvam, publisher=None, **settings) -> SarvamSttSinkFactory:
    return SarvamSttSinkFactory(stt_settings(**settings), publisher or Collector(), client_factory=fake.client_factory)


async def _noop(_: str) -> None:
    return None


def make_session(call_id: str = "call-1", stream_id: str = "stream-1", fmt: AudioFormat = L16_16K,
                 capacity: int = 500) -> StreamSession:
    sender = VobizStreamSender(stream_id, _noop)
    return StreamSession(call_id=call_id, stream_id=stream_id, account_id="1", tracks=("inbound",),
                         audio_format=fmt, queue=BoundedFrameQueue(capacity), sender=sender,
                         playback=PlaybackController(sender))


def frame(session: StreamSession, n: int, data: bytes) -> AudioFrame:
    return AudioFrame(data=data, format=session.audio_format, track="inbound", timestamp_ms=n * 20, chunk=n,
                      sequence=n, stream_id=session.stream_id, received_at=time.monotonic())


def produce(session: StreamSession, frames: list[bytes]) -> None:
    for i, data in enumerate(frames, start=1):
        session.queue.put(frame(session, i, data))
        session.stats.frames_received += 1


async def settle(predicate, timeout: float = 3.0) -> None:
    async def _wait() -> None:
        while not predicate():
            await asyncio.sleep(0.002)
    await asyncio.wait_for(_wait(), timeout)


def frames_of(n: int) -> list[bytes]:
    return [bytes([i % 256, (i * 7) % 256]) * 320 for i in range(1, n + 1)]  # distinct 640-byte frames


async def start(session: StreamSession, factory: SarvamSttSinkFactory) -> SarvamSttSink:
    consumer = session.start_consumer(factory, drain_timeout_s=0.5)
    await settle(lambda: consumer.state == RUNNING)
    return consumer._sink


# =============================================================================================
# audio forwarding
# =============================================================================================


async def test_five_vobiz_frames_become_one_3200_byte_sarvam_chunk() -> None:
    fake = FakeSarvam()
    session = make_session()
    sink = await start(session, factory_for(fake))
    frames = frames_of(5)
    produce(session, frames)
    await settle(lambda: len(fake.sockets[0].audio) == 1)
    (chunk,) = fake.sockets[0].audio
    assert len(chunk) == 3200 and chunk == b"".join(frames)  # exact bytes, in order
    assert sink.metrics()["audio_chunks_sent"] == 1 and sink.metrics()["audio_frames_forwarded"] == 5
    await session.close("test")


async def test_multiple_chunks_in_order_and_remainder_flushed_before_end() -> None:
    fake = FakeSarvam()
    session = make_session()
    await start(session, factory_for(fake))
    frames = frames_of(12)  # 2 full chunks + 2 frames (1,280 bytes)
    produce(session, frames)
    await settle(lambda: session.consumer.frames_processed == 12)
    await session.close("socket_closed")

    ws = fake.sockets[0]
    assert [len(c) for c in ws.audio] == [3200, 3200, 1280]
    assert b"".join(ws.audio) == b"".join(frames)  # nothing lost, nothing reordered
    events = [json.loads(t)["event"] for t in ws.sent]
    assert events == ["audio_input", "audio_input", "audio_input", "end"]  # remainder flushed BEFORE end


async def test_audio_payload_is_base64_of_unchanged_little_endian_bytes() -> None:
    fake = FakeSarvam()
    session = make_session()
    await start(session, factory_for(fake))
    frames = [os.urandom(640) for _ in range(5)]
    produce(session, frames)
    await settle(lambda: len(fake.sockets[0].sent) == 1)
    message = json.loads(fake.sockets[0].sent[0])
    assert set(message) == {"event", "audio"} and message["event"] == "audio_input"
    assert base64.b64decode(message["audio"], validate=True) == b"".join(frames)
    assert media_config.VOBIZ_INBOUND_L16_BYTE_ORDER == "little"  # the reason no swap happens
    await session.close("test")


async def test_byte_order_is_applied_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # If the inbound order were big-endian, the sink (the only place it is applied) swaps ONCE.
    monkeypatch.setattr(sink_module, "inbound_byte_order", lambda fmt: "big")
    fake = FakeSarvam()
    session = make_session()
    await start(session, factory_for(fake))
    frames = [os.urandom(640) for _ in range(5)]
    produce(session, frames)
    await settle(lambda: len(fake.sockets[0].audio) == 1)
    assert fake.sockets[0].audio[0] == swap_l16(b"".join(frames))  # swapped once, not twice
    await session.close("test")


async def test_mismatched_stream_format_is_refused_not_converted() -> None:
    fake = FakeSarvam()
    session = make_session(fmt=AudioFormat(ENC_MULAW, 8000))  # Sarvam configured linear16 @ 16 kHz
    consumer = session.start_consumer(factory_for(fake))
    await settle(lambda: not consumer.active)
    assert consumer.state == FAILED and consumer.failure_reason == "sink_factory_failed"
    assert fake.connects == 0  # refused before any Sarvam connection
    await session.close("test")


# =============================================================================================
# transcript events
# =============================================================================================


async def test_partial_and_final_events_are_published_in_order_for_the_call() -> None:
    fake, pub = FakeSarvam(), Collector()
    session = make_session("call-A", "stream-A")
    await start(session, factory_for(fake, pub))
    produce(session, frames_of(10))
    await settle(lambda: any(e.is_partial for e in pub.events))
    await session.close("socket_closed")

    types = [e.type for e in pub.events]
    assert types == ["session.begin", "vad.speech_start", "transcript.partial", "vad.speech_end",
                     "transcript.final", "session.end"]
    assert [e.sequence for e in pub.events] == list(range(1, 7))  # order preserved
    assert all(e.call_id == "call-A" and e.stream_id == "stream-A" for e in pub.events)
    partial, final = pub.events[2], pub.events[4]
    assert partial.is_partial and not partial.is_final
    assert final.is_final and not final.is_partial and final.text == SPOKEN
    assert final.language == "en-IN" and final.language_confidence == 0.95 and final.start_s == 0.1
    assert final.stt_request_id == "req-1" and final.speaker == "customer" and final.source == "sarvam"
    assert SPOKEN not in repr(final)


async def test_error_events_are_published_and_fatal_marks_the_session_failed() -> None:
    fake, pub = FakeSarvam(), Collector()
    session = make_session()
    sink = await start(session, factory_for(fake, pub))
    ws = fake.sockets[0]
    ws.push({"event": "error", "code": "chunk_too_large", "is_fatal": False, "message": "x"})
    await settle(lambda: sink.metrics()["stt_errors"] == 1)
    assert sink.metrics()["stt_state"] == "open"
    ws.push({"event": "error", "code": "invalid_subscription_key", "is_fatal": True, "message": "y"})
    ws.push_close(1003, "")
    await settle(lambda: sink.metrics()["stt_fatal_errors"] == 1)
    errors = [e for e in pub.events if e.type == "error"]
    assert [(e.error_code, e.error_is_fatal) for e in errors] == [("chunk_too_large", False),
                                                                  ("invalid_subscription_key", True)]
    await session.close("test")


async def test_two_simultaneous_calls_never_cross() -> None:
    fake, pub = FakeSarvam(), Collector()
    factory = factory_for(fake, pub)
    a, b = make_session("call-A", "stream-A"), make_session("call-B", "stream-B")
    await start(a, factory)
    await start(b, factory)
    assert len(fake.sockets) == 2  # one Sarvam session each, never shared
    produce(a, [b"\xaa\x00" * 320] * 5)
    produce(b, [b"\xbb\x00" * 320] * 5)
    await settle(lambda: all(len(ws.audio) == 1 for ws in fake.sockets))
    await asyncio.gather(a.close("x"), b.close("y"))

    audio_by_socket = {ws.audio[0][:1] for ws in fake.sockets}
    assert audio_by_socket == {b"\xaa", b"\xbb"}
    assert all(set(ws.audio[0][::2]) == {ws.audio[0][0]} for ws in fake.sockets)  # no mixing inside a socket
    for call_id, request_id in (("call-A", None), ("call-B", None)):
        events = pub.of(call_id)
        assert events and all(e.stream_id == ("stream-A" if call_id == "call-A" else "stream-B") for e in events)
        assert len({e.stt_request_id for e in events if e.stt_request_id}) == 1
    assert {pub.of("call-A")[0].stt_request_id, pub.of("call-B")[0].stt_request_id} == {"req-1", "req-2"}


async def test_transcript_text_never_reaches_ordinary_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    secret_text = "my account PIN is 4321 and my number is 01711"
    fake = FakeSarvam(text=secret_text)
    session = make_session()
    frames = [os.urandom(640) for _ in range(10)]
    await start(session, SarvamSttSinkFactory(stt_settings(), LoggingTranscriptPublisher(),
                                              client_factory=fake.client_factory))
    produce(session, frames)
    await settle(lambda: session.consumer.frames_processed == 10)
    await session.close("socket_closed")
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for forbidden in (secret_text, "4321", "01711", secret_text[:8], KEY, base64.b64encode(b"".join(frames[:5])).decode()):
        assert forbidden not in text
    final = next(r for r in caplog.records if r.getMessage() == "vobiz_transcript_event" and r.event_type == "transcript.final")
    assert final.text_length == len(secret_text) and final.call_id == "call-1"


# =============================================================================================
# lifecycle
# =============================================================================================


async def test_connection_starts_with_the_stream_and_one_session_per_stream() -> None:
    fake = FakeSarvam()
    factory = factory_for(fake)
    assert fake.connects == 0  # creating the factory connects nothing
    session = make_session()
    sink = await start(session, factory)
    assert fake.connects == 1 and len(fake.sockets) == 1
    assert fake.sockets[0].headers == {"api-subscription-key": KEY} and KEY not in fake.sockets[0].url
    assert sink.metrics()["stt_state"] == "open" and sink.metrics()["stt_request_id"] == "req-1"
    session.start_consumer(factory)  # a second start returns the same consumer: no second session
    await asyncio.sleep(0.02)
    assert fake.connects == 1
    await session.close("test")


async def test_reconnected_stream_gets_a_fresh_session_and_the_old_one_is_finalized() -> None:
    fake, pub = FakeSarvam(), Collector()
    factory = factory_for(fake, pub)
    manager = CallStateManager()
    old, new = make_session(stream_id="s-old"), make_session(stream_id="s-new")
    await manager.attach_stream(old)
    await start(old, factory)
    produce(old, frames_of(5))
    await settle(lambda: old.consumer.frames_processed == 5)
    await manager.attach_stream(new)  # Vobiz reconnect: same call, new stream
    await start(new, factory)
    assert len(fake.sockets) == 2 and old.consumer.state == STOPPED
    assert json.loads(fake.sockets[0].sent[-1]) == {"event": "end"} and fake.sockets[0].closed
    assert not fake.sockets[1].closed
    assert {e.stream_id for e in pub.events if e.type == "session.end"} == {"s-old"}
    await manager.on_hangup("call-1")


async def test_normal_termination_finalizes_and_reports_metrics() -> None:
    fake = FakeSarvam()
    session = make_session()
    sink = await start(session, factory_for(fake))
    produce(session, frames_of(7))
    await settle(lambda: session.consumer.frames_processed == 7)
    await session.close("socket_closed")
    m = sink.metrics()
    assert m["stt_state"] == "closed" and m["session_end_received"] is True and m["finalize_timed_out"] is False
    assert m["finalize_timeouts"] == 0 and m["finalize_ms"] is not None and m["billed_audio_s"] == 1.0
    assert m["transcript_partials"] == 1 and m["transcript_finals"] == 1
    assert m["audio_chunks_sent"] == 2 and m["audio_bytes_sent"] == 7 * 640
    assert m["first_partial_ms"] is not None and m["close_reason"] == "socket_closed"
    assert fake.sockets[0].closed


async def test_finalization_timeout_does_not_block_cleanup() -> None:
    fake = FakeSarvam(final_on_end=False, session_end=False)  # Sarvam never finishes
    session = make_session()
    sink = await start(session, factory_for(fake, vobiz_sarvam_stt_finalize_timeout_s=0.2))
    produce(session, frames_of(5))
    await settle(lambda: session.consumer.frames_processed == 5)
    t0 = time.monotonic()
    await asyncio.wait_for(session.close("hangup"), 3)
    assert time.monotonic() - t0 < 1.5  # bounded by the finalize budget (+ margin)
    m = sink.metrics()
    assert m["finalize_timed_out"] is True and m["finalize_timeouts"] == 1 and m["stt_state"] == "closed"
    assert fake.sockets[0].closed


async def test_queued_frames_get_a_bounded_drain_before_finalization() -> None:
    gate = asyncio.Event()
    fake = FakeSarvam()
    session = make_session()
    sink = await start(session, factory_for(fake))
    real_send = sink._client.send_audio

    async def slow_send(pcm: bytes) -> int:  # downstream is momentarily slower than real time
        await gate.wait()
        return await real_send(pcm)

    sink._client.send_audio = slow_send
    produce(session, frames_of(10))
    await settle(lambda: len(session.queue) == 9)  # 1 in flight, 9 buffered
    asyncio.get_running_loop().call_later(0.05, gate.set)  # recovers within the drain window
    await session.close("socket_closed")
    assert session.consumer.frames_processed == 10 and session.queue.discarded_on_close == 0
    assert session.consumer.drained_completely is True and session.consumer.frames_buffered_at_stop == 9
    assert [len(c) for c in fake.sockets[0].audio] == [3200, 3200]


async def test_frames_that_cannot_drain_in_time_are_discarded_and_counted() -> None:
    fake = FakeSarvam()
    session = make_session()
    sink = await start(session, factory_for(fake))

    async def stuck(pcm: bytes) -> int:
        await asyncio.Event().wait()
        return 0

    sink._client.send_audio = stuck
    produce(session, frames_of(10))
    await settle(lambda: len(session.queue) == 9)
    t0 = time.monotonic()
    await session.close("socket_closed")
    assert time.monotonic() - t0 < 2.5  # drain (0.5 s) + finalize budget, never unbounded
    assert session.consumer.drained_completely is False
    assert session.queue.discarded_on_close == 9 and session.consumer.frames_processed == 0
    assert fake.sockets[0].closed


async def test_repeated_close_is_safe() -> None:
    fake = FakeSarvam()
    session = make_session()
    sink = await start(session, factory_for(fake))
    await asyncio.gather(session.close("a"), session.close("b"))
    await session.close("c")
    await sink.close("again")
    assert sink.metrics()["close_reason"] == "a" and [json.loads(t)["event"] for t in fake.sockets[0].sent] == ["end"]


async def test_call_with_no_speech_closes_cleanly() -> None:
    fake = FakeSarvam(final_on_end=False)
    session = make_session()
    sink = await start(session, factory_for(fake))
    produce(session, [b"\x00\x00" * 320] * 25)
    await settle(lambda: session.consumer.frames_processed == 25)
    await session.close("socket_closed")
    m = sink.metrics()
    assert m["transcript_finals"] == 0 and m["session_end_received"] is True and m["stt_state"] == "closed"


async def test_cleanup_survives_cancellation_of_the_caller() -> None:
    fake, pub = FakeSarvam(), Collector()
    session = make_session()
    sink = await start(session, factory_for(fake, pub))
    produce(session, frames_of(7))
    await settle(lambda: session.consumer.frames_processed == 7)
    closer = asyncio.create_task(session.close("socket_closed"))
    await asyncio.sleep(0)
    closer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closer
    await wait_for_pending_cleanups(3)
    assert sink.metrics()["stt_state"] == "closed" and any(e.is_final for e in pub.events)
    assert [len(c) for c in fake.sockets[0].audio] == [3200, 1280]


async def test_close_all_on_shutdown_finalizes_every_session() -> None:
    fake = FakeSarvam()
    factory = factory_for(fake)
    manager = CallStateManager()
    sessions = [make_session(f"c{i}", f"s{i}") for i in range(3)]
    for s in sessions:
        await manager.attach_stream(s)
        await start(s, factory)
        produce(s, frames_of(5))
    await settle(lambda: all(s.consumer.frames_processed == 5 for s in sessions))
    assert await manager.close_all("shutdown") == 3
    assert all(ws.closed for ws in fake.sockets) and all(s.consumer.state == STOPPED for s in sessions)
    assert all(json.loads(ws.sent[-1]) == {"event": "end"} for ws in fake.sockets)


# =============================================================================================
# failures
# =============================================================================================


async def test_sarvam_connection_failure_marks_only_that_stream_failed() -> None:
    fake = FakeSarvam(outcomes=[OSError("dns")] * 10)
    session = make_session()
    consumer = session.start_consumer(factory_for(fake))
    await settle(lambda: not consumer.active)
    assert consumer.state == FAILED and consumer.failure_reason == "sink_factory_failed"
    assert fake.connects == 4  # 1 + the client's 3 bounded retries, then stop
    produce(session, frames_of(5))
    assert consumer.frames_processed == 0  # nothing reported processed
    await session.close("test")


async def test_sarvam_auth_failure_is_not_retried() -> None:
    fake = FakeSarvam(outcomes=[invalid_status(403)])
    session = make_session()
    consumer = session.start_consumer(factory_for(fake))
    await settle(lambda: not consumer.active)
    assert consumer.state == FAILED and fake.connects == 1
    await session.close("test")


async def test_audio_send_failure_is_counted_and_bounded() -> None:
    fake = FakeSarvam()
    session = make_session()
    sink = await start(session, factory_for(fake))
    fake.sockets[0].closed = True  # the Sarvam socket dies: sends now fail
    produce(session, frames_of(5 * 60))
    await settle(lambda: not session.consumer.active)
    assert session.consumer.state == FAILED and session.consumer.failure_reason == "too_many_consecutive_errors"
    assert session.consumer.frames_processed == 4  # the first 4 only reached the chunker, the 5th send failed
    m = sink.metrics()
    assert m["send_errors"] == 50 and m["stt_state"] == "failed"  # every frame after the gap fails loudly
    assert session.consumer.processing_errors == 50
    await session.close("test")


async def test_receiver_failure_makes_further_frames_fail_loudly() -> None:
    fake = FakeSarvam()
    session = make_session()
    sink = await start(session, factory_for(fake))
    for _ in range(60):
        fake.sockets[0].push("garbage")  # receiver gives up after 50 malformed messages
    await settle(lambda: sink._client.state == "failed")
    with pytest.raises(SarvamSttUnavailable):
        await sink.handle_frame(frame(session, 1, b"\x00" * 640))
    await session.close("test")
    assert sink.metrics()["stt_state"] == "closed"


async def test_one_call_failing_does_not_affect_another() -> None:
    fake, pub = FakeSarvam(outcomes=[None, invalid_status(403)]), Collector()
    factory = factory_for(fake, pub)
    good, bad = make_session("good", "s-good"), make_session("bad", "s-bad")
    await start(good, factory)
    bad_consumer = bad.start_consumer(factory)
    await settle(lambda: not bad_consumer.active)
    assert bad_consumer.state == FAILED
    produce(good, frames_of(10))
    await settle(lambda: good.consumer.frames_processed == 10)
    await good.close("socket_closed")
    await bad.close("socket_closed")
    assert any(e.is_final and e.call_id == "good" for e in pub.events)
    assert not pub.of("bad")


async def test_publisher_failure_never_breaks_stt() -> None:
    class Broken:
        async def publish(self, event) -> None:
            raise RuntimeError("downstream down")

    fake = FakeSarvam()
    session = make_session()
    sink = await start(session, factory_for(fake, Broken()))
    produce(session, frames_of(10))
    await settle(lambda: session.consumer.frames_processed == 10)
    await session.close("socket_closed")
    m = sink.metrics()
    assert m["publisher_errors"] >= 3 and m["transcript_finals"] == 1 and m["session_end_received"] is True


# =============================================================================================
# configuration and wiring
# =============================================================================================


def test_feature_is_disabled_by_default_and_connects_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list = []
    monkeypatch.setattr("app.services.sarvam.realtime_stt.ws_connect", lambda *a, **k: called.append(a))
    assert Settings().vobiz_sarvam_stt_enabled is False
    app = create_app(make_settings(sarvam_api_key=SecretStr(KEY)))
    assert app.state.stt_sink_factory is None and app.state.stream_handler.audio_sink_factory is None
    with TestClient(app) as client:
        with open_stream(client) as stream:
            stream.ws.send_json(media_msg(b"\x00" * 640))
            wait_until(lambda: stream.session.stats.frames_received == 1)
            assert stream.session.consumer.state == "unavailable"
    assert called == []


def test_enabled_without_api_key_fails_fast_without_leaking() -> None:
    with pytest.raises(SarvamConfigError) as info:
        create_app(make_settings(vobiz_sarvam_stt_enabled=True, sarvam_api_key=SecretStr("")))
    assert "SARVAM_API_KEY" in str(info.value)


def test_enabled_with_invalid_realtime_settings_fails_fast() -> None:
    with pytest.raises(SarvamConfigError) as info:
        create_app(make_settings(vobiz_sarvam_stt_enabled=True, sarvam_api_key=SecretStr(KEY),
                                 sarvam_stt_sample_rate=44100))
    assert KEY not in str(info.value)


@pytest.mark.parametrize("timeout", [0, -1, 11])
def test_finalize_timeout_is_bounded(timeout: float) -> None:
    with pytest.raises(SarvamConfigError):
        SarvamSttSinkFactory(stt_settings(vobiz_sarvam_stt_finalize_timeout_s=timeout))


def test_stt_and_dev_capture_cannot_both_be_enabled() -> None:
    with pytest.raises(SarvamConfigError):
        create_app(make_settings(vobiz_sarvam_stt_enabled=True, sarvam_api_key=SecretStr(KEY),
                                 vobiz_dev_capture_enabled=True, app_env="development",
                                 vobiz_dev_capture_destination="+919876543210"))


def test_enabled_feature_installs_the_sarvam_factory_without_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list = []
    monkeypatch.setattr("app.services.sarvam.realtime_stt.ws_connect", lambda *a, **k: called.append(a))
    app = create_app(make_settings(vobiz_sarvam_stt_enabled=True, sarvam_api_key=SecretStr(KEY)))
    assert isinstance(app.state.stream_handler.audio_sink_factory, SarvamSttSinkFactory)
    assert called == []  # no connection until a stream starts
    with TestClient(app) as client:
        warnings = " ".join(client.get("/api/vobiz/diagnostics").json()["warnings"])
    assert "VOBIZ_SARVAM_STT_ENABLED" in warnings


# =============================================================================================
# end to end through the real Vobiz endpoint (simulated Vobiz, fake Sarvam)
# =============================================================================================


def e2e_app(fake: FakeSarvam, pub: Collector, **settings) -> TestClient:
    s = stt_settings(vobiz_sarvam_stt_enabled=True, **settings)
    return TestClient(create_app(s, audio_sink_factory=SarvamSttSinkFactory(s, pub, client_factory=fake.client_factory)))


def test_endpoint_stream_to_transcript_with_metrics() -> None:
    fake, pub = FakeSarvam(), Collector()
    frames = [os.urandom(640) for _ in range(23)]  # 4 full chunks + 3 frames
    with e2e_app(fake, pub) as client:
        with open_stream(client) as stream:
            session = stream.session
            wait_until(lambda: session.consumer.state == RUNNING)
            for i, data in enumerate(frames, start=1):
                stream.ws.send_json(media_msg(data, chunk=i, seq=i))
            wait_until(lambda: session.consumer.frames_processed == 23)
            listing = client.get("/api/vobiz/streams", headers=HEADERS).json()
            stt = listing["calls"][0]["current_stream"]["stt"]
            assert stt["stt_state"] == "open" and stt["audio_chunks_sent"] == 4 and stt["transcript_partials"] == 1
            assert SPOKEN not in json.dumps(listing) and KEY not in json.dumps(listing)
        wait_until(lambda: session.consumer.state == STOPPED)
    ws = fake.sockets[0]
    assert [len(c) for c in ws.audio] == [3200] * 4 + [1920]
    assert b"".join(ws.audio) == b"".join(frames)
    finals = [e for e in pub.events if e.is_final]
    assert len(finals) == 1 and finals[0].call_id == CALL and finals[0].stream_id == STREAM
    m = session.consumer.metrics()
    assert m["frames_received"] == 23 and m["frames_processed"] == 23
    assert m["frames_dropped"] == 0 and m["frames_discarded_on_close"] == 0


def test_endpoint_hangup_triggers_finalization() -> None:
    fake, pub = FakeSarvam(), Collector()
    with e2e_app(fake, pub) as client:
        with open_stream(client) as stream:
            session = stream.session
            wait_until(lambda: session.consumer.state == RUNNING)
            for i in range(1, 8):
                stream.ws.send_json(media_msg(b"\x01\x00" * 320, chunk=i, seq=i))
            wait_until(lambda: session.consumer.frames_processed == 7)
            form = {"CallUUID": CALL, "Direction": "outbound", "Event": "Hangup"}
            assert client.post("/api/vobiz/hangup", data=form).status_code == 200
            assert session.consumer.state == STOPPED
    assert any(e.is_final for e in pub.events) and fake.sockets[0].closed
    assert [json.loads(t)["event"] for t in fake.sockets[0].sent][-1] == "end"


def test_endpoint_two_calls_get_independent_sessions() -> None:
    fake, pub = FakeSarvam(), Collector()
    with e2e_app(fake, pub) as client:
        with open_stream(client, "call-a", "stream-a") as a, open_stream(client, "call-b", "stream-b") as b:
            for s in (a.session, b.session):
                wait_until(lambda s=s: s.consumer.state == RUNNING)
            for i in range(1, 6):
                a.ws.send_json(media_msg(b"\xaa\x00" * 320, stream="stream-a", chunk=i, seq=i))
                b.ws.send_json(media_msg(b"\xbb\x00" * 320, stream="stream-b", chunk=i, seq=i))
            wait_until(lambda: a.session.consumer.frames_processed == 5 and b.session.consumer.frames_processed == 5)
    assert len(fake.sockets) == 2
    assert {ws.audio[0][:1] for ws in fake.sockets} == {b"\xaa", b"\xbb"}
    assert {e.call_id for e in pub.events if e.is_final} == {"call-a", "call-b"}


def test_endpoint_app_shutdown_releases_sessions() -> None:
    fake, pub = FakeSarvam(), Collector()
    client = e2e_app(fake, pub)
    client.__enter__()
    try:
        from tests.test_vobiz_websocket import start_msg, url

        ws_cm = client.websocket_connect(url())
        ws = ws_cm.__enter__()
        ws.send_json(start_msg())
        session = wait_until(lambda: client.app.state.call_manager.current_stream(CALL))
        wait_until(lambda: session.consumer.state == RUNNING)
        ws.send_json(media_msg(b"\x00\x00" * 320))
        wait_until(lambda: session.consumer.frames_processed == 1)
        ws_cm.__exit__(None, None, None)
    finally:
        client.__exit__(None, None, None)
    assert fake.sockets[0].closed and session.consumer.state == STOPPED
