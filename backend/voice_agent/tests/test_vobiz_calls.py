"""Milestone 2 tests: POST /api/vobiz/calls. Vobiz is mocked; no real call is ever placed."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.routes import vobiz as vobiz_routes
from app.core.config import Settings
from app.integrations.vobiz.client import VobizClient
from app.main import create_app

AUTH_ID = "MA_TESTACCOUNT1234"
VOBIZ_TOKEN = "vobiz-secret-token"
INTERNAL = "internal-secret-token"
DEST = "+8801711111234"
HEADERS = {"X-Internal-Token": INTERNAL}


def make_settings(**overrides) -> Settings:
    values = dict(
        vobiz_auth_id=AUTH_ID,
        vobiz_auth_token=SecretStr(VOBIZ_TOKEN),
        vobiz_caller_id="+14155551234",
        public_base_url="https://abc.ngrok.app",
        internal_api_token=SecretStr(INTERNAL),
    )
    values.update(overrides)
    return Settings(**values)


class FakeVobiz:
    """Records requests and replies with a configurable response."""

    def __init__(self, status: int = 200, body: dict | None = None, headers: dict | None = None,
                 raises: Exception | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._status, self._headers, self._raises = status, headers or {}, raises
        self._body = body if body is not None else {
            "api_id": "api-1", "message": "Call fired", "request_uuid": "call-uuid-1"
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._raises:
            raise self._raises
        return httpx.Response(self._status, json=self._body, headers=self._headers)

    @property
    def last_json(self) -> dict:
        return json.loads(self.requests[-1].content)


def build(monkeypatch: pytest.MonkeyPatch, fake: FakeVobiz, settings: Settings | None = None) -> TestClient:
    settings = settings or make_settings()

    def factory(s: Settings) -> VobizClient:
        http = httpx.AsyncClient(base_url=s.vobiz_base_url + "/", transport=httpx.MockTransport(fake.handler))
        return VobizClient(s, http_client=http)

    monkeypatch.setattr(vobiz_routes, "VobizClient", factory)
    return TestClient(create_app(settings))


# --- success ---------------------------------------------------------------------------------


def test_valid_outbound_call(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeVobiz()
    client = build(monkeypatch, fake)

    response = client.post("/api/vobiz/calls", json={"to": DEST}, headers=HEADERS)

    assert response.status_code == 202
    body = response.json()
    assert body["success"] is True
    assert body["call_uuid"] == "call-uuid-1"  # Vobiz request_uuid
    assert body["api_id"] == "api-1"
    assert body["to"] == "***1234"
    assert DEST not in response.text and VOBIZ_TOKEN not in response.text

    assert len(fake.requests) == 1
    sent = fake.requests[0]
    assert sent.method == "POST"
    assert str(sent.url) == f"https://api.vobiz.ai/api/v1/Account/{AUTH_ID}/Call/"
    assert sent.headers["X-Auth-ID"] == AUTH_ID
    assert sent.headers["X-Auth-Token"] == VOBIZ_TOKEN
    assert fake.last_json == {
        "from": "+14155551234",
        "to": DEST,
        "answer_url": "https://abc.ngrok.app/api/vobiz/answer",
        "answer_method": "POST",
        "hangup_url": "https://abc.ngrok.app/api/vobiz/hangup",
        "hangup_method": "POST",
    }


def test_destination_without_plus_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeVobiz()
    build(monkeypatch, fake).post("/api/vobiz/calls", json={"to": "8801711111234"}, headers=HEADERS)
    assert fake.last_json["to"] == DEST


def test_configured_webhook_paths_are_used(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeVobiz()
    settings = make_settings(vobiz_answer_webhook_path="/hooks/a", vobiz_hangup_webhook_path="/hooks/h")
    build(monkeypatch, fake, settings).post("/api/vobiz/calls", json={"to": DEST}, headers=HEADERS)
    assert fake.last_json["answer_url"] == "https://abc.ngrok.app/hooks/a"
    assert fake.last_json["hangup_url"] == "https://abc.ngrok.app/hooks/h"


# --- input validation ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "to",
    ["", "abc", "12345", "+0123456789", "+880 1711 111234", "+8801711111234<+8801722222222", "sip:a@b.com", None, 8801711111234],
)
def test_invalid_destinations_rejected_without_calling_vobiz(monkeypatch: pytest.MonkeyPatch, to) -> None:
    fake = FakeVobiz()
    response = build(monkeypatch, fake).post("/api/vobiz/calls", json={"to": to}, headers=HEADERS)
    assert response.status_code == 422
    assert fake.requests == []


def test_caller_supplied_answer_url_is_not_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeVobiz()
    body = {"to": DEST, "answer_url": "https://evil.example/answer"}
    response = build(monkeypatch, fake).post("/api/vobiz/calls", json=body, headers=HEADERS)
    assert response.status_code == 422
    assert fake.requests == []


def test_missing_caller_id_returns_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeVobiz()
    client = build(monkeypatch, fake, make_settings(vobiz_caller_id=""))
    response = client.post("/api/vobiz/calls", json={"to": DEST}, headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "vobiz_config_error"
    assert "VOBIZ_CALLER_ID" in response.json()["error"]["message"]
    assert fake.requests == []


def test_invalid_caller_id_returns_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeVobiz()
    client = build(monkeypatch, fake, make_settings(vobiz_caller_id="not-a-number"))
    response = client.post("/api/vobiz/calls", json={"to": DEST}, headers=HEADERS)
    assert response.status_code == 503
    assert fake.requests == []


@pytest.mark.parametrize("base", ["", "http://abc.ngrok.app", "https://localhost:8000", "https://127.0.0.1"])
def test_non_public_base_url_blocks_call(monkeypatch: pytest.MonkeyPatch, base: str) -> None:
    fake = FakeVobiz()
    client = build(monkeypatch, fake, make_settings(public_base_url=base))
    response = client.post("/api/vobiz/calls", json={"to": DEST}, headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "vobiz_config_error"
    assert fake.requests == []


def test_missing_vobiz_credentials_block_call(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeVobiz()
    client = build(monkeypatch, fake, make_settings(vobiz_auth_id="", vobiz_auth_token=SecretStr("")))
    response = client.post("/api/vobiz/calls", json={"to": DEST}, headers=HEADERS)
    assert response.status_code == 503
    assert fake.requests == []


# --- internal authentication -----------------------------------------------------------------


def test_missing_internal_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeVobiz()
    response = build(monkeypatch, fake).post("/api/vobiz/calls", json={"to": DEST})
    assert response.status_code == 401
    assert fake.requests == []


@pytest.mark.parametrize("supplied", ["wrong", "", INTERNAL + "x", INTERNAL.upper()])
def test_wrong_internal_token_rejected(monkeypatch: pytest.MonkeyPatch, supplied: str) -> None:
    fake = FakeVobiz()
    response = build(monkeypatch, fake).post(
        "/api/vobiz/calls", json={"to": DEST}, headers={"X-Internal-Token": supplied}
    )
    assert response.status_code == 401
    assert fake.requests == []


def test_auth_is_checked_before_body_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    response = build(monkeypatch, FakeVobiz()).post("/api/vobiz/calls", json={"to": "bad"})
    assert response.status_code == 401


def test_route_disabled_when_internal_token_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeVobiz()
    client = build(monkeypatch, fake, make_settings(internal_api_token=SecretStr("")))
    for headers in ({}, {"X-Internal-Token": ""}, HEADERS):
        response = client.post("/api/vobiz/calls", json={"to": DEST}, headers=headers)
        assert response.status_code == 503
    assert fake.requests == []


# --- Vobiz error handling --------------------------------------------------------------------


def test_vobiz_authentication_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeVobiz(401, {"error": "invalid credentials"})
    client = build(monkeypatch, fake)
    response = client.post("/api/vobiz/calls", json={"to": DEST}, headers=HEADERS)
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["type"] == "vobiz_auth_error" and error["upstream_status"] == 401
    assert VOBIZ_TOKEN not in response.text
    # Definite rejection: the guard is released so a corrected retry is possible.
    fake._status = 200
    fake._body = {"api_id": "a", "message": "Call fired", "request_uuid": "u2"}
    assert client.post("/api/vobiz/calls", json={"to": DEST}, headers=HEADERS).status_code == 202


def test_vobiz_insufficient_balance(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"status": "error", "error": {"code": "INSUFFICIENT_BALANCE", "message": "Balance too low"}, "requestId": "r1"}
    client = build(monkeypatch, FakeVobiz(402, body))
    response = client.post("/api/vobiz/calls", json={"to": DEST}, headers=HEADERS)
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["type"] == "vobiz_payment_required"
    assert error["upstream_code"] == "INSUFFICIENT_BALANCE" and error["request_id"] == "r1"


def test_vobiz_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeVobiz(429, {"error": {"code": "RATE_LIMIT_EXCEEDED", "message": "CPS"}}, {"Retry-After": "2"})
    response = build(monkeypatch, fake).post("/api/vobiz/calls", json={"to": DEST}, headers=HEADERS)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "2"
    assert response.json()["error"]["type"] == "vobiz_rate_limited"
    assert len(fake.requests) == 1  # no automatic retry


def test_vobiz_rejects_unowned_caller_id_with_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeVobiz(400, {"error": "From number not owned by account"})
    response = build(monkeypatch, fake).post("/api/vobiz/calls", json={"to": DEST}, headers=HEADERS)
    assert response.status_code == 400
    assert "VOBIZ_CALLER_ID" in response.json()["error"]["message"]


def test_timeout_is_not_retried_and_blocks_resubmission(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeVobiz(raises=httpx.ReadTimeout("slow"))
    client = build(monkeypatch, fake)

    first = client.post("/api/vobiz/calls", json={"to": DEST}, headers=HEADERS)
    assert first.status_code == 504
    assert first.json()["error"]["type"] == "vobiz_timeout"
    assert "may or may not have been created" in first.json()["error"]["message"]
    assert len(fake.requests) == 1

    # The outcome is unknown, so an immediate resubmission is refused instead of risking a duplicate.
    second = client.post("/api/vobiz/calls", json={"to": DEST}, headers=HEADERS)
    assert second.status_code == 409
    assert len(fake.requests) == 1


def test_connection_error_and_server_error_keep_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    for fake, expected in (
        (FakeVobiz(raises=httpx.ConnectError("down")), 502),
        (FakeVobiz(500, {"error": "boom"}), 502),
    ):
        client = build(monkeypatch, fake)
        assert client.post("/api/vobiz/calls", json={"to": DEST}, headers=HEADERS).status_code == expected
        assert client.post("/api/vobiz/calls", json={"to": DEST}, headers=HEADERS).status_code == 409
        assert len(fake.requests) == 1


def test_success_without_request_uuid_is_an_error_and_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeVobiz(200, {"api_id": "a", "message": "Call fired"})
    client = build(monkeypatch, fake)
    response = client.post("/api/vobiz/calls", json={"to": DEST}, headers=HEADERS)
    assert response.status_code == 502
    assert client.post("/api/vobiz/calls", json={"to": DEST}, headers=HEADERS).status_code == 409


# --- duplicate protection --------------------------------------------------------------------


def test_duplicate_submission_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeVobiz()
    client = build(monkeypatch, fake)

    assert client.post("/api/vobiz/calls", json={"to": DEST}, headers=HEADERS).status_code == 202
    duplicate = client.post("/api/vobiz/calls", json={"to": "8801711111234"}, headers=HEADERS)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["type"] == "vobiz_duplicate_call"
    assert len(fake.requests) == 1

    other = client.post("/api/vobiz/calls", json={"to": "+8801722222222"}, headers=HEADERS)
    assert other.status_code == 202
    assert len(fake.requests) == 2


# --- logging hygiene -------------------------------------------------------------------------


def test_logs_contain_no_secrets_or_full_numbers(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    client = build(monkeypatch, FakeVobiz())
    # create_app() reconfigures the root logger and drops caplog's handler; re-attach it.
    logging.getLogger().addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG)
    client.post("/api/vobiz/calls", json={"to": DEST}, headers=HEADERS)
    client.post("/api/vobiz/calls", json={"to": DEST}, headers={"X-Internal-Token": "bad-guess"})
    ours = [r for r in caplog.records if r.name.startswith("app.")]
    assert any(r.getMessage() == "vobiz_call_created" for r in ours)
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in ours)
    for secret in (VOBIZ_TOKEN, INTERNAL, "bad-guess", DEST, "8801711111234", AUTH_ID):
        assert secret not in text
