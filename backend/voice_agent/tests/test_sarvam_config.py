"""Milestone 1 tests: config, client error mapping, diagnostics. No real API key needed."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.routes import sarvam_config
from app.core.config import Settings
from app.main import create_app
from app.services.sarvam.client import SarvamClient
from app.services.sarvam.errors import (
    SarvamAuthError,
    SarvamConfigError,
    SarvamConnectionError,
    SarvamQuotaError,
    SarvamRateLimitError,
    SarvamRequestError,
    SarvamServerError,
    SarvamTimeoutError,
)

KEY = "test-key-123"


def make_settings(key: str = KEY, **kw) -> Settings:
    return Settings(sarvam_api_key=SecretStr(key), **kw)


def mock_client(handler, settings: Settings | None = None) -> SarvamClient:
    settings = settings or make_settings()
    http = httpx.AsyncClient(
        base_url=settings.sarvam_base_url, transport=httpx.MockTransport(handler)
    )
    return SarvamClient(settings, http_client=http)


async def test_missing_key_raises_config_error_without_request() -> None:
    calls = []
    client = mock_client(lambda r: calls.append(r) or httpx.Response(200, json={}), make_settings(""))
    with pytest.raises(SarvamConfigError):
        await client.verify_auth()
    assert calls == []


async def test_verify_auth_sends_header_and_returns_request_id() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("api-subscription-key")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"request_id": "r1", "language_code": "en-IN"})

    result = await mock_client(handler).verify_auth()
    assert result == {"request_id": "r1"}
    assert seen["key"] == KEY
    assert seen["url"].endswith("/text-lid")


@pytest.mark.parametrize(
    ("status", "body", "exc"),
    [
        (403, {"error": {"message": "bad key", "code": "invalid_api_key_error", "request_id": "r"}}, SarvamAuthError),
        (402, {"error": {"message": "No credits available", "code": "insufficient_quota_error"}}, SarvamQuotaError),
        (429, {"error": {"message": "slow", "code": "rate_limit_exceeded_error"}}, SarvamRateLimitError),
        (400, {"detail": "model deprecated"}, SarvamRequestError),
        (503, {"error": {"message": "overloaded", "code": "model_overloaded"}}, SarvamServerError),
    ],
)
async def test_http_errors_map_to_exceptions(status: int, body: dict, exc: type) -> None:
    client = mock_client(lambda r: httpx.Response(status, json=body))
    with pytest.raises(exc) as info:
        await client.verify_auth()
    assert info.value.upstream_status == status
    assert KEY not in info.value.message


async def test_non_json_error_body_is_handled() -> None:
    client = mock_client(lambda r: httpx.Response(502, text="<html>bad gateway</html>"))
    with pytest.raises(SarvamServerError):
        await client.verify_auth()


async def test_timeout_and_connection_errors() -> None:
    def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    def refused(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(SarvamTimeoutError):
        await mock_client(timeout).verify_auth()
    with pytest.raises(SarvamConnectionError):
        await mock_client(refused).verify_auth()


async def test_retry_after_is_parsed() -> None:
    client = mock_client(lambda r: httpx.Response(429, headers={"Retry-After": "7"}, json={}))
    with pytest.raises(SarvamRateLimitError) as info:
        await client.verify_auth()
    assert info.value.retry_after_s == 7.0


def test_diagnostics_hides_key_and_reports_defaults() -> None:
    client = TestClient(create_app(make_settings()))
    response = client.get("/api/v1/sarvam/diagnostics")
    assert response.status_code == 200
    body = response.json()
    assert KEY not in response.text
    assert body["api_key_configured"] is True
    assert body["stt"]["model"] == "saaras:v4"
    assert body["tts"]["model"] == "bulbul:v4-flash"
    assert body["tts"]["sample_rate"] == 8000
    assert body["auth"]["checked"] is False


def test_diagnostics_warns_on_unknown_model_without_replacing_it() -> None:
    client = TestClient(create_app(make_settings(sarvam_tts_model="bulbul:v9")))
    body = client.get("/api/v1/sarvam/diagnostics").json()
    assert body["tts"]["model"] == "bulbul:v9"
    assert any("bulbul:v9" in w for w in body["warnings"])


def test_diagnostics_verify_without_key_returns_config_error() -> None:
    client = TestClient(create_app(make_settings("")))
    response = client.get("/api/v1/sarvam/diagnostics?verify=true")
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "sarvam_config_error"


def test_diagnostics_verify_success_and_auth_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def install(status: int, body: dict) -> None:
        def factory(settings: Settings) -> SarvamClient:
            return mock_client(lambda r: httpx.Response(status, json=body), settings)

        monkeypatch.setattr(sarvam_config, "SarvamClient", factory)

    client = TestClient(create_app(make_settings()))

    install(200, {"request_id": "abc"})
    ok = client.get("/api/v1/sarvam/diagnostics?verify=true").json()
    assert ok["auth"] == {
        "checked": True,
        "ok": True,
        "request_id": "abc",
        "note": "API key accepted. STT/TTS model access is not verified by this check.",
    }

    install(403, {"error": {"message": "nope", "code": "invalid_api_key_error"}})
    bad = client.get("/api/v1/sarvam/diagnostics?verify=true")
    assert bad.status_code == 502
    assert bad.json()["error"]["type"] == "sarvam_auth_error"
    assert KEY not in bad.text


def test_legacy_env_names_still_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.core.config.load_dotenv", lambda *_a, **_k: None)
    for name in ("SARVAM_STT_MODEL", "SARVAM_STT_LANGUAGE_CODE", "SARVAM_DEFAULT_LANGUAGE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SARVAM_MODEL", "saaras:v3")
    monkeypatch.setenv("SARVAM_LANGUAGE", "bn-IN")
    s = Settings.from_env()
    assert (s.sarvam_model, s.sarvam_language) == ("saaras:v3", "bn-IN")

    monkeypatch.setenv("SARVAM_STT_MODEL", "saaras:v4")
    assert Settings.from_env().sarvam_model == "saaras:v4"
