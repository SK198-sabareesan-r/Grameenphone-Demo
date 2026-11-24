"""Vobiz Milestone 1 tests: config, client, diagnostics. All HTTP is mocked."""

from __future__ import annotations

import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.routes import vobiz_config
from app.core.config import Settings
from app.integrations.vobiz.client import VobizClient
from app.integrations.vobiz.errors import (
    VobizAuthError,
    VobizConfigError,
    VobizConnectionError,
    VobizPaymentError,
    VobizRateLimitError,
    VobizRequestError,
    VobizServerError,
    VobizTimeoutError,
)
from app.main import create_app

AUTH_ID = "MA_TESTACCOUNT1234"
TOKEN = "super-secret-token-xyz"


def make_settings(auth_id: str = AUTH_ID, token: str = TOKEN, **kw) -> Settings:
    return Settings(vobiz_auth_id=auth_id, vobiz_auth_token=SecretStr(token), **kw)


def mock_client(handler, settings: Settings | None = None) -> VobizClient:
    settings = settings or make_settings()
    http = httpx.AsyncClient(
        base_url=settings.vobiz_base_url + "/", transport=httpx.MockTransport(handler)
    )
    return VobizClient(settings, http_client=http)


# --- configuration ---------------------------------------------------------------------------


def test_defaults() -> None:
    s = Settings()
    assert s.vobiz_base_url == "https://api.vobiz.ai/api/v1"
    assert s.vobiz_caller_id == ""
    assert s.vobiz_request_timeout_s == 15.0
    assert s.vobiz_validate_signature is True


def test_settings_do_not_leak_token() -> None:
    s = make_settings()
    assert TOKEN not in repr(s)
    assert TOKEN not in s.model_dump_json()


def test_from_env_reads_vobiz_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.core.config.load_dotenv", lambda *_a, **_k: None)
    monkeypatch.setenv("VOBIZ_AUTH_ID", "MA_X")
    monkeypatch.setenv("VOBIZ_AUTH_TOKEN", "tok")
    monkeypatch.setenv("VOBIZ_BASE_URL", "https://example.test/api/v1/")
    monkeypatch.setenv("VOBIZ_CALLER_ID", "14155551234")
    monkeypatch.setenv("VOBIZ_REQUEST_TIMEOUT_S", "7.5")
    monkeypatch.setenv("VOBIZ_VALIDATE_SIGNATURE", "false")
    s = Settings.from_env()
    assert s.vobiz_auth_id == "MA_X"
    assert s.vobiz_auth_token.get_secret_value() == "tok"
    assert s.vobiz_base_url == "https://example.test/api/v1"  # trailing slash stripped
    assert s.vobiz_caller_id == "14155551234"
    assert s.vobiz_request_timeout_s == 7.5
    assert s.vobiz_validate_signature is False


def test_from_env_empty_values_use_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.core.config.load_dotenv", lambda *_a, **_k: None)
    for name in ("VOBIZ_BASE_URL", "VOBIZ_REQUEST_TIMEOUT_S", "VOBIZ_VALIDATE_SIGNATURE"):
        monkeypatch.setenv(name, "")
    s = Settings.from_env()
    assert s.vobiz_base_url == "https://api.vobiz.ai/api/v1"
    assert s.vobiz_request_timeout_s == 15.0
    assert s.vobiz_validate_signature is True


# --- client: authentication and success ------------------------------------------------------


async def test_check_balance_sends_auth_headers_and_correct_url() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["id"] = request.headers.get("X-Auth-ID")
        seen["token"] = request.headers.get("X-Auth-Token")
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={"currency": "INR", "status": "active", "balance": 999.5, "available_balance": 999.5},
        )

    result = await mock_client(handler).check_balance("inr")

    assert seen["id"] == AUTH_ID
    assert seen["token"] == TOKEN
    assert seen["method"] == "GET"
    assert seen["url"] == f"https://api.vobiz.ai/api/v1/Account/{AUTH_ID}/balance/INR"
    assert result == {"currency": "INR", "status": "active"}  # amounts are dropped


async def test_missing_credentials_raise_config_error_without_request() -> None:
    calls: list = []
    client = mock_client(lambda r: calls.append(r) or httpx.Response(200, json={}), make_settings("", ""))
    with pytest.raises(VobizConfigError):
        await client.check_balance()
    assert calls == []


async def test_only_one_credential_is_not_enough() -> None:
    client = mock_client(lambda r: httpx.Response(200, json={}), make_settings(token=""))
    assert client.has_credentials is False
    with pytest.raises(VobizConfigError):
        await client.check_balance()


@pytest.mark.parametrize("bad", ["", "INRR", "I1R", "../x", "INR/../.."])
async def test_invalid_currency_is_rejected_before_request(bad: str) -> None:
    calls: list = []
    client = mock_client(lambda r: calls.append(r) or httpx.Response(200, json={}))
    with pytest.raises(VobizConfigError):
        await client.check_balance(bad)
    assert calls == []


async def test_auth_id_is_url_quoted_in_path() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.raw_path.decode()
        return httpx.Response(200, json={"currency": "INR", "status": "active"})

    await mock_client(handler, make_settings(auth_id="MA/evil id")).check_balance()
    assert "MA%2Fevil%20id" in seen["path"]


async def test_empty_success_body_returns_empty_dict() -> None:
    client = mock_client(lambda r: httpx.Response(204))
    assert await client.request("GET", "x", operation="t") == {}


async def test_non_json_success_body_raises_server_error() -> None:
    client = mock_client(lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(VobizServerError):
        await client.request("GET", "x", operation="t")


# --- client: error mapping -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "exc"),
    [
        (401, {"error": "invalid credentials"}, VobizAuthError),
        (403, {"status": "error", "error": {"code": "ACCOUNT_INACTIVE", "message": "inactive"}}, VobizAuthError),
        (402, {"status": "error", "error": {"code": "INSUFFICIENT_BALANCE", "message": "low"}}, VobizPaymentError),
        (404, {"message": "not found"}, VobizRequestError),
        (422, {"status": "error", "error": {"code": "VALIDATION_ERROR", "message": "bad"}}, VobizRequestError),
        (429, {"status": "error", "error": {"code": "RATE_LIMIT_EXCEEDED", "message": "slow"}}, VobizRateLimitError),
        (500, {"error": "boom"}, VobizServerError),
        (503, {}, VobizServerError),
    ],
)
async def test_http_errors_map_to_exceptions(status: int, body: dict, exc: type) -> None:
    client = mock_client(lambda r: httpx.Response(status, json=body))
    with pytest.raises(exc) as info:
        await client.check_balance()
    assert info.value.upstream_status == status
    assert TOKEN not in info.value.message


async def test_error_details_are_extracted() -> None:
    body = {
        "status": "error",
        "error": {"code": "INSUFFICIENT_BALANCE", "message": "Balance too low"},
        "requestId": "req_123",
    }
    client = mock_client(lambda r: httpx.Response(402, json=body))
    with pytest.raises(VobizPaymentError) as info:
        await client.check_balance()
    assert info.value.upstream_code == "INSUFFICIENT_BALANCE"
    assert info.value.request_id == "req_123"
    assert "Balance too low" in info.value.message


async def test_non_json_error_body_is_handled() -> None:
    client = mock_client(lambda r: httpx.Response(502, text="<html>bad gateway</html>"))
    with pytest.raises(VobizServerError):
        await client.check_balance()


async def test_retry_after_header_is_parsed() -> None:
    client = mock_client(lambda r: httpx.Response(429, headers={"Retry-After": "3"}, json={}))
    with pytest.raises(VobizRateLimitError) as info:
        await client.check_balance()
    assert info.value.retry_after_s == 3.0


async def test_timeout_maps_to_timeout_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(VobizTimeoutError):
        await mock_client(handler).check_balance()


@pytest.mark.parametrize("raised", [httpx.ConnectError("down"), httpx.ReadError("reset")])
async def test_network_failures_map_to_connection_error(raised: Exception) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise raised

    with pytest.raises(VobizConnectionError):
        await mock_client(handler).check_balance()


async def test_no_automatic_retry() -> None:
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, json={})

    with pytest.raises(VobizServerError):
        await mock_client(handler).check_balance()
    assert len(calls) == 1


async def test_logs_never_contain_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    client = mock_client(lambda r: httpx.Response(401, json={"error": "invalid credentials"}))
    with pytest.raises(VobizAuthError):
        await client.check_balance()
    ours = [r for r in caplog.records if r.name.startswith("app.")]
    assert ours
    for record in ours:
        assert TOKEN not in record.getMessage()
        assert TOKEN not in str(record.__dict__)
        assert AUTH_ID not in str(record.__dict__)


def test_logging_config_silences_httpx_request_urls() -> None:
    from app.core.logging import configure_logging

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    httpx_logger = logging.getLogger("httpx")
    saved_httpx = httpx_logger.level
    try:
        configure_logging("INFO")
        assert httpx_logger.level == logging.WARNING
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        httpx_logger.setLevel(saved_httpx)


async def test_injected_client_is_not_closed_but_owned_client_is() -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    await VobizClient(make_settings(), http_client=http).aclose()
    assert http.is_closed is False
    await http.aclose()

    owned = VobizClient(make_settings())
    await owned.aclose()
    assert owned._http.is_closed is True


async def test_default_client_uses_configured_timeout_and_base_url() -> None:
    client = VobizClient(make_settings(vobiz_request_timeout_s=4.0))
    try:
        assert client._http.timeout.read == 4.0
        assert str(client._http.base_url) == "https://api.vobiz.ai/api/v1/"
    finally:
        await client.aclose()


# --- diagnostics endpoint --------------------------------------------------------------------


def test_diagnostics_hides_credentials() -> None:
    client = TestClient(create_app(make_settings()))
    response = client.get("/api/v1/vobiz/diagnostics")
    assert response.status_code == 200
    assert TOKEN not in response.text
    assert AUTH_ID not in response.text  # only a masked hint is shown
    body = response.json()
    assert body["api"]["credentials_configured"] is True
    assert body["api"]["auth_id_hint"] == "MA_...1234"
    assert body["api"]["base_url"] == "https://api.vobiz.ai/api/v1"
    assert body["connectivity"]["checked"] is False


def test_diagnostics_without_credentials_warns() -> None:
    client = TestClient(create_app(Settings()))
    body = client.get("/api/v1/vobiz/diagnostics").json()
    assert body["api"]["credentials_configured"] is False
    assert body["api"]["auth_id_hint"] is None
    warnings = " ".join(body["warnings"])
    assert "VOBIZ_AUTH_ID" in warnings and "VOBIZ_AUTH_TOKEN" in warnings


def test_diagnostics_warns_on_non_default_base_url() -> None:
    client = TestClient(create_app(make_settings(vobiz_base_url="http://localhost:9/api/v1")))
    warnings = " ".join(client.get("/api/v1/vobiz/diagnostics").json()["warnings"])
    assert "differs from the documented value" in warnings
    assert "not https" in warnings


def test_diagnostics_verify_without_credentials_returns_503() -> None:
    client = TestClient(create_app(Settings()))
    response = client.get("/api/v1/vobiz/diagnostics?verify=true")
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "vobiz_config_error"


def test_diagnostics_verify_success_and_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def install(status: int, body: dict) -> None:
        def factory(settings: Settings) -> VobizClient:
            return mock_client(lambda r: httpx.Response(status, json=body), settings)

        monkeypatch.setattr(vobiz_config, "VobizClient", factory)

    client = TestClient(create_app(make_settings()))

    install(200, {"currency": "INR", "status": "active", "balance": 123.45, "available_balance": 100})
    ok = client.get("/api/v1/vobiz/diagnostics?verify=true")
    assert ok.status_code == 200
    assert ok.json()["connectivity"]["ok"] is True
    assert ok.json()["connectivity"]["account_status"] == "active"
    assert "123.45" not in ok.text and TOKEN not in ok.text

    install(401, {"error": "invalid credentials"})
    bad = client.get("/api/v1/vobiz/diagnostics?verify=true")
    assert bad.status_code == 502
    assert bad.json()["error"]["type"] == "vobiz_auth_error"
    assert bad.json()["error"]["upstream_status"] == 401
    assert TOKEN not in bad.text

    install(429, {"error": "slow"})
    limited = client.get("/api/v1/vobiz/diagnostics?verify=true")
    assert limited.status_code == 429


def test_diagnostics_verify_timeout_returns_504(monkeypatch: pytest.MonkeyPatch) -> None:
    def factory(settings: Settings) -> VobizClient:
        def handler(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("t")

        return mock_client(handler, settings)

    monkeypatch.setattr(vobiz_config, "VobizClient", factory)
    response = TestClient(create_app(make_settings())).get("/api/v1/vobiz/diagnostics?verify=true")
    assert response.status_code == 504
    assert response.json()["error"]["type"] == "vobiz_timeout"


def test_diagnostics_verify_bad_currency_returns_503() -> None:
    client = TestClient(create_app(make_settings()))
    response = client.get("/api/v1/vobiz/diagnostics?verify=true&currency=bad1")
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "vobiz_config_error"


def test_existing_routes_still_registered() -> None:
    client = TestClient(create_app(Settings()))
    assert client.get("/health").json() == {"status": "ok"}
    paths = set(client.get("/openapi.json").json()["paths"])
    assert {"/api/v1/vobiz/answer", "/api/v1/vobiz/hangup", "/api/v1/sarvam/diagnostics"} <= paths
