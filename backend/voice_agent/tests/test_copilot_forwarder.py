"""Voice agent -> Copilot forwarding (M3). A mocked HTTP transport stands in for the Copilot: no network."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.publishers.copilot_forwarder import (
    CopilotCallListener,
    CopilotEventForwarder,
    attribute_speaker,
    safe_call_id,
    session_id_for,
)
from app.publishers.pipeline import TranscriptEventPipeline
from app.services.voice.track_router import TRACK_INBOUND, TRACK_OUTBOUND
from tests.ai_fakes import final, partial, settle

TOKEN = "super-secret-internal-token"
T_URL = "http://copilot.test/api/v1/internal/transcripts/events"
CALL = "call-uuid-1"


def make_settings(**over) -> Settings:
    values = dict(
        copilot_transcript_webhook_url=T_URL, internal_api_token=SecretStr(TOKEN), copilot_forward_backoff_s=0.01,
        copilot_forward_max_backoff_s=0.04, copilot_forward_max_attempts=4, copilot_forward_buffer_size=16,
        copilot_forward_drain_timeout_s=2.0,
    )
    values.update(over)
    return Settings(**values)


class Copilot:
    """A fake Copilot: records every request and answers with whatever `script` says."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.script: list[int] = []  # status codes to answer first, then 201
        self.gate: asyncio.Event | None = None
        self.seen_ids: set[str] = set()

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.gate is not None:
            await self.gate.wait()
        if self.script:
            return httpx.Response(self.script.pop(0))
        return httpx.Response(201, json={"status": "stored"})

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]

    def transcripts(self) -> list[dict]:
        return [b for r, b in zip(self.requests, self.bodies) if r.url.path.endswith("/transcripts/events")]

    def calls(self) -> list[dict]:
        return [b for r, b in zip(self.requests, self.bodies) if r.url.path.endswith("/calls/events")]


@pytest.fixture
def copilot() -> Copilot:
    return Copilot()


def make_forwarder(copilot: Copilot, sleeps: list | None = None, **over) -> CopilotEventForwarder:
    async def no_sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)
        await asyncio.sleep(0)

    client = httpx.AsyncClient(transport=httpx.MockTransport(copilot.handler))
    return CopilotEventForwarder(make_settings(**over), client=client, sleep=no_sleep)


async def drained(fwd: CopilotEventForwarder, call_id: str = CALL) -> None:
    await fwd.finish_call(call_id)


# ---- what is sent ------------------------------------------------------------------------------


async def test_partial_and_final_events_are_forwarded_with_the_required_metadata(copilot) -> None:
    fwd = make_forwarder(copilot)
    await fwd.on_event(partial(CALL, "I need", 1))
    await fwd.on_event(final(CALL, "I need help with my mobile recharge.", 2))
    await drained(fwd)
    first, second = copilot.transcripts()
    assert first["transcript_type"] == "partial" and second["transcript_type"] == "final"
    assert second["transcript_text"] == "I need help with my mobile recharge."
    for body in (first, second):
        assert body["call_id"] == CALL and body["session_id"] == f"sess-{CALL}"
        assert body["speaker"] == "customer" and body["speaker_basis"] == "primary_stream_inbound_track"
        assert body["call_status"] == "in_progress" and body["agent_mode"] == "ai_agent"
        assert body["timestamp"].endswith("Z") and body["event_id"].startswith(f"{CALL}-t")
    assert [first["sequence_number"], second["sequence_number"]] == [1, 2]


async def test_requests_carry_the_internal_token_and_go_to_the_configured_route(copilot) -> None:
    fwd = make_forwarder(copilot)
    await fwd.on_event(final(CALL, "hello", 1))
    fwd.emit_call_event(CALL, "call_started", {"from_number": "+8801700000000"})
    await drained(fwd)
    assert {r.headers["x-internal-token"] for r in copilot.requests} == {TOKEN}
    assert [r.url.path for r in copilot.requests] == [
        "/api/v1/internal/transcripts/events", "/api/v1/internal/calls/events"]


async def test_sequence_numbers_are_per_call_and_cover_every_speaker_in_arrival_order(copilot) -> None:
    fwd = make_forwarder(copilot)
    await fwd.on_event(final(CALL, "customer one", 1))
    fwd.emit_ai_message(CALL, "ai one")
    await fwd.on_event(final(CALL, "customer two", 2))
    await fwd.on_event(final("other-call", "x", 1))
    await fwd.finish_call(CALL)
    await fwd.finish_call("other-call")
    mine = [b for b in copilot.transcripts() if b["call_id"] == CALL]
    assert [(b["sequence_number"], b["speaker"]) for b in mine] == [(1, "customer"), (2, "ai_agent"), (3, "customer")]
    other = [b for b in copilot.transcripts() if b["call_id"] == "other-call"]
    assert other[0]["sequence_number"] == 1 and other[0]["session_id"] != mine[0]["session_id"]


async def test_ai_replies_are_sent_as_ai_agent_final_lines(copilot) -> None:
    fwd = make_forwarder(copilot)
    fwd.emit_ai_message(CALL, "Assalamu alaikum, how can I help?")
    await drained(fwd)
    body = copilot.transcripts()[0]
    assert (body["speaker"], body["transcript_type"], body["speaker_basis"]) == ("ai_agent", "final", "ai_agent_reply")


async def test_vad_and_session_events_are_not_forwarded(copilot) -> None:
    from tests.ai_fakes import speech_start

    fwd = make_forwarder(copilot)
    await fwd.on_event(speech_start(CALL, 1))
    await fwd.on_event(partial(CALL, "   ", 2))  # blank text
    await drained(fwd)
    assert copilot.requests == []


# ---- speaker attribution -----------------------------------------------------------------------


@pytest.mark.parametrize("track,mode,outbound,expected", [
    (None, "ai_agent", "unknown", ("customer", "primary_stream_inbound_track")),
    (TRACK_INBOUND, "human_agent", "unknown", ("customer", "monitor_inbound_track")),
    (TRACK_OUTBOUND, "human_agent", "unknown", ("unknown", "outbound_track_unverified")),
    (TRACK_OUTBOUND, "human_agent", "human_agent", ("human_agent", "monitor_outbound_track_configured")),
    (TRACK_OUTBOUND, "ai_agent", "human_agent", ("unknown", "outbound_track_unverified")),  # never while the AI has the call
    ("track_weird", "human_agent", "human_agent", ("unknown", "unrecognized_track")),
])
def test_speaker_comes_from_the_media_track_and_is_never_guessed(track, mode, outbound, expected) -> None:
    event = final(CALL, "x", 1, track=track)
    assert attribute_speaker(event, mode, outbound) == expected


async def test_the_stt_default_speaker_label_is_not_trusted(copilot) -> None:
    """The STT sink leaves TranscriptEvent.speaker at "customer" for every track on purpose."""
    fwd = make_forwarder(copilot)
    await fwd.on_event(final(CALL, "voice on the outbound track", 1, track=TRACK_OUTBOUND, speaker="customer"))
    await drained(fwd)
    assert copilot.transcripts()[0]["speaker"] == "unknown"


async def test_after_the_transfer_outbound_speech_is_the_human_only_when_configured(copilot) -> None:
    fwd = make_forwarder(copilot, vobiz_outbound_track_speaker="human_agent")
    await fwd.on_event(final(CALL, "before transfer", 1, track=TRACK_OUTBOUND))  # AI mode: stays unknown
    fwd.emit_call_event(CALL, "human_agent_connected")
    await fwd.on_event(final(CALL, "hello, this is the agent", 2, track=TRACK_OUTBOUND))
    await fwd.on_event(final(CALL, "hello", 3, track=TRACK_INBOUND, stream_id="mon"))
    await drained(fwd)
    speakers = [(b["speaker"], b["agent_mode"]) for b in copilot.transcripts()]
    assert speakers == [("unknown", "ai_agent"), ("human_agent", "human_agent"), ("customer", "human_agent")]


# ---- duplicates and call state -------------------------------------------------------------------


async def test_a_final_delivered_twice_is_forwarded_once(copilot) -> None:
    fwd = make_forwarder(copilot)
    await fwd.on_event(final(CALL, "same", 5))
    await fwd.on_event(final(CALL, "same", 5))
    await drained(fwd)
    assert len(copilot.transcripts()) == 1
    assert fwd.snapshot(CALL) is None  # the state is released after finish_call


async def test_lifecycle_events_update_the_status_and_mode_later_transcripts_carry(copilot) -> None:
    fwd = make_forwarder(copilot)
    fwd.emit_call_event(CALL, "call_started", {"from_number": "+8801700000000"})
    await fwd.on_event(final(CALL, "one", 1))
    fwd.emit_call_event(CALL, "human_handoff_requested", {"reason": "refund"})
    await fwd.on_event(final(CALL, "two", 2, track=TRACK_INBOUND))
    fwd.emit_call_event(CALL, "human_agent_connected")
    await fwd.on_event(final(CALL, "three", 3, track=TRACK_INBOUND))
    fwd.emit_call_event(CALL, "customer_put_on_hold", {"source": "human_agent_dtmf"})
    await fwd.on_event(final(CALL, "four", 4, track=TRACK_INBOUND))
    await drained(fwd)
    got = [(b["transcript_text"], b["call_status"], b["agent_mode"]) for b in copilot.transcripts()]
    assert got == [
        ("one", "in_progress", "ai_agent"), ("two", "handoff_requested", "ai_agent"),
        ("three", "with_human", "human_agent"), ("four", "on_hold", "human_agent"),
    ]


async def test_once_only_lifecycle_events_are_sent_once(copilot) -> None:
    fwd = make_forwarder(copilot)
    for _ in range(3):
        fwd.emit_call_event(CALL, "call_started")
        fwd.emit_call_event(CALL, "call_ended")
    fwd.emit_call_event(CALL, "customer_put_on_hold")
    fwd.emit_call_event(CALL, "customer_put_on_hold")  # repeatable events get their own ids
    await drained(fwd)
    types = [b["type"] for b in copilot.calls()]
    assert types.count("call_started") == 1 and types.count("call_ended") == 1 and types.count("customer_put_on_hold") == 2
    assert len({b["event_id"] for b in copilot.calls()}) == len(types)


def test_ids_are_stable_and_safe() -> None:
    assert session_id_for("abc-123") == session_id_for("abc-123") == "sess-abc-123"
    assert safe_call_id("abc-123") == "abc-123"
    weird = safe_call_id("../../etc/passwd")
    assert weird.startswith("call-") and "/" not in weird and weird == safe_call_id("../../etc/passwd")
    assert len(session_id_for("x" * 300)) <= 128


# ---- reliability -------------------------------------------------------------------------------


async def test_a_temporary_copilot_failure_is_retried_with_the_same_event_id(copilot) -> None:
    copilot.script = [503, 502]
    sleeps: list[float] = []
    fwd = make_forwarder(copilot, sleeps)
    await fwd.on_event(final(CALL, "important", 1))
    await drained(fwd)
    assert len(copilot.requests) == 3
    assert len({b["event_id"] for b in copilot.bodies}) == 1  # idempotent: the Copilot can de-duplicate
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0] * 0.9  # back-off grows (with jitter)


async def test_network_errors_are_retried_too(copilot) -> None:
    calls = {"n": 0}

    async def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(201)

    async def no_sleep(_s: float) -> None:
        await asyncio.sleep(0)

    fwd = CopilotEventForwarder(make_settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(flaky)), sleep=no_sleep)
    await fwd.on_event(final(CALL, "x", 1))
    await drained(fwd)
    assert calls["n"] == 3


@pytest.mark.parametrize("status", [401, 403, 409, 422, 404])
async def test_permanent_rejections_are_not_retried_and_do_not_stop_later_events(copilot, status) -> None:
    copilot.script = [status]
    fwd = make_forwarder(copilot)
    await fwd.on_event(final(CALL, "rejected", 1))
    await fwd.on_event(final(CALL, "accepted", 2))
    await drained(fwd)
    assert [b["transcript_text"] for b in copilot.transcripts()] == ["rejected", "accepted"]


async def test_it_gives_up_after_the_attempt_limit_and_carries_on(copilot, caplog) -> None:
    copilot.script = [503] * 4  # all attempts for the first event
    fwd = make_forwarder(copilot)
    with caplog.at_level(logging.ERROR):
        await fwd.on_event(final(CALL, "lost", 1))
        await fwd.on_event(final(CALL, "kept", 2))
        await drained(fwd)
    assert [b["transcript_text"] for b in copilot.transcripts()] == ["lost"] * 4 + ["kept"]
    assert any(r.message == "copilot_forward_gave_up" for r in caplog.records)


async def test_partials_get_one_retry_so_they_never_hold_up_finals(copilot) -> None:
    copilot.script = [503] * 10
    fwd = make_forwarder(copilot)
    await fwd.on_event(partial(CALL, "p", 1))
    await drained(fwd)
    assert len(copilot.requests) == 2


async def test_a_slow_copilot_never_blocks_the_caller(copilot) -> None:
    copilot.gate = asyncio.Event()  # the Copilot hangs
    fwd = make_forwarder(copilot)
    await asyncio.wait_for(fwd.on_event(final(CALL, "x", 1)), 0.2)
    await asyncio.wait_for(fwd.on_event(partial(CALL, "y", 2)), 0.2)
    fwd.emit_ai_message(CALL, "z")
    fwd.emit_call_event(CALL, "call_started")
    copilot.gate.set()
    await drained(fwd)
    assert len(copilot.requests) == 4


async def test_the_pipeline_keeps_delivering_to_other_subscribers_while_the_copilot_hangs(copilot) -> None:
    copilot.gate = asyncio.Event()
    fwd = make_forwarder(copilot)
    pipeline = TranscriptEventPipeline(queue_size=64, final_reserve=8, subscriber_timeout_s=2.0, drain_timeout_s=1.0)
    other: list[str] = []
    pipeline.add_global_subscriber(fwd, "copilot")
    pipeline.add_global_subscriber(lambda e: other.append(e.text or ""), "other")
    for i in range(1, 21):
        await asyncio.wait_for(pipeline.publish(final(CALL, f"line {i}", i)), 0.2)  # publish never waits
    await settle(lambda: len(other) == 20)
    copilot.gate.set()
    await pipeline.close_call(CALL)
    await fwd.finish_call(CALL)
    assert len(copilot.transcripts()) == 20


async def test_a_full_buffer_drops_the_oldest_partial_first_and_keeps_finals(copilot) -> None:
    copilot.gate = asyncio.Event()
    fwd = make_forwarder(copilot, copilot_forward_buffer_size=8)
    for i in range(1, 9):
        await fwd.on_event(partial(CALL, f"partial {i}", i))
    for i in range(9, 17):
        await fwd.on_event(final(CALL, f"final {i}", i))
    snap = fwd.snapshot(CALL)
    assert snap["dropped_partials"] >= 8 and snap["dropped_other"] == 0
    copilot.gate.set()
    await drained(fwd)
    sent_finals = [b["transcript_text"] for b in copilot.transcripts() if b["transcript_type"] == "final"]
    assert sent_finals == [f"final {i}" for i in range(9, 17)]


async def test_finals_are_only_dropped_at_the_hard_cap_and_it_is_logged(copilot, caplog) -> None:
    copilot.gate = asyncio.Event()
    fwd = make_forwarder(copilot, copilot_forward_buffer_size=8)
    with caplog.at_level(logging.ERROR):
        for i in range(1, 40):
            await fwd.on_event(final(CALL, f"final {i}", i))
        snap = fwd.snapshot(CALL)
    assert snap["dropped_other"] > 0 and snap["buffered"] <= 16
    assert any(r.message == "copilot_forward_buffer_overflow" for r in caplog.records)
    copilot.gate.set()
    await drained(fwd)


async def test_a_call_that_ends_with_the_copilot_down_does_not_hang_shutdown(copilot) -> None:
    copilot.gate = asyncio.Event()  # never opens
    fwd = make_forwarder(copilot, copilot_forward_drain_timeout_s=0.1)
    await fwd.on_event(final(CALL, "x", 1))
    await asyncio.wait_for(fwd.finish_call(CALL), 1.0)
    await asyncio.wait_for(fwd.aclose(), 1.0)


async def test_emit_methods_never_raise_even_when_the_forwarder_is_broken(copilot, monkeypatch) -> None:
    fwd = make_forwarder(copilot)
    monkeypatch.setattr(fwd, "_push", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    fwd.emit_ai_message(CALL, "x")
    fwd.emit_call_event(CALL, "call_started")


# ---- configuration and privacy -----------------------------------------------------------------


async def test_forwarding_is_off_without_a_url_or_token(copilot) -> None:
    for over in ({"copilot_transcript_webhook_url": ""}, {"internal_api_token": SecretStr("")}):
        fwd = make_forwarder(copilot, **over)
        assert not fwd.enabled
        await fwd.on_event(final(CALL, "x", 1))
        fwd.emit_ai_message(CALL, "y")
        fwd.emit_call_event(CALL, "call_started")
        await fwd.aclose()
    assert copilot.requests == []


def test_the_calls_route_is_derived_from_the_transcript_route() -> None:
    assert make_settings().copilot_calls_webhook_url == "http://copilot.test/api/v1/internal/calls/events"


async def test_logs_never_contain_transcript_text_or_the_token(copilot, caplog) -> None:
    copilot.script = [503, 422]
    fwd = make_forwarder(copilot)
    with caplog.at_level(logging.DEBUG):
        await fwd.on_event(final(CALL, "my secret account number is 12345", 1))
        await fwd.on_event(final(CALL, "my secret account number is 12345", 1))
        await drained(fwd)
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "12345" not in logged and TOKEN not in logged


async def test_the_call_listener_reports_connect_and_end_and_releases_the_call(copilot) -> None:
    from types import SimpleNamespace

    fwd = make_forwarder(copilot)
    listener = CopilotCallListener(fwd, conversation_enabled=True)
    await listener.on_stream_start(SimpleNamespace(kind="primary", call_id=CALL))
    await listener.on_stream_start(SimpleNamespace(kind="monitor", call_id=CALL))  # monitor streams are not the AI
    await fwd.on_event(final(CALL, "hello", 1))
    await listener.on_call_ended(CALL, "hangup")
    assert [b["type"] for b in copilot.calls()] == ["ai_agent_connected", "call_ended"]
    assert fwd.snapshot(CALL) is None


async def test_replay_of_the_real_transfer_call_attributes_each_track_correctly(copilot) -> None:
    """Track lines from the real call of 2026-11-24 (customer = the caller, human = the person who answered)."""
    fwd = make_forwarder(copilot, vobiz_outbound_track_speaker="human_agent")
    fwd.emit_call_event(CALL, "call_started")
    await fwd.on_event(final(CALL, "Can I talk to human agent?", 1))                       # primary stream, before the transfer
    fwd.emit_call_event(CALL, "human_handoff_requested")
    await fwd.on_event(partial(CALL, "I", 2, track=TRACK_OUTBOUND, stream_id="mon"))        # while it is still ringing
    fwd.emit_call_event(CALL, "human_agent_dialing")
    fwd.emit_call_event(CALL, "human_agent_connected")                                      # the human answered
    await fwd.on_event(final(CALL, "Hello.", 3, track=TRACK_OUTBOUND, stream_id="mon"))
    await fwd.on_event(final(CALL, "Hello ma'am.", 4, track=TRACK_INBOUND, stream_id="mon"))
    await fwd.on_event(final(CALL, "Thank you.", 5, track=TRACK_INBOUND, stream_id="mon"))
    await drained(fwd)
    got = [(b["transcript_text"], b["speaker"], b["agent_mode"]) for b in copilot.transcripts()]
    assert got == [
        ("Can I talk to human agent?", "customer", "ai_agent"),
        ("I", "unknown", "ai_agent"),  # outbound audio before anyone answered is never credited to the human
        ("Hello.", "human_agent", "human_agent"),
        ("Hello ma'am.", "customer", "human_agent"),
        ("Thank you.", "customer", "human_agent"),
    ]
    # customer speech is never labelled as the human agent, on any track
    assert all(b["speaker"] != "human_agent" for b in copilot.transcripts() if b["transcript_text"] in ("Hello ma'am.", "Thank you."))
