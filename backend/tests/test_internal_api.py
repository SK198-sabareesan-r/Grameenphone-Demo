"""POST /api/v1/internal/transcripts/events and /calls/events: authentication, validation, storage, panel."""

import json

import pytest

from datetime import datetime, timezone

from tests.conftest import INTERNAL_TOKEN

T_URL = "/api/v1/internal/transcripts/events"
C_URL = "/api/v1/internal/calls/events"
TOKEN = {"X-Internal-Token": INTERNAL_TOKEN}
ANON = {"Authorization": ""}  # the agent login must not matter for internal routes


def transcript(n=1, **over):
    body = {
        "event_id": f"CALL_9-t{n}", "call_id": "CALL_9", "session_id": "sess-CALL_9", "speaker": "customer",
        "speaker_basis": "primary_stream_inbound_track", "transcript_text": f"line {n}", "transcript_type": "final",
        "timestamp": "2026-11-24T10:30:00Z", "sequence_number": n, "call_status": "in_progress", "agent_mode": "ai_agent",
    }
    body.update(over)
    return body


def call_event(kind, n=1, ts="2026-11-24T10:30:00Z", **over):
    body = {"event_id": f"CALL_9-{kind}-{n}", "call_id": "CALL_9", "session_id": "sess-CALL_9", "type": kind,
            "timestamp": ts, "source": "voice_agent", "data": {}}
    body.update(over)
    return body


def read_json(settings, name, call="CALL_9"):
    path = settings.conversations_dir / call / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---- authentication -----------------------------------------------------------------------------


@pytest.mark.parametrize("url,body", [(T_URL, transcript()), (C_URL, call_event("call_started"))])
def test_requests_without_the_internal_token_are_rejected(client, url, body):
    assert client.post(url, json=body, headers=ANON).status_code == 401
    assert client.post(url, json=body, headers={**ANON, "X-Internal-Token": "wrong"}).status_code == 401
    # A signed-in agent session is not an internal credential.
    assert client.post(url, json=body).status_code == 401


def test_the_internal_routes_fail_closed_when_no_token_is_configured(runtime_dir, users_file, fake_bedrock):
    from tests.conftest import make_settings, open_client

    with open_client(make_settings(runtime_dir, internal_api_token=""), fake_bedrock) as c:
        assert c.post(T_URL, json=transcript(), headers=TOKEN).status_code == 503


def test_the_token_is_not_echoed_in_error_responses(client):
    response = client.post(T_URL, json=transcript(), headers={"X-Internal-Token": "wrong"})
    assert INTERNAL_TOKEN not in response.text and "wrong" not in response.text


# ---- validation ---------------------------------------------------------------------------------


@pytest.mark.parametrize("change", [
    {"call_id": "../evil"}, {"call_id": ""}, {"session_id": "has space"}, {"speaker": "robot"},
    {"transcript_type": "interim"}, {"transcript_text": ""}, {"sequence_number": -1}, {"timestamp": "yesterday"},
    {"timestamp": "2026-11-24T10:30:00"}, {"agent_mode": "bot"}, {"call_status": "weird"}, {"surprise": 1},
])
def test_invalid_transcript_events_are_rejected_and_nothing_is_written(client, settings, change):
    response = client.post(T_URL, json=transcript(**change), headers=TOKEN)
    assert response.status_code == 422
    assert not (settings.conversations_dir / "CALL_9").exists()


def test_call_events_with_an_unknown_type_are_rejected(client):
    assert client.post(C_URL, json=call_event("call_exploded"), headers=TOKEN).status_code == 422


def test_secret_looking_keys_are_dropped_from_call_event_data(client, settings):
    body = call_event("ai_agent_connected", data={"api_key": "SECRET-1", "auth_token": "SECRET-2", "note": "ok"})
    assert client.post(C_URL, json=body, headers=TOKEN).status_code == 201
    stored = json.dumps(read_json(settings, "call_events"))
    assert "SECRET-1" not in stored and "SECRET-2" not in stored and '"note": "ok"' in stored


# ---- transcript events --------------------------------------------------------------------------


def test_a_valid_event_is_created_then_a_repeat_is_a_safe_duplicate(client, settings):
    first = client.post(T_URL, json=transcript(), headers=TOKEN)
    assert first.status_code == 201 and first.json() == {"status": "stored", "event_id": "CALL_9-t1"}
    again = client.post(T_URL, json=transcript(), headers=TOKEN)
    assert again.status_code == 200 and again.json()["status"] == "duplicate"
    assert len(read_json(settings, "transcript")["events"]) == 1


def test_a_second_session_for_the_same_call_or_a_reused_sequence_is_a_conflict(client):
    client.post(T_URL, json=transcript(), headers=TOKEN)
    assert client.post(T_URL, json=transcript(2, session_id="sess-OTHER"), headers=TOKEN).status_code == 409
    assert client.post(T_URL, json=transcript(1, event_id="CALL_9-different"), headers=TOKEN).status_code == 409


def test_a_write_failure_is_a_retryable_503_and_the_panel_is_not_updated(client, settings, monkeypatch):
    from copilot.services import conversation_store as cs

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(cs, "atomic_write_json", boom)
    response = client.post(T_URL, json=transcript(), headers=TOKEN)
    assert response.status_code == 503 and response.headers["retry-after"]
    assert client.get("/api/calls/current").json()["call"] is None  # nothing half-applied
    monkeypatch.undo()
    assert client.post(T_URL, json=transcript(), headers=TOKEN).status_code == 201  # the retry works


def test_partial_events_are_stored_but_do_not_create_panel_turns(client, settings):
    client.post(T_URL, json=transcript(1, transcript_type="partial", transcript_text="I need"), headers=TOKEN)
    assert read_json(settings, "transcript")["events"][0]["transcript_type"] == "partial"
    assert client.get("/api/calls/current").json()["call"]["turns"] == []


def test_final_events_reach_the_panel_with_their_own_timestamp_and_speaker(client):
    client.post(T_URL, json=transcript(1, transcript_text="Net kaj korche na"), headers=TOKEN)
    client.post(T_URL, json=transcript(2, speaker="ai_agent", transcript_text="Let me check"), headers=TOKEN)
    turns = client.get("/api/calls/current").json()["call"]["turns"]
    assert [(t["speaker"], t["text"]) for t in turns] == [("customer", "Net kaj korche na"), ("ai_agent", "Let me check")]
    assert turns[0]["ts"] == datetime(2026, 11, 24, 10, 30, tzinfo=timezone.utc).timestamp()  # event time, not arrival time


def test_unverified_speech_is_shown_and_stored_but_never_analysed(client, settings, fake_bedrock):
    client.post(T_URL, json=transcript(1, speaker="unknown", speaker_basis="outbound_track_unverified",
                                       transcript_text="mystery voice"), headers=TOKEN)
    assert client.get("/api/calls/current").json()["call"]["turns"][0]["speaker"] == "unknown"
    stored = read_json(settings, "transcript")["events"][0]
    assert stored["speaker"] == "unknown" and stored["speaker_basis"] == "outbound_track_unverified"
    folder = settings.conversations_dir / "CALL_9"  # in nobody's context: no context file was even created
    assert not (folder / "customer_context.json").exists() and not (folder / "human_agent_context.json").exists()
    assert fake_bedrock.calls == []


def test_speakers_land_in_their_own_context_files(client, settings):
    client.post(T_URL, json=transcript(1, transcript_text="I need help with my recharge"), headers=TOKEN)
    client.post(T_URL, json=transcript(2, speaker="ai_agent", transcript_text="Sure"), headers=TOKEN)
    client.post(T_URL, json=transcript(3, speaker="human_agent", agent_mode="human_agent", transcript_text="Hello"), headers=TOKEN)
    customer, ai, human = (read_json(settings, n) for n in ("customer_context", "ai_agent_context", "human_agent_context"))
    assert customer["conversation_history_refs"] == ["CALL_9-t1"] and customer["initial_intent"] == "I need help with my recharge"
    assert [m["text"] for m in ai["messages"]] == ["Sure"]
    assert human["transcript_refs"] == ["CALL_9-t3"]
    assert "Hello" not in json.dumps(ai) and "Sure" not in json.dumps(human)  # never mixed


# ---- lifecycle events ---------------------------------------------------------------------------


def test_call_started_resolves_the_customer_and_keeps_the_original_number_only_in_customer_context(client, settings):
    client.post(C_URL, json=call_event("call_started", data={"from_number": "+8801200000001", "direction": "inbound"}), headers=TOKEN)
    customer = read_json(settings, "customer_context")
    assert customer["customer"]["id"] == customer["customer_id"] == "rahim"
    assert customer["caller_ref"] == "***0001"
    assert customer["caller_number"] == "+8801200000001"  # the original number, for the call-transcripts bucket
    assert "+8801200000001" not in json.dumps(read_json(settings, "call_events"))  # the event log keeps it masked
    assert client.get("/api/calls/current").json()["call"]["agent_mode"] == "ai_agent"


def test_handoff_events_fill_the_contexts_and_move_the_panel_status(client, settings):
    client.post(C_URL, json=call_event("call_started"), headers=TOKEN)
    client.post(T_URL, json=transcript(1, transcript_text="I want a refund"), headers=TOKEN)
    client.post(C_URL, json=call_event("human_handoff_requested", data={
        "reason": "refund needs a human", "requested_by": "ai_agent", "pending_actions": ["Approve refund"]}), headers=TOKEN)
    assert client.get("/api/calls/current").json()["call"]["status"] == "handoff_requested"
    client.post(C_URL, json=call_event("human_agent_dialing", data={"agent_ref": "+8801911000999"}), headers=TOKEN)
    client.post(C_URL, json=call_event("human_agent_connected", data={"agent_ref": "+8801911000999"}), headers=TOKEN)
    call = client.get("/api/calls/current").json()["call"]
    assert (call["status"], call["agent_mode"]) == ("with_human", "human_agent")

    ai, human = read_json(settings, "ai_agent_context"), read_json(settings, "human_agent_context")
    assert ai["handoff"]["reason"] == "refund needs a human" and ai["handoff"]["pending_actions"] == ["Approve refund"]
    assert ai["handoff"]["requested_at"]
    assert human["handoff"]["status"] == "connected" and human["handoff"]["accepted_at"]
    assert human["agent_ref"] == "***0999" and "+8801911000999" not in json.dumps(human)
    types = [e["type"] for e in read_json(settings, "call_events")["events"]]
    assert "ai_agent_assist_activated" in types  # the Copilot itself records that background assist is on
    assert human["assist_activated_at"]


def test_a_repeated_connect_event_does_not_activate_assist_twice(client, settings):
    for _ in range(3):
        client.post(C_URL, json=call_event("human_agent_connected"), headers=TOKEN)
    types = [e["type"] for e in read_json(settings, "call_events")["events"]]
    assert types.count("human_agent_connected") == 1 and types.count("ai_agent_assist_activated") == 1


def test_hold_and_resume_update_status_and_the_human_context(client, settings):
    client.post(C_URL, json=call_event("human_agent_connected"), headers=TOKEN)
    client.post(C_URL, json=call_event("customer_put_on_hold", data={"source": "human_agent_dtmf"}), headers=TOKEN)
    assert client.get("/api/calls/current").json()["call"]["status"] == "on_hold"
    client.post(C_URL, json=call_event("customer_resumed", data={"source": "human_agent_dtmf"}), headers=TOKEN)
    assert client.get("/api/calls/current").json()["call"]["status"] == "with_human"
    holds = read_json(settings, "human_agent_context")["hold_events"]
    assert [h["type"] for h in holds] == ["customer_put_on_hold", "customer_resumed"]
    assert holds[0]["source"] == "human_agent_dtmf"


def test_a_failed_handoff_returns_the_call_to_the_ai(client, settings):
    client.post(C_URL, json=call_event("human_handoff_requested", data={"reason": "x"}), headers=TOKEN)
    client.post(C_URL, json=call_event("human_handoff_failed", data={"reason": "no-answer"}), headers=TOKEN)
    call = client.get("/api/calls/current").json()["call"]
    assert (call["status"], call["agent_mode"]) == ("handoff_failed", "ai_agent")
    assert read_json(settings, "human_agent_context")["handoff"]["failure"] == "no-answer"


def test_call_ended_closes_the_call_on_the_panel(client, settings):
    client.post(C_URL, json=call_event("call_started"), headers=TOKEN)
    client.post(C_URL, json=call_event("call_ended", data={"reason": "hangup"}), headers=TOKEN)
    call = client.get("/api/calls/current").json()["call"]
    assert call["ended"] is True and call["status"] == "ended"
    assert client.post(T_URL, json=transcript(9), headers=TOKEN).status_code == 201  # a late event is still stored
    assert len(read_json(settings, "transcript")["events"]) == 1


# ---- agent-facing context ----------------------------------------------------------------------


def test_the_agent_can_read_the_handoff_context_and_add_notes(client, settings):
    client.post(T_URL, json=transcript(1, transcript_text="I want a refund"), headers=TOKEN)
    client.post(C_URL, json=call_event("human_handoff_requested", data={"reason": "refund"}), headers=TOKEN)
    context = client.get("/api/calls/CALL_9/context")
    assert context.status_code == 200 and context.json()["handoff_reason"] == "refund"
    assert context.json()["initial_intent"] == "I want a refund"
    note = client.post("/api/calls/CALL_9/notes", json={"text": "Customer is calm"})
    assert note.status_code == 201
    assert read_json(settings, "human_agent_context")["notes"][0]["text"] == "Customer is calm"
    assert read_json(settings, "human_agent_context")["notes"][0]["author"] == "tania"


def test_context_and_notes_need_a_signed_in_agent_and_a_known_call(client):
    assert client.get("/api/calls/CALL_9/context").status_code == 404
    assert client.post("/api/calls/CALL_9/notes", json={"text": "x"}).status_code == 404
    assert client.get("/api/calls/CALL_9/context", headers=ANON).status_code == 401
    assert client.get("/api/calls/..%2Fevil/context").status_code in (404, 422)
