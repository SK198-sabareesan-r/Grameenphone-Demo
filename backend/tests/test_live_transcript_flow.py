"""Live transcript, end to end through the real Copilot app: internal API -> JSON -> panel WebSocket -> the real
frontend reducer (run with Node). Mocked: only the outside world (no Vobiz, Sarvam or AWS)."""

import json
import shutil
import subprocess

import pytest

from config.settings import BASE_DIR
from tests.conftest import INTERNAL_TOKEN

T_URL = "/api/v1/internal/transcripts/events"
C_URL = "/api/v1/internal/calls/events"
TOKEN = {"X-Internal-Token": INTERNAL_TOKEN}
CALL = "DIAG-flow-1"
HELPER = BASE_DIR.parent / "frontend" / "tests" / "helpers" / "reduce_events.mjs"


def transcript(n, text, kind="final", speaker="customer"):
    return {"event_id": f"{CALL}-t{n}", "call_id": CALL, "session_id": f"sess-{CALL}", "speaker": speaker,
            "speaker_basis": "primary_stream_inbound_track", "transcript_text": text, "transcript_type": kind,
            "timestamp": "2026-11-24T10:30:00Z", "sequence_number": n, "call_status": "in_progress", "agent_mode": "ai_agent"}


def lifecycle(kind, **data):
    return {"event_id": f"{CALL}-{kind}", "call_id": CALL, "session_id": f"sess-{CALL}", "type": kind,
            "timestamp": "2026-11-24T10:30:00Z", "source": "voice_agent", "data": data}


def read_until(ws, done, limit=30):
    """Read panel messages until `done(messages)` is true. Bounded by the message count the server really sends."""
    messages = []
    while len(messages) < limit and not done(messages):
        messages.append(ws.receive_json())
    assert done(messages), [m["type"] for m in messages]
    return messages


def has_final(speaker):
    return lambda ms: any(m["type"] == "transcript" and m["data"]["kind"] == "final" and m["data"]["speaker"] == speaker for m in ms)


def reduce_with_node(messages):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, str(HELPER)], input=json.dumps(messages), capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_partial_then_final_reach_json_the_websocket_and_the_frontend_state(client, settings):
    with client.websocket_connect("/api/panel/stream") as ws:
        assert client.post(C_URL, json=lifecycle("call_started", from_number="+8801700123456"), headers=TOKEN).status_code == 201
        assert client.post(T_URL, json=transcript(1, "amar net", "partial"), headers=TOKEN).status_code == 201
        assert client.post(T_URL, json=transcript(2, "amar net kaj korche na"), headers=TOKEN).status_code == 201
        messages = read_until(ws, has_final("customer"))

    # 1. the call is registered first, then the lines arrive, partial before final
    kinds = [m["type"] for m in messages]
    assert kinds[0] == "call.started" and messages[0]["data"]["call_sid"] == CALL
    lines = [m["data"] for m in messages if m["type"] == "transcript"]
    assert [(d["kind"], d["speaker"]) for d in lines] == [("partial", "customer"), ("final", "customer")]
    assert lines[1]["text"] == "amar net kaj korche na" and lines[1]["ts"] > 0

    # 2. both events are in transcript.json (partials are stored too)
    stored = json.loads((settings.conversations_dir / CALL / "transcript.json").read_text(encoding="utf-8"))
    assert [(e["transcript_type"], e["text"]) for e in stored["events"]] == [
        ("partial", "amar net"), ("final", "amar net kaj korche na")]

    # 3. the real frontend reducer: "No call on screen" becomes an active call, partial becomes final
    steps = reduce_with_node(messages)
    assert steps[0]["active"] is True and steps[0]["statusText"] == "AI agent handling"
    partial_step = next(s for s in steps if s["partials"]["customer"] == "amar net")
    assert partial_step["turns"] == []
    last = steps[-1]
    assert last["partials"]["customer"] == "" and last["turns"] == [{"speaker": "customer", "text": "amar net kaj korche na"}]


def test_a_panel_that_connects_after_the_call_started_gets_it_in_the_snapshot(client):
    client.post(C_URL, json=lifecycle("call_started"), headers=TOKEN)
    client.post(T_URL, json=transcript(1, "hello there"), headers=TOKEN)
    with client.websocket_connect("/api/panel/stream") as ws:
        snapshot = ws.receive_json()
    assert snapshot["type"] == "snapshot" and snapshot["data"]["call_sid"] == CALL
    steps = reduce_with_node([snapshot])
    assert steps[0]["active"] is True and steps[0]["turns"][0]["text"] == "hello there"


def test_a_transcript_event_with_no_call_started_still_puts_the_call_on_screen(client):
    """If call_started was lost, the first transcript line must not be silently discarded."""
    with client.websocket_connect("/api/panel/stream") as ws:
        client.post(T_URL, json=transcript(1, "no start event first"), headers=TOKEN)
        first = ws.receive_json()
    assert first["type"] == "call.started" and first["data"]["call_sid"] == CALL


def test_a_wrong_token_is_rejected_and_nothing_reaches_the_panel_or_disk(client, settings):
    with client.websocket_connect("/api/panel/stream") as ws:
        for headers in ({"X-Internal-Token": "wrong"}, {}):
            assert client.post(T_URL, json=transcript(1, "x"), headers=headers).status_code == 401
            assert client.post(C_URL, json=lifecycle("call_started"), headers=headers).status_code == 401
        assert client.get("/api/calls/current").json()["call"] is None
        assert not (settings.conversations_dir / CALL).exists()
        # the panel is still usable afterwards: a correct request is delivered
        client.post(T_URL, json=transcript(1, "now valid"), headers=TOKEN)
        assert ws.receive_json()["type"] == "call.started"


def test_the_status_and_ai_to_human_transition_reach_the_frontend_state(client):
    with client.websocket_connect("/api/panel/stream") as ws:
        client.post(C_URL, json=lifecycle("call_started"), headers=TOKEN)
        client.post(C_URL, json=lifecycle("human_handoff_requested", reason="refund"), headers=TOKEN)
        client.post(C_URL, json=lifecycle("human_agent_connected"), headers=TOKEN)
        client.post(T_URL, json={**transcript(1, "Hello, this is the agent", speaker="human_agent"), "agent_mode": "human_agent"}, headers=TOKEN)
        messages = read_until(ws, has_final("human_agent"))
    steps = reduce_with_node(messages)
    assert [s["status"] for s in steps if s["type"] == "call.status"][-1] == "with_human"
    assert steps[-1]["agentMode"] == "human_agent" and steps[-1]["statusText"] == "Human agent connected · AI background assist"
    assert steps[-1]["turns"] == [{"speaker": "human_agent", "text": "Hello, this is the agent"}]
