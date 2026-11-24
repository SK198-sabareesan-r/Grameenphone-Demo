"""Milestone 2 unit tests: XML builder, signatures, stream tokens, URL/phone validation, config."""

from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from app.core.config import Settings
from app.integrations.vobiz.authentication import compute_signature, verify_signature
from app.integrations.vobiz.call_control import DuplicateCallGuard
from app.integrations.vobiz.errors import VobizConfigError, VobizDuplicateCallError
from app.integrations.vobiz.phone import mask_number, normalize_e164
from app.integrations.vobiz.stream_token import issue_stream_token, verify_stream_token
from app.integrations.vobiz.urls import https_to_wss, join_public_url, validate_public_https_url
from app.integrations.vobiz.webhooks import EventDeduper
from app.integrations.vobiz.xml_builder import build_bidirectional_stream_xml

TOKEN = "auth-token-for-tests"
URL = "https://abc.ngrok.app/api/v1/vobiz/answer"


# --- VobizXML --------------------------------------------------------------------------------


def test_stream_xml_exact_output() -> None:
    xml = build_bidirectional_stream_xml("wss://abc.ngrok.app/ws/vobiz/stream/c1?token=t.n.s")
    assert xml == (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>"
        '<Stream bidirectional="true" audioTrack="inbound" keepCallAlive="true" '
        'contentType="audio/x-l16;rate=16000">'
        "wss://abc.ngrok.app/ws/vobiz/stream/c1?token=t.n.s"
        "</Stream></Response>"
    )


def test_stream_xml_is_valid_vobizxml() -> None:
    root = ET.fromstring(build_bidirectional_stream_xml("wss://h.example/ws"))
    assert root.tag == "Response"
    stream = root.find("Stream")
    assert stream is not None
    assert stream.attrib == {
        "bidirectional": "true",
        "audioTrack": "inbound",
        "keepCallAlive": "true",
        "contentType": "audio/x-l16;rate=16000",
    }
    assert stream.text == "wss://h.example/ws"
    assert root.find("Connect") is None and root.find("Start") is None  # no wrapper elements around <Stream>


@pytest.mark.parametrize(
    "content_type", ["audio/x-l16;rate=8000", "audio/x-l16;rate=16000", "audio/x-mulaw;rate=8000"]
)
def test_stream_xml_supports_documented_inbound_formats(content_type: str) -> None:
    root = ET.fromstring(build_bidirectional_stream_xml("wss://h.example/ws", content_type=content_type))
    assert root.find("Stream").attrib["contentType"] == content_type


@pytest.mark.parametrize(
    "content_type", ["audio/x-l16;rate=24000", "audio/x-mulaw;rate=16000", "audio/wav", '"/><Hangup/>']
)
def test_stream_xml_rejects_unsupported_content_type(content_type: str) -> None:
    with pytest.raises(ValueError):
        build_bidirectional_stream_xml("wss://h.example/ws", content_type=content_type)


def test_stream_xml_escapes_special_characters() -> None:
    url = "wss://h.example/ws?a=1&b=2&c=<x>"
    xml = build_bidirectional_stream_xml(url)
    assert "a=1&amp;b=2&amp;c=&lt;x&gt;" in xml
    assert ET.fromstring(xml).find("Stream").text == url  # round-trips unchanged


def test_stream_xml_cannot_be_injected_through_url() -> None:
    url = "wss://h.example/ws</Stream><Hangup/>"
    xml = build_bidirectional_stream_xml(url)
    root = ET.fromstring(xml)
    assert root.find("Hangup") is None
    assert root.find("Stream").text == url


def test_stream_xml_keep_call_alive_can_be_disabled() -> None:
    root = ET.fromstring(build_bidirectional_stream_xml("wss://h.example/ws", keep_call_alive=False))
    assert root.find("Stream").attrib["keepCallAlive"] == "false"


@pytest.mark.parametrize(
    "url",
    ["", "https://h.example/ws", "ws://h.example/ws", "wss://", "wss:///path", "wss://h.example/a b", "wss://h.example/\nx"],
)
def test_stream_xml_rejects_bad_websocket_urls(url: str) -> None:
    with pytest.raises(ValueError):
        build_bidirectional_stream_xml(url)


def test_stream_xml_allows_insecure_ws_only_when_asked() -> None:
    assert "ws://localhost:8000/ws" in build_bidirectional_stream_xml(
        "ws://localhost:8000/ws", allow_insecure=True
    )


# --- signature validation --------------------------------------------------------------------


def _headers(scheme: str, nonce: str = "12345678901234567890", url: str = URL, token: str = TOKEN) -> dict:
    sep = "." if scheme.endswith("v3") else ""
    sig = compute_signature(token, url, nonce, separator=sep)
    family = "v3" if scheme.endswith("v3") else "v2"
    return {f"X-Vobiz-Signature-{scheme.upper()}": sig, f"X-Vobiz-Signature-{family.upper()}-Nonce": nonce}


@pytest.mark.parametrize("scheme", ["v3", "v2", "ma-v3", "ma-v2"])
def test_valid_signatures_accepted(scheme: str) -> None:
    result = verify_signature(TOKEN, URL, _headers(scheme))
    assert result.status == "valid" and result.ok


def test_signature_known_vectors() -> None:
    # Independent computation of base64(HMAC-SHA256(key, msg)) for the documented message layouts.
    import base64
    import hashlib
    import hmac

    nonce = "11112222333344445555"
    v2 = base64.b64encode(hmac.new(TOKEN.encode(), (URL + nonce).encode(), hashlib.sha256).digest()).decode()
    v3 = base64.b64encode(hmac.new(TOKEN.encode(), (URL + "." + nonce).encode(), hashlib.sha256).digest()).decode()
    assert compute_signature(TOKEN, URL, nonce, separator="") == v2
    assert compute_signature(TOKEN, URL, nonce, separator=".") == v3
    assert v2 != v3


def test_query_string_is_ignored_when_signing() -> None:
    headers = _headers("v3")
    assert verify_signature(TOKEN, URL + "?CallUUID=abc&x=1", headers).ok


def test_wrong_token_is_invalid() -> None:
    result = verify_signature("other-token", URL, _headers("v3"))
    assert result.status == "invalid"
    assert result.present_headers == ("x-vobiz-signature-v3",)


def test_wrong_url_is_invalid() -> None:
    assert verify_signature(TOKEN, "https://evil.example/api/v1/vobiz/answer", _headers("v3")).status == "invalid"


def test_tampered_signature_is_invalid() -> None:
    headers = _headers("v3")
    headers["X-Vobiz-Signature-V3"] = "A" + headers["X-Vobiz-Signature-V3"][1:]
    assert verify_signature(TOKEN, URL, headers).status == "invalid"


def test_signature_without_nonce_is_invalid() -> None:
    headers = _headers("v3")
    del headers["X-Vobiz-Signature-V3-Nonce"]
    assert verify_signature(TOKEN, URL, headers).status == "invalid"


def test_v3_signature_does_not_validate_as_v2() -> None:
    headers = _headers("v3")
    swapped = {
        "X-Vobiz-Signature-V2": headers["X-Vobiz-Signature-V3"],
        "X-Vobiz-Signature-V2-Nonce": headers["X-Vobiz-Signature-V3-Nonce"],
    }
    assert verify_signature(TOKEN, URL, swapped).status == "invalid"


def test_missing_signature_is_reported_as_missing_not_invalid() -> None:
    result = verify_signature(TOKEN, URL, {"Host": "abc.ngrok.app", "User-Agent": "Vobiz-Webhook/1.0"})
    assert result.status == "missing" and not result.ok
    assert result.present_headers == ()


def test_legacy_v1_signature_alone_is_not_accepted() -> None:
    result = verify_signature(TOKEN, URL, {"X-Vobiz-Signature": "whatever"})
    assert result.status == "missing"


def test_header_lookup_is_case_insensitive() -> None:
    headers = {k.lower(): v for k, v in _headers("v3").items()}
    assert verify_signature(TOKEN, URL, headers).ok


def test_empty_auth_token_never_validates() -> None:
    assert verify_signature("", URL, _headers("v3", token="")).status == "invalid"


def test_non_ascii_signature_header_does_not_crash() -> None:
    headers = _headers("v3")
    headers["X-Vobiz-Signature-V3"] = "é" * 10
    assert verify_signature(TOKEN, URL, headers).status == "invalid"


# --- stream tokens ---------------------------------------------------------------------------


def test_stream_token_roundtrip_and_binding() -> None:
    token = issue_stream_token(TOKEN, "call-1", now=1000)
    assert verify_stream_token(TOKEN, "call-1", token, now=1001)
    assert not verify_stream_token(TOKEN, "call-2", token, now=1001)  # bound to the call
    assert not verify_stream_token("other-token", "call-1", token, now=1001)  # bound to the key


def test_stream_token_expires() -> None:
    token = issue_stream_token(TOKEN, "call-1", ttl_s=60, now=1000)
    assert verify_stream_token(TOKEN, "call-1", token, now=1059)
    assert not verify_stream_token(TOKEN, "call-1", token, now=1061)


def test_stream_tokens_are_unique_per_issue_and_url_safe() -> None:
    a, b = issue_stream_token(TOKEN, "c"), issue_stream_token(TOKEN, "c")
    assert a != b
    assert all(ch.isalnum() or ch in "-_." for ch in a)


@pytest.mark.parametrize("bad", ["", "x", "1.2", "a.b.c", "9999999999.nonce.", "..", "1.2.3.4"])
def test_malformed_stream_tokens_rejected(bad: str) -> None:
    assert not verify_stream_token(TOKEN, "call-1", bad, now=1000)


def test_tampered_stream_token_rejected() -> None:
    expiry, nonce, sig = issue_stream_token(TOKEN, "c", now=1000).split(".")
    forged = f"{int(expiry) + 99999}.{nonce}.{sig}"
    assert not verify_stream_token(TOKEN, "c", forged, now=1000)


def test_stream_token_requires_auth_token() -> None:
    with pytest.raises(VobizConfigError):
        issue_stream_token("", "c")


# --- URL / phone validation ------------------------------------------------------------------


def test_public_https_url_accepted_and_trimmed() -> None:
    assert validate_public_https_url("https://abc.ngrok.app/") == "https://abc.ngrok.app"
    assert validate_public_https_url("https://8.8.8.8") == "https://8.8.8.8"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://abc.ngrok.app",
        "https://localhost",
        "https://localhost:8000",
        "https://127.0.0.1",
        "https://10.0.0.5",
        "https://192.168.1.10",
        "https://169.254.1.1",
        "https://[::1]",
        "https://myserver",
        "https://printer.local",
        "https://user:pw@abc.ngrok.app",
        "https://abc ngrok.app",
        "ftp://abc.ngrok.app",
    ],
)
def test_non_public_urls_rejected(url: str) -> None:
    with pytest.raises(VobizConfigError):
        validate_public_https_url(url)


def test_join_and_wss_helpers() -> None:
    assert join_public_url("https://h.example/", "/api/v1/vobiz/answer") == "https://h.example/api/v1/vobiz/answer"
    assert https_to_wss("https://h.example/ws") == "wss://h.example/ws"
    for bad in ("no-slash", "/a b", "/a?x=1", "/a#f"):
        with pytest.raises(VobizConfigError):
            join_public_url("https://h.example", bad)
    with pytest.raises(VobizConfigError):
        https_to_wss("http://h.example")


@pytest.mark.parametrize("raw", ["+8801711111111", "8801711111111", " +14155551234 "])
def test_e164_normalized(raw: str) -> None:
    assert normalize_e164(raw).startswith("+") and normalize_e164(raw).lstrip("+").isdigit()


@pytest.mark.parametrize(
    "raw", ["", "abc", "+0123456789", "12345", "+1234567890123456", "+880 1711 111111", "+880-171-1111111",
            "8801711111111<8801722222222", "sip:agent@x.com", "+88017111111a"]
)
def test_invalid_numbers_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_e164(raw)


def test_mask_number() -> None:
    assert mask_number("+8801711111234") == "***1234"
    assert mask_number("123") == "***"
    assert mask_number(None) == ""


# --- guards ----------------------------------------------------------------------------------


def test_duplicate_guard_window_and_release() -> None:
    now = [0.0]
    guard = DuplicateCallGuard(window_s=30, clock=lambda: now[0])
    key = guard.key_for("+8801711111111")
    guard.reserve(key)
    with pytest.raises(VobizDuplicateCallError):
        guard.reserve(key)
    guard.reserve(guard.key_for("+8801722222222"))  # other destination is fine
    now[0] = 31
    guard.reserve(key)  # window elapsed
    guard.release(key)
    guard.reserve(key)  # released


def test_duplicate_guard_does_not_store_raw_numbers() -> None:
    guard = DuplicateCallGuard()
    key = guard.key_for("+8801711111111")
    guard.reserve(key)
    assert "8801711111111" not in " ".join(guard._entries)


def test_event_deduper_ttl_and_bound() -> None:
    now = [0.0]
    dedupe = EventDeduper(max_entries=2, ttl_s=10, clock=lambda: now[0])
    assert dedupe.is_duplicate("a") is False
    assert dedupe.is_duplicate("a") is True
    dedupe.is_duplicate("b")
    dedupe.is_duplicate("c")  # exceeds max -> oldest ("a") evicted
    assert len(dedupe._seen) <= 3
    now[0] = 100
    assert dedupe.is_duplicate("c") is False  # expired


# --- configuration ---------------------------------------------------------------------------


def test_milestone2_defaults() -> None:
    s = Settings()
    assert s.vobiz_answer_webhook_path == "/api/v1/vobiz/answer"
    assert s.vobiz_hangup_webhook_path == "/api/v1/vobiz/hangup"
    assert s.vobiz_stream_ws_path == "/ws/vobiz/stream"
    assert s.vobiz_inbound_content_type == "audio/x-l16;rate=16000"
    assert s.internal_api_token.get_secret_value() == ""
    leaky = Settings(internal_api_token="internal-secret-value")  # type: ignore[arg-type]
    assert "internal-secret-value" not in repr(leaky)
    assert "internal-secret-value" not in leaky.model_dump_json()


def test_milestone2_env_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.core.config.load_dotenv", lambda *_a, **_k: None)
    monkeypatch.setenv("VOBIZ_ANSWER_WEBHOOK_PATH", "/hooks/answer")
    monkeypatch.setenv("VOBIZ_HANGUP_WEBHOOK_PATH", "/hooks/hangup")
    monkeypatch.setenv("VOBIZ_STREAM_WS_PATH", "/ws/custom")
    monkeypatch.setenv("VOBIZ_INBOUND_CONTENT_TYPE", "audio/x-mulaw;rate=8000")
    monkeypatch.setenv("INTERNAL_API_TOKEN", "internal")
    s = Settings.from_env()
    assert s.vobiz_answer_webhook_path == "/hooks/answer"
    assert s.vobiz_hangup_webhook_path == "/hooks/hangup"
    assert s.vobiz_stream_ws_path == "/ws/custom"
    assert s.vobiz_inbound_content_type == "audio/x-mulaw;rate=8000"
    assert s.internal_api_token.get_secret_value() == "internal"
