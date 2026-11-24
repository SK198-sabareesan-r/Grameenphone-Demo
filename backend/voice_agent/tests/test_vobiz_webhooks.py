"""Milestone 2 tests: answer and hangup webhooks."""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.integrations.vobiz.authentication import compute_signature
from app.integrations.vobiz.stream_token import verify_stream_token
from app.main import create_app

TOKEN = "vobiz-secret-token"
BASE = "https://abc.ngrok.app"
ANSWER = "/api/v1/vobiz/answer"
HANGUP = "/api/v1/vobiz/hangup"

ANSWER_FORM = {
    "CallUUID": "5401fd2e-6344-40df-a22c-c8ffea7a92e7",
    "From": "+14155551234",
    "To": "+8801711111234",
    "Direction": "outbound",
    "Event": "StartApp",
    "CallStatus": "in-progress",
    "RequestUUID": "req-1",
    "ALegUUID": "a-leg-1",
}
HANGUP_FORM = {
    "CallUUID": "5401fd2e-6344-40df-a22c-c8ffea7a92e7",
    "From": "+14155551234",
    "To": "+8801711111234",
    "Direction": "outbound",
    "Event": "Hangup",
    "CallStatus": "completed",
    "Duration": "12",
    "BillDuration": "60",
    "HangupCause": "NORMAL_CLEARING",
    "StartTime": "2026-11-23 10:00:00",
    "EndTime": "2026-11-23 10:00:12",
}


def make_settings(**overrides) -> Settings:
    values = dict(
        vobiz_auth_id="MA_TEST",
        vobiz_auth_token=SecretStr(TOKEN),
        public_base_url=BASE,
        vobiz_validate_signature=False,
    )
    values.update(overrides)
    return Settings(**values)


def signed(path: str, scheme: str = "v3", token: str = TOKEN, nonce: str = "98765432109876543210") -> dict:
    sep = "." if scheme.endswith("v3") else ""
    family = "V3" if scheme.endswith("v3") else "V2"
    sig = compute_signature(token, f"{BASE}{path}", nonce, separator=sep)
    return {f"X-Vobiz-Signature-{scheme.upper()}": sig, f"X-Vobiz-Signature-{family}-Nonce": nonce}


@pytest.fixture
def lax() -> TestClient:
    return TestClient(create_app(make_settings()))


@pytest.fixture
def strict() -> TestClient:
    return TestClient(create_app(make_settings(vobiz_validate_signature=True)))


# --- answer webhook --------------------------------------------------------------------------


def test_valid_answer_webhook_returns_vobizxml(lax: TestClient) -> None:
    response = lax.post(ANSWER, data=ANSWER_FORM)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    root = ET.fromstring(response.text)
    assert root.tag == "Response"
    stream = root.find("Stream")
    assert stream.attrib == {
        "bidirectional": "true",
        "audioTrack": "inbound",
        "keepCallAlive": "true",
        "contentType": "audio/x-l16;rate=16000",
    }
    url = urlsplit(stream.text)
    assert url.scheme == "wss" and url.netloc == "abc.ngrok.app"
    assert url.path == f"/ws/vobiz/stream/{ANSWER_FORM['CallUUID']}"
    token = parse_qs(url.query)["token"][0]
    assert verify_stream_token(TOKEN, ANSWER_FORM["CallUUID"], token)
    assert not verify_stream_token(TOKEN, "some-other-call", token)
    assert TOKEN not in response.text


def test_each_answer_gets_a_distinct_token(lax: TestClient) -> None:
    a = lax.post(ANSWER, data=ANSWER_FORM).text
    b = lax.post(ANSWER, data=ANSWER_FORM).text
    assert a != b


def test_answer_uses_configured_audio_format_and_paths() -> None:
    client = TestClient(
        create_app(
            make_settings(
                vobiz_answer_webhook_path="/hooks/answer",
                vobiz_stream_ws_path="/ws/custom/",
                vobiz_inbound_content_type="audio/x-mulaw;rate=8000",
            )
        )
    )
    assert client.post(ANSWER, data=ANSWER_FORM).status_code == 404  # default path is not registered
    response = client.post("/hooks/answer", data=ANSWER_FORM)
    stream = ET.fromstring(response.text).find("Stream")
    assert stream.attrib["contentType"] == "audio/x-mulaw;rate=8000"
    assert urlsplit(stream.text).path == f"/ws/custom/{ANSWER_FORM['CallUUID']}"


def test_answer_accepts_inbound_direction_and_ignores_unknown_fields(lax: TestClient) -> None:
    form = {**ANSWER_FORM, "Direction": "INBOUND", "Surprise": "x"}
    assert lax.post(ANSWER, data=form).status_code == 200


def test_unsafe_call_uuid_is_url_quoted_in_stream_url(lax: TestClient) -> None:
    response = lax.post(ANSWER, data={**ANSWER_FORM, "CallUUID": "a/b?c=d&e"})
    assert response.status_code == 200
    path = urlsplit(ET.fromstring(response.text).find("Stream").text).path
    assert path == "/ws/vobiz/stream/a%2Fb%3Fc%3Dd%26e"


@pytest.mark.parametrize(
    "mutation",
    [
        {"CallUUID": ""},
        {"CallUUID": "has space"},
        {"CallUUID": "x" * 200},
        {"From": ""},
        {"To": ""},
        {"Direction": "sideways"},
        {"Direction": ""},
    ],
)
def test_invalid_answer_payload_rejected(lax: TestClient, mutation: dict) -> None:
    response = lax.post(ANSWER, data={**ANSWER_FORM, **mutation})
    assert response.status_code == 400


@pytest.mark.parametrize("missing", ["CallUUID", "From", "To", "Direction"])
def test_missing_required_answer_fields_rejected(lax: TestClient, missing: str) -> None:
    form = {k: v for k, v in ANSWER_FORM.items() if k != missing}
    assert lax.post(ANSWER, data=form).status_code == 400


def test_empty_answer_body_rejected(lax: TestClient) -> None:
    assert lax.post(ANSWER, data={}).status_code == 400


def test_answer_requires_public_https_base_url() -> None:
    client = TestClient(create_app(make_settings(public_base_url="")))
    response = client.post(ANSWER, data=ANSWER_FORM)
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "vobiz_config_error"


def test_answer_with_unsupported_content_type_setting_is_config_error() -> None:
    client = TestClient(create_app(make_settings(vobiz_inbound_content_type="audio/x-l16;rate=24000")))
    response = client.post(ANSWER, data=ANSWER_FORM)
    assert response.status_code == 503


def test_answer_logs_masked_numbers_only(lax: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    lax.post(ANSWER, data=ANSWER_FORM)
    record = next(r for r in caplog.records if r.getMessage() == "vobiz_answer_received")
    assert record.call_uuid == ANSWER_FORM["CallUUID"]
    assert record.to == "***1234" and record.__dict__["from"] == "***1234"
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records if r.name.startswith("app."))
    assert "8801711111234" not in text and "token=" not in text and TOKEN not in text


# --- signature enforcement -------------------------------------------------------------------


@pytest.mark.parametrize("scheme", ["v3", "v2", "ma-v3", "ma-v2"])
def test_answer_accepts_valid_signature(strict: TestClient, scheme: str) -> None:
    response = strict.post(ANSWER, data=ANSWER_FORM, headers=signed(ANSWER, scheme))
    assert response.status_code == 200


def test_valid_signature_with_query_string(strict: TestClient) -> None:
    response = strict.post(f"{ANSWER}?foo=bar", data=ANSWER_FORM, headers=signed(ANSWER))
    assert response.status_code == 200


def test_answer_rejects_invalid_signature(strict: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    headers = signed(ANSWER, token="wrong-token")
    response = strict.post(ANSWER, data=ANSWER_FORM, headers=headers)
    assert response.status_code == 403
    record = next(r for r in caplog.records if r.getMessage() == "vobiz_signature_invalid")
    assert record.vobiz_signature_headers == ["x-vobiz-signature-v3"]
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records if r.name.startswith("app."))
    assert headers["X-Vobiz-Signature-V3"] not in text and TOKEN not in text  # no secrets in logs


def test_signature_for_a_different_path_is_rejected(strict: TestClient) -> None:
    assert strict.post(ANSWER, data=ANSWER_FORM, headers=signed(HANGUP)).status_code == 403


def test_answer_rejects_missing_signature_and_logs_diagnostics(
    strict: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    response = strict.post(ANSWER, data=ANSWER_FORM, headers={"User-Agent": "Vobiz-Webhook/1.0"})
    assert response.status_code == 403
    assert response.json()["detail"] == "Vobiz signature missing"
    record = next(r for r in caplog.records if r.getMessage() == "vobiz_signature_missing")
    assert record.vobiz_signature_headers == []
    assert "user-agent" in record.request_header_names  # names only, never values
    assert "Vobiz-Webhook/1.0" not in str(record.__dict__)
    assert record.endpoint == "answer"


def test_validation_can_be_disabled_for_local_dev_with_a_warning(
    lax: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    assert lax.post(ANSWER, data=ANSWER_FORM).status_code == 200
    assert any(r.getMessage() == "vobiz_signature_validation_disabled" for r in caplog.records)


def test_signature_validation_needs_public_base_url() -> None:
    client = TestClient(create_app(make_settings(vobiz_validate_signature=True, public_base_url="")))
    assert client.post(ANSWER, data=ANSWER_FORM).status_code == 503


def test_signature_validation_needs_auth_token() -> None:
    settings = make_settings(vobiz_validate_signature=True, vobiz_auth_token=SecretStr(""))
    assert TestClient(create_app(settings)).post(ANSWER, data=ANSWER_FORM).status_code == 503


def test_signature_is_checked_before_the_payload(strict: TestClient) -> None:
    assert strict.post(ANSWER, data={}).status_code == 403


def test_validation_is_on_by_default() -> None:
    assert Settings().vobiz_validate_signature is True


# --- hangup webhook --------------------------------------------------------------------------


def test_hangup_webhook_is_recorded(lax: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    response = lax.post(HANGUP, data=HANGUP_FORM)
    assert response.status_code == 200
    assert response.json() == {"status": "received", "duplicate": False}

    record = next(r for r in caplog.records if r.getMessage() == "vobiz_hangup_received")
    assert record.call_uuid == HANGUP_FORM["CallUUID"]
    assert record.call_status == "completed"
    assert record.hangup_cause == "NORMAL_CLEARING"
    assert record.duration_s == "12" and record.bill_duration_s == "60"
    assert record.leg == "a_leg"  # B-leg events are not reported here
    assert record.to == "***1234"
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records if r.name.startswith("app."))
    assert "8801711111234" not in text and "14155551234" not in text


def test_duplicate_hangup_is_handled_safely(lax: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    first = lax.post(HANGUP, data=HANGUP_FORM)
    second = lax.post(HANGUP, data=HANGUP_FORM)
    third = lax.post(HANGUP, data=HANGUP_FORM)
    assert first.json()["duplicate"] is False
    assert second.status_code == third.status_code == 200  # still 200, so Vobiz stops retrying
    assert second.json()["duplicate"] is True and third.json()["duplicate"] is True
    messages = [r.getMessage() for r in caplog.records]
    assert messages.count("vobiz_hangup_received") == 1
    assert messages.count("vobiz_hangup_duplicate") == 2


def test_hangups_for_different_calls_are_independent(lax: TestClient) -> None:
    lax.post(HANGUP, data=HANGUP_FORM)
    other = lax.post(HANGUP, data={**HANGUP_FORM, "CallUUID": "another-call"})
    assert other.json()["duplicate"] is False


def test_hangup_accepts_status_alias_and_minimal_payload(lax: TestClient) -> None:
    minimal = {"CallUUID": "c1", "Direction": "outbound", "Status": "completed"}
    assert lax.post(HANGUP, data=minimal).status_code == 200


@pytest.mark.parametrize("mutation", [{"CallUUID": ""}, {"Direction": "weird"}, {"CallUUID": "a b"}])
def test_invalid_hangup_payload_rejected(lax: TestClient, mutation: dict) -> None:
    assert lax.post(HANGUP, data={**HANGUP_FORM, **mutation}).status_code == 400


def test_hangup_requires_valid_signature_when_enabled(strict: TestClient) -> None:
    assert strict.post(HANGUP, data=HANGUP_FORM).status_code == 403
    assert strict.post(HANGUP, data=HANGUP_FORM, headers=signed(HANGUP, token="nope")).status_code == 403
    assert strict.post(HANGUP, data=HANGUP_FORM, headers=signed(HANGUP)).status_code == 200


def test_hangup_accepts_json_body_too(lax: TestClient) -> None:
    response = lax.post(HANGUP, json={"CallUUID": "json-call", "Direction": "outbound", "Event": "Hangup"})
    assert response.status_code == 200


def test_hangup_does_not_return_xml(lax: TestClient) -> None:
    assert lax.post(HANGUP, data=HANGUP_FORM).headers["content-type"].startswith("application/json")


# --- legacy functionality is untouched -------------------------------------------------------


def test_existing_routes_still_registered(lax: TestClient) -> None:
    paths = set(lax.get("/openapi.json").json()["paths"])
    assert {
        "/health",
        "/api/v1/sarvam/diagnostics",
        "/api/v1/vobiz/diagnostics",
        "/api/v1/vobiz/calls",
        ANSWER,
        HANGUP,
    } <= paths


def test_diagnostics_reports_new_settings_without_secrets() -> None:
    settings = make_settings(internal_api_token=SecretStr("internal-secret"), vobiz_caller_id="+14155551234")
    body = TestClient(create_app(settings)).get("/api/v1/vobiz/diagnostics")
    data = body.json()
    assert data["calls"]["internal_api_token_configured"] is True
    assert data["calls"]["answer_url"] == f"{BASE}{ANSWER}"
    assert data["calls"]["hangup_url"] == f"{BASE}{HANGUP}"
    assert data["calls"]["stream_content_type"] == "audio/x-l16;rate=16000"
    assert "internal-secret" not in body.text and TOKEN not in body.text


def test_diagnostics_warns_when_internal_token_missing() -> None:
    data = TestClient(create_app(make_settings())).get("/api/v1/vobiz/diagnostics").json()
    assert any("INTERNAL_API_TOKEN" in w for w in data["warnings"])
