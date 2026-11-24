"""M5 test-first contract: track-aware transcript events and the M4.4 pipeline (ADR-002/003/005/006).

Scope: transcript pipeline behaviour only. No audio routing, no Sarvam sessions, no Vobiz, no handoff.

Approved contract exercised here
--------------------------------
* `TranscriptEvent.stream_id` stays the PHYSICAL Vobiz stream id. It is never replaced by a composite.
* `TranscriptEvent.track` is a new, optional, additive field (default None). Neutral labels only:
  `track_inbound`, `track_outbound`. Track identity is never speaker identity; `speaker` keeps its
  default and meaning.
* Sequence checking is keyed internally by (physical stream id, track). Tracks on one stream are
  independent; `track=None` keeps today's behaviour. The composite key is never published.
* A track ending (a `session.end` for one track) does not close the call-level session; only call
  closure does (M4.4 lifecycle unchanged).

EXPECTED TEST-FIRST STATE: until the approved `track` field and the composite sequence key exist,
tests that build events with `track=...` fail with `TypeError: ... unexpected keyword argument
'track'`, and tests that rely on per-track sequences fail on duplicate counts. That is the missing
contract showing, not a test defect. Nothing here is skipped, xfailed or weakened.

These tests import only `app.publishers.*` and helpers from the existing M4.4 test module. They do
not import `app.main`, so no application settings or `.env` handling is involved.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging

import pytest

from app.publishers.pipeline import CallClosedError, PublisherSubscriber, TranscriptSubscriber
from app.publishers.transcripts import (
    SESSION_END, TRANSCRIPT_FINAL, TRANSCRIPT_PARTIAL, LoggingTranscriptPublisher, TranscriptEvent,
)
from tests.test_transcript_pipeline import Rec, ev, final, leftover_tasks, pipe, settle

TRACK_IN = "track_inbound"
TRACK_OUT = "track_outbound"
PHYS = "phys-stream-1"  # a physical Vobiz stream id (opaque to the pipeline)
SECRET_TEXT = "tracked secret sentence 98765"

# TranscriptEvent fields as of M4.4, in declaration order. `track` must be appended after them so
# positional construction keeps working.
M44_FIELDS = [
    "call_id", "stream_id", "sequence", "type", "received_at", "source", "speaker", "is_final", "text",
    "language", "language_confidence", "utterance_idx", "start_s", "end_s", "stt_request_id", "error_code",
    "error_is_fatal",
]
M44_LOG_FIELDS = {
    "call_id", "stream_id", "sequence", "event_type", "is_final", "utterance_idx", "language", "text_length",
    "stt_request_id", "error_code",
}


def tev(track: str | None, seq: int, *, stream: str = PHYS, type_: str = TRANSCRIPT_PARTIAL, **kw) -> TranscriptEvent:
    """A transcript event for call 'c1' on physical stream `stream` and logical `track`."""
    return ev(call="c1", seq=seq, type_=type_, stream=stream, track=track, **kw)


def seqs_of(rec: Rec, track: str | None) -> list[int]:
    return [e.sequence for e in rec.events if e.track == track]


# =============================================================================================
# Group A: additive track metadata
# =============================================================================================


async def test_ttrk_01_physical_stream_id_is_unchanged_by_track_metadata() -> None:
    event = tev(TRACK_IN, 1)
    assert event.stream_id == PHYS and TRACK_IN not in event.stream_id  # no namespaced/composite stream id
    assert event.log_fields()["stream_id"] == PHYS

    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    await p.publish(event)
    await p.publish(tev(TRACK_OUT, 1))
    await settle(lambda: len(r.events) == 2)
    assert [e.stream_id for e in r.events] == [PHYS, PHYS]  # what subscribers see is the physical id
    await p.close_call("c1")


async def test_ttrk_02_track_identity_is_a_separate_optional_additive_field() -> None:
    a, b = tev(TRACK_IN, 1), tev(TRACK_OUT, 1)
    assert a.stream_id == b.stream_id == PHYS
    assert (a.track, b.track) == (TRACK_IN, TRACK_OUT)

    fields = {f.name: f for f in dataclasses.fields(TranscriptEvent)}
    assert "track" in fields and fields["track"].default is None  # optional, additive
    names = [f.name for f in dataclasses.fields(TranscriptEvent)]
    assert names[: len(M44_FIELDS)] == M44_FIELDS  # existing fields keep their order; track comes after


async def test_ttrk_03_legacy_event_without_track_stays_valid_and_speaker_semantics_are_unchanged() -> None:
    # the construction pattern used by the Sarvam sink today (keywords only, no track)
    legacy = TranscriptEvent(call_id="c1", stream_id=PHYS, sequence=1, type=TRANSCRIPT_FINAL, received_at="t")
    assert legacy.speaker == "customer" and legacy.stream_id == PHYS and legacy.source == "sarvam"
    assert M44_LOG_FIELDS <= set(legacy.log_fields())  # existing log/serialization fields remain

    tracked = tev(TRACK_OUT, 1, text=SECRET_TEXT)
    assert tracked.speaker == "customer"  # a track label never becomes the speaker
    assert tracked.speaker not in (TRACK_IN, TRACK_OUT)
    explicit = tev(TRACK_IN, 2, speaker="someone-else")
    assert explicit.speaker == "someone-else" and explicit.track == TRACK_IN  # track never overwrites speaker
    assert SECRET_TEXT not in str(tracked.log_fields()) and M44_LOG_FIELDS <= set(tracked.log_fields())

    assert legacy.track is None  # legacy events carry no track


# =============================================================================================
# Group B: track-aware sequence handling
# =============================================================================================


async def test_ttrk_04_same_sequence_on_different_tracks_is_accepted_independently() -> None:
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    await p.publish(tev(TRACK_IN, 1, text="in"))
    await p.publish(tev(TRACK_OUT, 1, text="out"))  # same physical stream, same sequence, other track
    await settle(lambda: len(r.events) == 2)

    m = p.get_session("c1").metrics()
    assert m["duplicates"] == 0 and m["sequence_anomalies"] == 0 and m["events_accepted"] == 2
    assert {e.track for e in r.events} == {TRACK_IN, TRACK_OUT}
    assert all(e.stream_id == PHYS for e in r.events)  # the composite key is never published
    await p.close_call("c1")


async def test_ttrk_05_same_track_duplicate_is_dropped_under_the_existing_policy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    await p.publish(tev(TRACK_IN, 1))
    await p.publish(tev(TRACK_OUT, 1))
    await p.publish(tev(TRACK_IN, 1))  # retransmission on the SAME physical stream and track
    await p.publish(tev(TRACK_IN, 2))  # sentinel: FIFO delivery means the duplicate would have arrived first
    await settle(lambda: seqs_of(r, TRACK_IN) == [1, 2])

    m = p.get_session("c1").metrics()
    assert m["duplicates"] == 1 and m["events_accepted"] == 3
    assert seqs_of(r, TRACK_IN) == [1, 2] and seqs_of(r, TRACK_OUT) == [1]  # not delivered twice
    assert any(rec.message == "transcript_duplicate_sequence" for rec in caplog.records)
    await p.close_call("c1")


async def test_ttrk_06_tracks_keep_independent_sequence_ordering_state() -> None:
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    for seq in (1, 2, 3):  # interleaved arrival, overlapping sequence values
        await p.publish(tev(TRACK_IN, seq))
        await p.publish(tev(TRACK_OUT, seq))
    await p.publish(tev(TRACK_IN, 5))   # inbound: a gap
    await p.publish(tev(TRACK_IN, 4))   # inbound: out of order
    await p.publish(tev(TRACK_OUT, 4))  # outbound: perfectly in order, unaffected by inbound's anomalies
    await settle(lambda: len(r.events) == 9)  # 3 + 3 interleaved, then in:5, in:4, out:4

    m = p.get_session("c1").metrics()
    assert (m["duplicates"], m["sequence_gaps"], m["out_of_order"]) == (0, 1, 1)
    assert seqs_of(r, TRACK_IN) == [1, 2, 3, 5, 4]  # delivered as received, never reordered
    assert seqs_of(r, TRACK_OUT) == [1, 2, 3, 4]
    await p.close_call("c1")


async def test_ttrk_07_events_without_a_track_keep_the_m44_sequence_behaviour() -> None:
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    legacy = [ev(call="c1", seq=s, stream=PHYS) for s in (0, 1, 1, 3, 2)]  # no track: today's pattern
    for event in legacy:
        await p.publish(event)
    await settle(lambda: len(r.events) == 4)

    m = p.get_session("c1").metrics()
    assert m["duplicates"] == 1 and m["sequence_gaps"] == 1 and m["out_of_order"] == 1
    assert r.seqs() == [0, 1, 3, 2]  # duplicate dropped, the rest delivered in received order
    assert all(e.stream_id == PHYS for e in r.events)
    assert all(e.track is None for e in r.events)
    await p.close_call("c1")


# =============================================================================================
# Group C: pipeline and lifecycle compatibility
# =============================================================================================


async def test_ttrk_08_existing_subscriber_kinds_receive_track_aware_events() -> None:
    p = pipe()
    rec, via_callable, via_publisher = Rec(), [], []

    class Pub:  # an existing TranscriptPublisher, adapted by the existing PublisherSubscriber
        async def publish(self, event: TranscriptEvent) -> None:
            via_publisher.append(event)

    assert isinstance(rec, TranscriptSubscriber)  # subscriber contract is unchanged
    p.add_global_subscriber(rec, "rec")
    p.add_global_subscriber(lambda e: via_callable.append(e), "callable")
    p.add_global_subscriber(PublisherSubscriber(Pub()), "publisher")
    p.add_global_subscriber(PublisherSubscriber(LoggingTranscriptPublisher()), "logging")

    sent = [tev(TRACK_IN, 1, text="a"), tev(TRACK_OUT, 1, text="b"), final(seq=2, stream=PHYS, track=TRACK_IN)]
    for event in sent:
        await p.publish(event)
    await settle(lambda: len(rec.events) == 3 and len(via_callable) == 3 and len(via_publisher) == 3)

    assert rec.events == sent and via_callable == sent and via_publisher == sent  # same events, same order
    assert [e.track for e in rec.events] == [TRACK_IN, TRACK_OUT, TRACK_IN]
    await p.close_call("c1")


async def test_ttrk_09_one_track_ending_does_not_close_the_call_session() -> None:
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    await p.publish(tev(TRACK_IN, 1))
    await p.publish(tev(TRACK_IN, 2, type_=TRANSCRIPT_FINAL))
    await p.publish(tev(TRACK_IN, 3, type_=SESSION_END))  # the inbound track's STT session ends
    await p.publish(tev(TRACK_OUT, 1))                    # the other track keeps producing
    await p.publish(tev(TRACK_OUT, 2, type_=TRANSCRIPT_FINAL))
    await settle(lambda: len(r.events) == 5)

    session = p.get_session("c1")
    assert session is not None and session.state == "open"
    assert r.closed == [] and p.calls_closed == 0 and not session.closing_or_closed
    assert seqs_of(r, TRACK_OUT) == [1, 2]  # still accepted and delivered after the other track ended
    await p.close_call("c1")


async def test_ttrk_10_explicit_call_closure_remains_authoritative() -> None:
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    await p.publish(tev(TRACK_IN, 1))
    await p.publish(tev(TRACK_OUT, 1, type_=TRANSCRIPT_FINAL))  # neither track has ended
    report = await p.close_call("c1", "hangup")

    assert p.get_session("c1") is None and p.calls_closed == 1
    assert len(r.closed) == 1 and r.closed[0].reason == "hangup" and r.closed[0].finals_accepted == 1
    assert seqs_of(r, TRACK_IN) == [1] and seqs_of(r, TRACK_OUT) == [1]  # drained before the marker
    assert report.undelivered_events == 0 and not report.drain_timed_out
    await asyncio.sleep(0.02)
    assert leftover_tasks() == []


async def test_ttrk_11_late_track_events_cannot_reopen_a_closed_call() -> None:
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    await p.publish(tev(TRACK_IN, 1))
    await p.close_call("c1", "hangup")

    await p.publish(tev(TRACK_IN, 2))                            # late partial, first track
    await p.publish(tev(TRACK_OUT, 1, type_=TRANSCRIPT_FINAL))   # late final, other track

    assert p.get_session("c1") is None and p.open_call("c1") is None and p.calls_opened == 1
    assert p.rejected_after_close == 2
    assert seqs_of(r, TRACK_IN) == [1] and seqs_of(r, TRACK_OUT) == []
    with pytest.raises(CallClosedError):
        p.subscribe("c1", Rec(), "late")


async def test_ttrk_12_failing_subscriber_does_not_stop_others_receiving_track_events(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    p, ok = pipe(), Rec()

    class Bad:
        calls = 0

        async def on_event(self, event: TranscriptEvent) -> None:
            Bad.calls += 1
            raise RuntimeError(SECRET_TEXT)

    p.add_global_subscriber(Bad(), "bad")
    p.add_global_subscriber(ok, "ok")
    sent = [tev(TRACK_IN, 1, text=SECRET_TEXT), tev(TRACK_OUT, 1, text=SECRET_TEXT),
            tev(TRACK_IN, 2, text=SECRET_TEXT), tev(TRACK_OUT, 2, text=SECRET_TEXT)]
    for event in sent:
        await p.publish(event)
    await settle(lambda: len(ok.events) == 4 and Bad.calls == 4)

    subs = {s["name"]: s for s in p.get_session("c1").metrics()["subscribers"]}
    assert subs["bad"]["failures"] == 4 and subs["ok"]["failures"] == 0
    assert seqs_of(ok, TRACK_IN) == [1, 2] and seqs_of(ok, TRACK_OUT) == [1, 2]
    assert p.get_session("c1").state == "open"  # a failing subscriber never closes the shared session
    assert SECRET_TEXT not in caplog.text  # neither exception text nor transcript text is logged
    await p.close_call("c1")


# =============================================================================================
# TTRK-13: legacy and tracked events share a physical stream but not a sequence namespace
# =============================================================================================


async def test_ttrk_13_legacy_and_tracked_namespaces_on_one_physical_stream_are_independent() -> None:
    stream = "stream-A"
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    for track in (None, TRACK_IN, TRACK_OUT):  # same physical stream, same sequence, three namespaces
        await p.publish(tev(track, 1, stream=stream))
    await settle(lambda: len(r.events) == 3)

    m = p.get_session("c1").metrics()
    assert m["events_accepted"] == 3 and m["duplicates"] == 0 and m["sequence_anomalies"] == 0
    assert m["streams"] == 1 and m["sequence_namespaces"] == 3  # one physical stream, three identities
    assert [e.stream_id for e in r.events] == [stream] * 3  # the published id is never composite
    assert [e.track for e in r.events] == [None, TRACK_IN, TRACK_OUT]

    # a repeat is a duplicate only inside its own namespace, one namespace at a time
    for expected, track in enumerate((None, TRACK_IN, TRACK_OUT), start=1):
        await p.publish(tev(track, 1, stream=stream))
        assert p.get_session("c1").metrics()["duplicates"] == expected
    for track in (None, TRACK_IN, TRACK_OUT):  # sentinels: FIFO delivery proves the repeats were not delivered
        await p.publish(tev(track, 2, stream=stream))
    await settle(lambda: len(r.events) == 6)

    m = p.get_session("c1").metrics()
    assert m["events_accepted"] == 6 and m["duplicates"] == 3 and m["sequence_anomalies"] == 3
    assert [(e.track, e.sequence) for e in r.events] == [
        (None, 1), (TRACK_IN, 1), (TRACK_OUT, 1), (None, 2), (TRACK_IN, 2), (TRACK_OUT, 2),
    ]
    assert m["streams"] == 1 and m["sequence_namespaces"] == 3
    await p.close_call("c1")
