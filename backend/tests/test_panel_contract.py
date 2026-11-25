"""Pins the panel contract the React frontend depends on: event names, payload keys, routes."""

import re

from config.settings import BASE_DIR
from copilot.services.summarizer import EMPTY_DRAFT
from tests.conftest import FAST_ARN

SESSION_VIEW_KEYS = {
    "call_sid", "ended", "status", "agent_mode", "caller_number", "current_location", "location_prompt", "heard", "turns", "card", "mood", "compliance",
    "suggestions", "answers", "summary", "ticket", "recording_url",
}
FINAL_TURN_KEYS = {"id", "speaker", "text", "ts", "kind"}
REDUCER = BASE_DIR.parent / "frontend" / "src" / "state" / "callReducer.js"


def _backend_event_names() -> set[str]:
    names: set[str] = set()
    for path in (BASE_DIR / "copilot").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        names |= set(re.findall(r"""broadcast\(\s*"([\w.]+)\"""", source))
        names |= set(re.findall(r"""hub\.send\([^,]+,\s*"([\w.]+)\"""", source))
    return names


def test_every_backend_event_is_handled_by_the_frontend_reducer_and_vice_versa():
    reducer_cases = set(re.findall(r'case "([\w.]+)":', REDUCER.read_text(encoding="utf-8")))
    backend = _backend_event_names()
    assert backend == reducer_cases
    assert {"snapshot", "call.started", "transcript", "card", "mood", "compliance",
            "suggestions", "call.ended", "summary", "ticket.created"} <= backend


def test_panel_stream_sends_no_snapshot_when_there_is_no_call_then_live_events(client, fake_bedrock):
    fake_bedrock.responses[FAST_ARN] = {
        "customer_mood": "frustrated", "agent_mood": "neutral", "tip": "Apologise first.",
        "suggestion_id": "nba-network-apology",
    }
    with client.websocket_connect("/api/panel/stream") as ws:
        response = client.post("/api/transcript", json={"speaker": "customer", "text": "Bhai, amar net kaj korche na."})
        assert response.status_code == 200
        started = ws.receive_json()
        assert started["type"] == "call.started"  # first message: nothing was sent before the call
        assert set(started["data"]) == SESSION_VIEW_KEYS
        assert started["data"]["turns"] == [] and started["data"]["ended"] is False

        transcript = ws.receive_json()
        assert transcript["type"] == "transcript"
        assert set(transcript["data"]) == FINAL_TURN_KEYS
        assert transcript["data"]["kind"] == "final" and transcript["data"]["speaker"] == "customer"

        prompt = ws.receive_json()  # no card: the agent confirms the mobile number first. A network complaint asks for the location.
        assert prompt["type"] == "location.prompt"

        def next_event():  # the live lookup (answer.*) runs alongside analysis; order is not fixed
            while (msg := ws.receive_json())["type"].startswith("answer"):
                pass
            return msg

        mood = next_event()
        assert mood == {"type": "mood", "data": {"customer": "frustrated", "agent": "neutral", "tip": "Apologise first."}}
        suggestions = next_event()
        assert suggestions["type"] == "suggestions"
        assert [set(s) for s in suggestions["data"]] == [{"id", "title", "text", "source", "used"}]


def test_reconnecting_panel_gets_a_full_snapshot(client):
    client.post("/api/transcript", json={"speaker": "agent", "text": "Assalamu alaikum."})
    with client.websocket_connect("/api/panel/stream") as ws:
        snapshot = ws.receive_json()
    assert snapshot["type"] == "snapshot"
    assert set(snapshot["data"]) == SESSION_VIEW_KEYS
    assert [t["text"] for t in snapshot["data"]["turns"]] == ["Assalamu alaikum."]
    assert set(snapshot["data"]["turns"][0]) == {"id", "speaker", "text", "ts"}


def test_current_call_route_shape(client):
    assert client.get("/api/calls/current").json() == {"call": None}
    client.post("/api/transcript", json={"speaker": "customer", "text": "Hello"})
    body = client.get("/api/calls/current").json()
    assert set(body["call"]) == SESSION_VIEW_KEYS


def test_ending_the_call_broadcasts_call_ended_then_the_summary_draft(client):
    client.post("/api/transcript", json={"speaker": "customer", "text": "Net kaj korche na."})
    with client.websocket_connect("/api/panel/stream") as ws:
        assert ws.receive_json()["type"] == "snapshot"
        assert client.post("/api/calls/current/end").json() == {"ok": True}
        seen = []
        while True:
            message = ws.receive_json()
            seen.append(message["type"])
            if message["type"] == "summary":
                break
    assert "call.ended" in seen and seen.index("call.ended") < seen.index("summary")
    assert set(message["data"]) == set(EMPTY_DRAFT)
    assert client.post("/api/calls/current/end").status_code == 409  # already ended


def test_end_call_without_a_call_is_409(client):
    assert client.post("/api/calls/current/end").status_code == 409


def test_rest_routes_the_frontend_calls_exist(client):
    # HTTP routes from the OpenAPI schema; /api/panel/stream (WebSocket) is covered by the tests above.
    paths = set(client.get("/openapi.json").json()["paths"])
    assert {
        "/api/health", "/api/calls/current", "/api/calls/current/end",
        "/api/transcript", "/api/analyze", "/api/summary", "/api/tickets", "/api/tickets/categories",
        "/api/ask", "/api/suggestions/{suggestion_id}/use", "/api/identity", "/api/locations", "/api/location",
        "/api/dashboard", "/api/data/reset",
    } <= paths


def test_ticket_categories_and_ticket_created_event(client):
    assert client.get("/api/tickets/categories").json()["categories"] == [
        "Network", "Billing", "Pack", "SIM", "Roaming", "Other"
    ]
    client.post("/api/transcript", json={"speaker": "customer", "text": "Hello"})
    call_sid = client.get("/api/calls/current").json()["call"]["call_sid"]
    with client.websocket_connect("/api/panel/stream") as ws:
        ws.receive_json()  # snapshot
        response = client.post("/api/tickets", json={"call_sid": call_sid, "customer_id": "rahim",
                                                     "category": "Network", "subject": "Net down"})
        assert response.status_code == 200
        event = ws.receive_json()
        while event["type"] != "ticket.created":  # background analysis may broadcast first
            event = ws.receive_json()
    assert event["data"]["ticket_id"] == "TKT-10234"
    assert client.post("/api/tickets", json={"category": "Nope"}).status_code == 422
