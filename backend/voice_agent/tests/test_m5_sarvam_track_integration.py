"""M5 dual-track Sarvam integration: one independent (MOCKED) Sarvam session per recognized track.

`FakeSarvam` is the scripted fake from the M4.3 tests; no real Sarvam connection exists here. These
tests prove our wiring (isolation, labels, ids, sequences, finalization), not Sarvam or Vobiz behaviour.
Audio is synthetic: each track's frames carry a distinct byte signature so leakage would be visible.
"""

from __future__ import annotations

import copy
import json
import logging
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.publishers.transcripts import TranscriptEvent
from app.services.audio.audio_buffer import AudioFrame
from app.services.voice.audio_consumer import FAILED, RUNNING
from app.services.voice.sarvam_stt_sink import SarvamSttSinkFactory
from app.services.voice.track_router import TRACK_INBOUND, TRACK_OUTBOUND, TrackRoutingSinkFactory
from tests.test_m5_track_router_lifecycle import HANGUP, monitor_frame, open_monitor
from tests.test_sarvam_realtime_stt import invalid_status
from tests.test_transcript_pipeline import Rec
from tests.test_transcript_pipeline import pipe as make_pipeline
from tests.test_transcript_pipeline import settle as pipeline_settle
from tests.test_vobiz_sarvam_stt import (
    KEY, SPOKEN, Collector, FakeSarvam, factory_for, make_session, settle, start, stt_settings,
)
from tests.test_vobiz_websocket import CALL, wait_until

SIG_IN, SIG_OUT = 0x11, 0x22  # first byte of every sample pair: identifies which track a chunk came from
PHYS = "phys-1"


def frame(session, provider_track: str, n: int, sig: int) -> AudioFrame:
    return AudioFrame(data=bytes([sig, 1]) * 320, format=session.audio_format, track=provider_track,
                      timestamp_ms=n * 20, chunk=n, sequence=n, stream_id=session.stream_id,
                      received_at=time.monotonic())


def feed(session, provider_track: str, sig: int, *, count: int = 5, first: int = 0) -> None:
    for n in range(first, first + count):  # five 640-byte frames = one 3,200-byte Sarvam chunk
        session.queue.put(frame(session, provider_track, n, sig))
        session.stats.frames_received += 1


class Stack:
    """A monitored physical stream whose consumer runs a router with real per-track Sarvam sinks (fake socket)."""

    def __init__(self, fake: FakeSarvam, *, finalize_s: float = 0.5, extra_subscribers: tuple = ()) -> None:
        self.fake = fake
        self.pipeline = make_pipeline()
        self.rec = Rec()
        for i, sub in enumerate(extra_subscribers):
            self.pipeline.add_global_subscriber(sub, f"extra{i}")
        self.pipeline.add_global_subscriber(self.rec, "rec")
        self.sarvam = SarvamSttSinkFactory(
            stt_settings(vobiz_sarvam_stt_finalize_timeout_s=finalize_s), self.pipeline,
            client_factory=fake.client_factory,
        )
        self.routing = TrackRoutingSinkFactory(
            self.sarvam.create_for_track, queue_frames=100, drain_timeout_s=0.2,
            child_close_timeout_s=self.sarvam.close_timeout_s,
        )
        self.session = make_session("call-1", PHYS)
        self.session.kind = "monitor"

    async def open(self):
        self.consumer = self.session.start_consumer(self.routing, drain_timeout_s=0.2)
        await settle(lambda: self.consumer.state == RUNNING)
        self.router = self.consumer._sink
        return self

    def track_of_socket(self) -> dict[str, object]:
        """Map track label -> its fake Sarvam socket, using the audio signature each socket received."""
        out = {}
        for ws in self.fake.sockets:
            if ws.audio:
                out[TRACK_INBOUND if ws.audio[0][0] == SIG_IN else TRACK_OUTBOUND] = ws
        return out


async def both_tracks_flowing(stack: Stack) -> None:
    feed(stack.session, "inbound", SIG_IN)
    feed(stack.session, "outbound", SIG_OUT)
    await settle(lambda: len(stack.fake.sockets) == 2 and all(ws.audio for ws in stack.fake.sockets))
    await settle(lambda: {e.track for e in stack.rec.events if e.is_partial} == {TRACK_INBOUND, TRACK_OUTBOUND})


# =============================================================================================
# independent sessions, no shared audio
# =============================================================================================


async def test_s01_two_independent_sarvam_sessions_are_created() -> None:
    stack = await Stack(FakeSarvam()).open()
    await both_tracks_flowing(stack)
    assert stack.fake.connects == 2 and len(stack.fake.sockets) == 2
    assert stack.fake.sockets[0] is not stack.fake.sockets[1]
    await stack.session.close("socket_closed")


async def test_s02_each_session_receives_only_its_own_tracks_audio() -> None:
    stack = await Stack(FakeSarvam()).open()
    await both_tracks_flowing(stack)
    mapping = stack.track_of_socket()
    assert set(mapping) == {TRACK_INBOUND, TRACK_OUTBOUND}
    for track, sig in ((TRACK_INBOUND, SIG_IN), (TRACK_OUTBOUND, SIG_OUT)):
        chunks = mapping[track].audio
        assert chunks and all(set(chunk[0::2]) == {sig} for chunk in chunks)  # no byte from the other track
        assert [len(c) for c in chunks] == [3200]
    await stack.session.close("socket_closed")


# =============================================================================================
# identity: track, physical stream id, sequences, speaker
# =============================================================================================


async def test_s03_s04_s05_events_carry_the_right_track_physical_stream_id_and_independent_sequences() -> None:
    stack = await Stack(FakeSarvam()).open()
    await both_tracks_flowing(stack)
    await stack.session.close("socket_closed")

    mapping = stack.track_of_socket()
    request_track = {mapping[t].request_id: t for t in mapping}  # Sarvam request id -> track, via audio signature
    events: list[TranscriptEvent] = stack.rec.events
    assert events and all(e.stream_id == PHYS for e in events)  # always the physical Vobiz stream id
    assert {e.track for e in events} == {TRACK_INBOUND, TRACK_OUTBOUND}
    checked = 0
    for e in events:
        if e.stt_request_id is not None:
            assert e.track == request_track[e.stt_request_id]  # the label matches the socket that produced it
            checked += 1
    assert checked > 0
    for track in (TRACK_INBOUND, TRACK_OUTBOUND):
        seqs = [e.sequence for e in events if e.track == track]
        assert seqs == list(range(1, len(seqs) + 1)) and len(seqs) >= 2  # each track counts from 1 on its own
    m = stack.pipeline.get_session("call-1").metrics() if stack.pipeline.get_session("call-1") else None
    # (the session is still open: only the physical stream ended, not the call)
    assert m is not None and m["duplicates"] == 0 and m["sequence_anomalies"] == 0
    assert m["streams"] == 1 and m["sequence_namespaces"] == 2
    await stack.pipeline.close_call("call-1")


async def test_s06_speaker_semantics_are_unchanged_for_tracked_events() -> None:
    stack = await Stack(FakeSarvam()).open()
    await both_tracks_flowing(stack)
    await stack.session.close("socket_closed")
    assert {e.speaker for e in stack.rec.events} == {"customer"}  # the existing default; track is not speaker
    await stack.pipeline.close_call("call-1")


async def test_s07_legacy_factory_call_is_unchanged_and_emits_untracked_events() -> None:
    fake, pub = FakeSarvam(), Collector()
    session = make_session("call-L", "stream-L")
    await start(session, factory_for(fake, pub))  # SarvamSttSinkFactory.__call__: the M4.3 path
    for n in range(5):
        session.queue.put(frame(session, "inbound", n, SIG_IN))
        session.stats.frames_received += 1
    await settle(lambda: any(e.is_partial for e in pub.events))
    await session.close("socket_closed")
    assert pub.events and {e.track for e in pub.events} == {None}
    assert {e.stream_id for e in pub.events} == {"stream-L"} and {e.speaker for e in pub.events} == {"customer"}
    assert [e.sequence for e in pub.events] == list(range(1, len(pub.events) + 1))


# =============================================================================================
# failure isolation and finalization
# =============================================================================================


async def test_s08_one_sarvam_connection_failure_does_not_stop_the_other_track() -> None:
    fake = FakeSarvam(outcomes=[invalid_status(403)])  # the FIRST connect (inbound) is refused
    stack = await Stack(fake).open()
    feed(stack.session, "inbound", SIG_IN)
    await settle(lambda: stack.router.worker(TRACK_INBOUND) is not None and fake.connects >= 1)
    feed(stack.session, "outbound", SIG_OUT)
    await settle(lambda: stack.router.worker(TRACK_INBOUND).consumer.state == FAILED and len(fake.sockets) == 1)
    await settle(lambda: any(e.is_partial and e.track == TRACK_OUTBOUND for e in stack.rec.events))
    assert not any(e.track == TRACK_INBOUND for e in stack.rec.events)
    await stack.session.close("socket_closed")
    assert any(e.is_final and e.track == TRACK_OUTBOUND for e in stack.rec.events)  # the healthy track finalized
    await stack.pipeline.close_call("call-1")


async def test_s09_each_session_finalizes_exactly_once() -> None:
    stack = await Stack(FakeSarvam()).open()
    await both_tracks_flowing(stack)
    await stack.session.close("socket_closed")
    await stack.session.close("socket_closed")  # duplicate close
    for ws in stack.fake.sockets:
        events = [json.loads(t)["event"] for t in ws.sent]
        assert events.count("end") == 1 and events[-1] == "end" and ws.closed
    finals = [e for e in stack.rec.events if e.is_final]
    assert sorted(e.track for e in finals) == [TRACK_INBOUND, TRACK_OUTBOUND]
    await stack.pipeline.close_call("call-1")


async def test_s10_finalization_timeout_is_bounded_and_recorded_per_track() -> None:
    fake = FakeSarvam(final_on_end=False, session_end=False)  # Sarvam never answers `end`
    stack = await Stack(fake, finalize_s=0.3).open()
    await both_tracks_flowing(stack)
    loop = __import__("asyncio").get_running_loop()
    started = loop.time()
    await stack.session.close("socket_closed")
    assert loop.time() - started < 3.0  # drain + finalize budget, concurrent across tracks, never open-ended
    for track in (TRACK_INBOUND, TRACK_OUTBOUND):
        sink = stack.router.metrics()["tracks"][track]["sink"]
        assert sink["finalize_timed_out"] is True and sink["track"] == track
    assert all(ws.closed for ws in fake.sockets)
    await stack.pipeline.close_call("call-1")


async def test_s11_error_events_carry_their_track() -> None:
    stack = await Stack(FakeSarvam()).open()
    await both_tracks_flowing(stack)
    ws_out = stack.track_of_socket()[TRACK_OUTBOUND]
    ws_out.push({"event": "error", "code": "chunk_too_large", "is_fatal": False, "message": "x"})
    await pipeline_settle(lambda: any(e.type == "error" for e in stack.rec.events))
    errors = [e for e in stack.rec.events if e.type == "error"]
    assert [(e.track, e.error_code) for e in errors] == [(TRACK_OUTBOUND, "chunk_too_large")]
    await stack.session.close("socket_closed")
    await stack.pipeline.close_call("call-1")


async def test_s12_ending_one_track_does_not_close_the_call_transcript_session_or_the_other_track() -> None:
    stack = await Stack(FakeSarvam()).open()
    await both_tracks_flowing(stack)
    assert await stack.router.end_track(TRACK_INBOUND, "track_done") is True
    await pipeline_settle(lambda: any(e.type == "session.end" and e.track == TRACK_INBOUND for e in stack.rec.events))
    session = stack.pipeline.get_session("call-1")
    assert session is not None and session.state == "open" and stack.rec.closed == []

    before = len(stack.track_of_socket()[TRACK_OUTBOUND].audio)
    feed(stack.session, "outbound", SIG_OUT, first=10)  # the other track keeps streaming to its own Sarvam session
    await settle(lambda: len(stack.track_of_socket()[TRACK_OUTBOUND].audio) == before + 1)
    assert not any(e.type == "session.end" and e.track == TRACK_OUTBOUND for e in stack.rec.events)
    await stack.session.close("socket_closed")
    await stack.pipeline.close_call("call-1")


async def test_s13_a_failing_subscriber_does_not_affect_either_tracks_delivery() -> None:
    class Bad:
        async def on_event(self, event) -> None:
            raise RuntimeError("subscriber failed")

    stack = await Stack(FakeSarvam(), extra_subscribers=(Bad(),)).open()
    await both_tracks_flowing(stack)
    await stack.session.close("socket_closed")
    assert {e.track for e in stack.rec.events if e.is_final} == {TRACK_INBOUND, TRACK_OUTBOUND}
    await stack.pipeline.close_call("call-1")


async def test_s14_logs_contain_no_audio_transcript_text_or_keys(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    stack = await Stack(FakeSarvam()).open()
    await both_tracks_flowing(stack)
    await stack.session.close("socket_closed")
    await stack.pipeline.close_call("call-1")
    text = caplog.text
    assert SPOKEN not in text and KEY not in text
    assert bytes([SIG_IN, 1] * 320).hex() not in text and bytes([SIG_OUT, 1] * 320).hex() not in text
    assert "track" in text  # structured fields identify the track


# =============================================================================================
# end to end through the monitor WebSocket (synthetic Vobiz client, mocked Sarvam)
# =============================================================================================


def test_s15_monitor_websocket_routes_both_tracks_to_independent_sarvam_sessions() -> None:
    fake, pub = FakeSarvam(), Collector()
    s = stt_settings(vobiz_sarvam_stt_enabled=True)
    sarvam = SarvamSttSinkFactory(s, pub, client_factory=fake.client_factory)
    routing = TrackRoutingSinkFactory(sarvam.create_for_track, queue_frames=100, drain_timeout_s=0.2,
                                      child_close_timeout_s=sarvam.close_timeout_s)
    with TestClient(create_app(s, monitor_sink_factory=routing)) as client:
        with open_monitor(client, stream="mon-9") as mon:
            wait_until(lambda: mon.session.consumer is not None and mon.session.consumer.state == RUNNING)
            for n in range(1, 11):
                msg_in = copy.deepcopy(monitor_frame("inbound", n, "mon-9"))
                msg_out = copy.deepcopy(monitor_frame("outbound", n, "mon-9"))
                mon.ws.send_json(msg_in)
                mon.ws.send_json(msg_out)
            wait_until(lambda: len(fake.sockets) == 2 and all(ws.audio for ws in fake.sockets))
            snap = client.get("/api/v1/vobiz/streams", headers={"X-Internal-Token": "internal-token"}).json()
            call = snap["calls"][0]
            assert call["active_streams"] == 1 and call["monitor_streams"][0]["kind"] == "monitor"
            assert client.post(HANGUP, data={"CallUUID": CALL, "Direction": "outbound", "Event": "Hangup"}).status_code == 200
            assert mon.session.closed
    finals = [e for e in pub.events if e.is_final]
    assert sorted(e.track for e in finals) == [TRACK_INBOUND, TRACK_OUTBOUND]
    assert {e.stream_id for e in pub.events} == {"mon-9"} and {e.call_id for e in pub.events} == {CALL}
    assert all(ws.closed for ws in fake.sockets)
