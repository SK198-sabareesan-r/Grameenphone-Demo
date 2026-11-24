"""The real create_app() wiring of the Copilot forwarder: is it registered, enabled, and fed by real hooks?

The first M3 tests built the forwarder by hand. These go through create_app() with a mocked Copilot, so a
missing registration or a disabled forwarder (what an empty COPILOT_TRANSCRIPT_WEBHOOK_URL causes) is caught.
"""

from __future__ import annotations

import json
import logging

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.main import create_app
from tests.ai_fakes import final, partial
from tests.test_vobiz_websocket import CALL, HEADERS, make_settings, open_stream, wait_until

URL = "http://copilot.test/api/v1/internal/transcripts/events"
ANSWER = {
    "CallUUID": CALL, "From": "+8801700123456", "To": "+8809600000000", "Direction": "inbound",
    "Event": "StartApp", "CallStatus": "in-progress",
}


def build(sent: list, **over):
    async def copilot(request: httpx.Request) -> httpx.Response:
        sent.append((request.url.path, request.headers.get("x-internal-token"), json.loads(request.content)))
        return httpx.Response(201, json={"status": "stored"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(copilot))
    settings = make_settings(copilot_transcript_webhook_url=URL, **over)
    return create_app(settings, copilot_http_client=client), settings


def test_the_forwarder_is_registered_on_the_real_pipeline_and_receives_stt_events() -> None:
    sent: list = []
    app, settings = build(sent)
    with TestClient(app) as client:
        assert app.state.copilot.enabled
        with open_stream(client):
            pipeline = app.state.transcript_pipeline
            client.portal.call(pipeline.publish, partial(CALL, "amar net", 1))
            client.portal.call(pipeline.publish, final(CALL, "amar net kaj korche na", 2))
            wait_until(lambda: sum(1 for p, _, _ in sent if p.endswith("/transcripts/events")) >= 2)
    transcripts = [b for p, _, b in sent if p.endswith("/transcripts/events")]
    assert [(b["transcript_type"], b["speaker"]) for b in transcripts] == [("partial", "customer"), ("final", "customer")]
    assert {t for _, t, _ in sent} == {settings.internal_api_token.get_secret_value()}  # the token travels as a header


def test_the_answer_webhook_reports_call_started_and_hangup_reports_call_ended() -> None:
    sent: list = []
    app, _ = build(sent)
    with TestClient(app) as client:
        assert client.post("/api/vobiz/answer", data=ANSWER).status_code == 200
        wait_until(lambda: any(b.get("type") == "call_started" for _, _, b in sent))
        started = next(b for _, _, b in sent if b.get("type") == "call_started")
        assert started["data"]["from_number"] == "+8801700123456" and started["call_id"] == CALL
        client.post("/api/vobiz/hangup", data={"CallUUID": CALL, "Direction": "inbound", "Event": "Hangup", "CallStatus": "completed"})
        wait_until(lambda: any(b.get("type") == "call_ended" for _, _, b in sent))


def test_a_stream_start_reports_the_ai_agent_connected_when_the_ai_conversation_is_on() -> None:
    from tests.ai_fakes import FakeLlm, FakeTts
    from tests.test_ai_conversation_app import null_factory

    sent: list = []
    settings = make_settings(copilot_transcript_webhook_url=URL, ai_conversation_enabled=True, sarvam_api_key=SecretStr("k"))
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: (sent.append((r.url.path, None, json.loads(r.content))), httpx.Response(201))[1]))
    app = create_app(settings, copilot_http_client=http, conversation_llm=FakeLlm(), conversation_tts=FakeTts(),
                     audio_sink_factory=null_factory)
    with TestClient(app) as client:
        with open_stream(client):
            wait_until(lambda: any(b.get("type") == "ai_agent_connected" for _, _, b in sent))


def test_an_empty_webhook_url_disables_forwarding_and_says_so_loudly() -> None:
    records: list[logging.LogRecord] = []

    class Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler, log = Grab(level=logging.INFO), logging.getLogger("app.main")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        app = create_app(make_settings(copilot_transcript_webhook_url=""))
    finally:
        log.removeHandler(handler)
    info = app.state.copilot.describe()
    assert info["enabled"] is False and info["disabled_reason"] == "COPILOT_TRANSCRIPT_WEBHOOK_URL is empty"
    warnings = [r for r in records if r.levelno == logging.WARNING and "copilot_forwarding_disabled" in r.getMessage()]
    assert warnings and warnings[0].reason == "COPILOT_TRANSCRIPT_WEBHOOK_URL is empty"
    # and it never feeds the pipeline, so nothing could be sent
    assert all(name != "copilot" for name, _ in app.state.transcript_pipeline._global)


def test_a_missing_token_is_reported_as_the_reason() -> None:
    app = create_app(make_settings(copilot_transcript_webhook_url=URL, internal_api_token=SecretStr("")))
    assert app.state.copilot.describe()["disabled_reason"] == "INTERNAL_API_TOKEN is empty"


def test_diagnostics_show_the_forwarding_state_without_the_token() -> None:
    sent: list = []
    app, settings = build(sent)
    with TestClient(app) as client:
        body = client.get("/api/vobiz/streams", headers=HEADERS)
        assert body.status_code == 200
        info = body.json()["copilot_forwarding"]
        assert info["enabled"] is True and info["transcripts_url"] == URL and info["token_set"] is True
        assert settings.internal_api_token.get_secret_value() not in body.text


def test_a_copilot_that_is_down_does_not_stall_the_stream_or_the_pipeline() -> None:
    async def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client_http = httpx.AsyncClient(transport=httpx.MockTransport(down))
    app = create_app(make_settings(copilot_transcript_webhook_url=URL, copilot_forward_max_attempts=2,
                                   copilot_forward_backoff_s=0.01), copilot_http_client=client_http)
    with TestClient(app) as client:
        seen = []
        app.state.transcript_pipeline.add_global_subscriber(lambda e: seen.append(e.sequence), "probe")
        with open_stream(client) as stream:
            for i in range(1, 6):
                client.portal.call(app.state.transcript_pipeline.publish, final(CALL, f"line {i}", i))
            wait_until(lambda: len(seen) == 5)  # other subscribers still get everything
            assert not stream.session.closed  # and the stream is untouched
