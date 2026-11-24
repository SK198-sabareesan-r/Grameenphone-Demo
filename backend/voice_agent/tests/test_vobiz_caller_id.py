"""Caller ID configuration for the existing Vobiz number (+919262175535). Vobiz is mocked; no call is placed."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.routes import vobiz as vobiz_routes
from app.api.routes import vobiz_config
from app.core.config import Settings
from app.integrations.vobiz.client import VobizClient
from app.main import create_app

CALLER_ID = "+919262175535"
AUTH_ID = "MA_TESTACCOUNT1234"
VOBIZ_TOKEN = "vobiz-secret-token"
INTERNAL = "internal-secret-token"
BASE = "https://abc123.ngrok-free.app"
DEST = "+919876543210"  # Indian destination: Vobiz India rules require IN -> IN
HEADERS = {"X-Internal-Token": INTERNAL}


def make_settings(**overrides) -> Settings:
    values = dict(
        vobiz_auth_id=AUTH_ID,
        vobiz_auth_token=SecretStr(VOBIZ_TOKEN),
        vobiz_caller_id=CALLER_ID,
        public_base_url=BASE,
        internal_api_token=SecretStr(INTERNAL),
    )
    values.update(overrides)
    return Settings(**values)


class FakeVobiz:
    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status
        self.body = body if body is not None else {"api_id": "api-1", "message": "Call fired", "request_uuid": "uuid-1"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)


def build(monkeypatch: pytest.MonkeyPatch, fake: FakeVobiz, settings: Settings | None = None) -> TestClient:
    def factory(s: Settings) -> VobizClient:
        http = httpx.AsyncClient(base_url=s.vobiz_base_url + "/", transport=httpx.MockTransport(fake.handler))
        return VobizClient(s, http_client=http)

    monkeypatch.setattr(vobiz_routes, "VobizClient", factory)
    monkeypatch.setattr(vobiz_config, "VobizClient", factory)
    return TestClient(create_app(settings or make_settings()))


# 1. loaded from configuration


def test_existing_number_is_loaded_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.core.config.load_dotenv", lambda *_a, **_k: None)
    monkeypatch.setenv("VOBIZ_CALLER_ID", CALLER_ID)
    assert Settings.from_env().vobiz_caller_id == CALLER_ID


def test_env_example_documents_caller_id_with_a_placeholder() -> None:
    with open("config/.env.example", encoding="utf-8") as fh:
        lines = [line.strip() for line in fh]
    assert "VOBIZ_CALLER_ID=+91XXXXXXXXXX" in lines
    assert f"VOBIZ_CALLER_ID={CALLER_ID}" not in lines  # no real number in the committed example


# 2-5. request construction


def test_outbound_request_uses_configured_number_and_public_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeVobiz()
    response = build(monkeypatch, fake).post("/api/v1/vobiz/calls", json={"to": DEST}, headers=HEADERS)

    assert response.status_code == 202 and response.json()["call_uuid"] == "uuid-1"
    assert len(fake.requests) == 1
    sent = fake.requests[0]
    assert sent.method == "POST"
    assert str(sent.url) == f"https://api.vobiz.ai/api/v1/Account/{AUTH_ID}/Call/"
    body = json.loads(sent.content)
    assert body["from"] == CALLER_ID
    assert body["to"] == DEST
    assert body["answer_url"] == f"{BASE}/api/v1/vobiz/answer" and body["answer_method"] == "POST"
    assert body["hangup_url"] == f"{BASE}/api/v1/vobiz/hangup" and body["hangup_method"] == "POST"
    assert set(body) == {"from", "to", "answer_url", "answer_method", "hangup_url", "hangup_method"}


def test_caller_id_without_plus_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeVobiz()
    build(monkeypatch, fake, make_settings(vobiz_caller_id="919262175535")).post(
        "/api/v1/vobiz/calls", json={"to": DEST}, headers=HEADERS
    )
    assert json.loads(fake.requests[0].content)["from"] == CALLER_ID


# 6-7. missing / invalid caller ID


@pytest.mark.parametrize("caller_id", ["", "9262175535x", "+91 92621 75535", "091-9262175535", "+0919262175535"])
def test_missing_or_invalid_caller_id_is_rejected_without_calling_vobiz(
    monkeypatch: pytest.MonkeyPatch, caller_id: str
) -> None:
    fake = FakeVobiz()
    response = build(monkeypatch, fake, make_settings(vobiz_caller_id=caller_id)).post(
        "/api/v1/vobiz/calls", json={"to": DEST}, headers=HEADERS
    )
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "vobiz_config_error"
    assert fake.requests == []


# 8. caller ID cannot be overridden


@pytest.mark.parametrize(
    "extra",
    [{"from": "+14155550100"}, {"from_number": "+14155550100"}, {"caller_id": "+14155550100"},
     {"answer_url": "https://evil.example/a"}, {"hangup_url": "https://evil.example/h"}],
)
def test_request_cannot_override_caller_id_or_webhooks(monkeypatch: pytest.MonkeyPatch, extra: dict) -> None:
    fake = FakeVobiz()
    response = build(monkeypatch, fake).post("/api/v1/vobiz/calls", json={"to": DEST, **extra}, headers=HEADERS)
    assert response.status_code == 422
    assert fake.requests == []


# 9. internal authentication


@pytest.mark.parametrize("headers", [{}, {"X-Internal-Token": "wrong"}, {"X-Internal-Token": ""}])
def test_internal_auth_still_enforced(monkeypatch: pytest.MonkeyPatch, headers: dict) -> None:
    fake = FakeVobiz()
    response = build(monkeypatch, fake).post("/api/v1/vobiz/calls", json={"to": DEST}, headers=headers)
    assert response.status_code == 401
    assert fake.requests == []


# 10. duplicate protection


def test_duplicate_protection_still_active(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeVobiz()
    client = build(monkeypatch, fake)
    assert client.post("/api/v1/vobiz/calls", json={"to": DEST}, headers=HEADERS).status_code == 202
    second = client.post("/api/v1/vobiz/calls", json={"to": DEST}, headers=HEADERS)
    assert second.status_code == 409 and second.json()["error"]["type"] == "vobiz_duplicate_call"
    assert len(fake.requests) == 1


def test_not_owned_caller_id_is_surfaced_with_a_hint_and_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeVobiz(400, {"error": "From number not owned by account"})
    response = build(monkeypatch, fake).post("/api/v1/vobiz/calls", json={"to": DEST}, headers=HEADERS)
    assert response.status_code == 400
    assert "VOBIZ_CALLER_ID" in response.json()["error"]["message"]
    assert len(fake.requests) == 1


# 11. no Voice Application linking is assumed


def test_outbound_call_needs_no_application_or_number_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeVobiz()
    build(monkeypatch, fake).post("/api/v1/vobiz/calls", json={"to": DEST}, headers=HEADERS)
    paths = [r.url.path for r in fake.requests]
    assert paths == [f"/api/v1/Account/{AUTH_ID}/Call/"]  # one Make Call request, nothing else
    assert not any("Application" in p or "numbers" in p for p in paths)
    assert "app_id" not in json.loads(fake.requests[0].content)


# diagnostics


def test_diagnostics_reports_caller_id_status_without_revealing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    client = build(monkeypatch, FakeVobiz(200, {"currency": "INR", "status": "active", "balance": 900}))
    response = client.get("/api/v1/vobiz/diagnostics?verify=true")
    assert response.status_code == 200
    data = response.json()
    calls = data["calls"]
    assert calls["caller_id_configured"] is True and calls["caller_id_valid_e164"] is True
    assert calls["caller_id_hint"] == "***5535"
    assert calls["internal_api_token_configured"] is True and calls["public_base_url_configured"] is True
    assert data["connectivity"]["ok"] is True and data["connectivity"]["currency"] == "INR"
    assert any("India" in w and "media anchoring" in w for w in data["warnings"])
    for secret in ("9262175535", VOBIZ_TOKEN, INTERNAL, AUTH_ID):
        assert secret not in response.text


def test_diagnostics_flags_invalid_caller_id() -> None:
    data = TestClient(create_app(make_settings(vobiz_caller_id="not-a-number"))).get("/api/v1/vobiz/diagnostics").json()
    assert data["calls"]["caller_id_configured"] is True and data["calls"]["caller_id_valid_e164"] is False
    assert any("not a valid E.164" in w for w in data["warnings"])


def test_diagnostics_without_caller_id() -> None:
    data = TestClient(create_app(make_settings(vobiz_caller_id=""))).get("/api/v1/vobiz/diagnostics").json()
    assert data["calls"]["caller_id_configured"] is False and data["calls"]["caller_id_hint"] is None
    assert any("VOBIZ_CALLER_ID is not set" in w for w in data["warnings"])


def test_logs_do_not_contain_full_numbers(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    client = build(monkeypatch, FakeVobiz())
    logging.getLogger().addHandler(caplog.handler)  # create_app() replaced the root handlers
    caplog.set_level(logging.DEBUG)
    client.post("/api/v1/vobiz/calls", json={"to": DEST}, headers=HEADERS)
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records if r.name.startswith("app."))
    assert "vobiz_call_created" in text
    for secret in ("9262175535", "9876543210", VOBIZ_TOKEN, INTERNAL):
        assert secret not in text
