"""VobizXML builder. Pure functions, no I/O.

Syntax follows the Vobiz <Stream> reference: the WebSocket URL is the element's text content, and
attributes are bidirectional, audioTrack, keepCallAlive and contentType.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from xml.etree import ElementTree as ET

# Inbound formats Vobiz can send to our WebSocket (the <Stream contentType> attribute).
INBOUND_CONTENT_TYPES = frozenset(
    {"audio/x-l16;rate=8000", "audio/x-l16;rate=16000", "audio/x-mulaw;rate=8000"}
)
XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>'


def _validate_ws_url(ws_url: str, *, allow_insecure: bool) -> None:
    allowed = ("wss://", "ws://") if allow_insecure else ("wss://",)
    if not ws_url.startswith(allowed):
        raise ValueError(f"WebSocket URL must start with {' or '.join(allowed)}")
    if len(ws_url) > 2048 or any(ch.isspace() or ord(ch) < 32 for ch in ws_url):
        raise ValueError("WebSocket URL is too long or contains whitespace/control characters")
    if len(ws_url.split("://", 1)[1].split("/", 1)[0]) == 0:
        raise ValueError("WebSocket URL has no host")


_HEADER_TOKEN = re.compile(r"^[A-Za-z0-9]+$")
MAX_EXTRA_HEADERS_LEN = 512


def _format_extra_headers(extra_headers: Mapping[str, str]) -> str:
    """`extraHeaders` is "k1=v1,k2=v2"; Vobiz allows only [A-Za-z0-9] and max 512 bytes."""
    for key, value in extra_headers.items():
        if not _HEADER_TOKEN.match(key) or not _HEADER_TOKEN.match(value):
            raise ValueError("extraHeaders keys and values may only contain letters and digits")
    text = ",".join(f"{k}={v}" for k, v in extra_headers.items())
    if len(text.encode()) > MAX_EXTRA_HEADERS_LEN:
        raise ValueError(f"extraHeaders is longer than {MAX_EXTRA_HEADERS_LEN} bytes")
    return text


def _add_max_retries(attributes: dict[str, str], max_retries: int) -> None:
    """<Stream maxRetries>: reconnect attempts when the WebSocket drops mid-stream (Vobiz default 0, max 10)."""
    retries = int(max_retries)
    if not 0 <= retries <= 10:
        raise ValueError("maxRetries must be 0-10")
    if retries:
        attributes["maxRetries"] = str(retries)


def build_bidirectional_stream_xml(
    ws_url: str,
    *,
    content_type: str = "audio/x-l16;rate=16000",
    keep_call_alive: bool = True,
    allow_insecure: bool = False,
    extra_headers: Mapping[str, str] | None = None,
    max_retries: int = 0,
) -> str:
    """Return VobizXML that starts a bidirectional audio stream to `ws_url`.

    bidirectional="true" requires audioTrack="inbound"; keepCallAlive requires bidirectional and
    keeps the call open until the stream disconnects (without it the call hangs up immediately
    with "End Of XML Instructions"). Text and attribute values are XML-escaped by ElementTree.
    """
    if content_type not in INBOUND_CONTENT_TYPES:
        raise ValueError(f"contentType must be one of {sorted(INBOUND_CONTENT_TYPES)}")
    _validate_ws_url(ws_url, allow_insecure=allow_insecure)

    attributes = {
        "bidirectional": "true",
        "audioTrack": "inbound",
        "keepCallAlive": "true" if keep_call_alive else "false",
        "contentType": content_type,
    }
    if extra_headers:
        attributes["extraHeaders"] = _format_extra_headers(extra_headers)
    _add_max_retries(attributes, max_retries)

    response = ET.Element("Response")
    stream = ET.SubElement(response, "Stream", attributes)
    stream.text = ws_url
    return f"{XML_DECLARATION}\n{ET.tostring(response, encoding='unicode')}"


_DIGIT_PATTERN = re.compile(r"^[0-9*#]{1,16}$")


def build_handoff_xml(
    *,
    number: str | Sequence[str],
    monitor_ws_url: str | None,
    callback_url: str,
    action_url: str,
    content_type: str = "audio/x-l16;rate=16000",
    timeout_s: int = 30,
    caller_id: str | None = None,
    digits_match_bleg: tuple[str, ...] = (),
    stream_extra_headers: Mapping[str, str] | None = None,
    allow_insecure: bool = False,
    stream_max_retries: int = 0,
) -> str:
    """VobizXML for a transferred call: fork BOTH audio tracks to a one-way monitor stream, then dial a human.

    `<Stream bidirectional="false" audioTrack="both" keepCallAlive="false">` does not hold the call, so
    `<Dial>` runs right away while the stream keeps forking. The AI never plays audio on this stream
    (it is one-way). `callbackUrl` receives the B-leg events (answered, connected, digits, hangup) and
    `action` receives the final result; with redirect="true" the XML returned by `action` decides what
    happens next (used to return the caller to the AI when the human does not answer).

    `digits_match_bleg` are digit strings the human agent may press on their own phone; Vobiz reports a
    match as DialDigitsMatch with DialDigitsPressedBy=bleg.
    """
    if content_type not in INBOUND_CONTENT_TYPES:
        raise ValueError(f"contentType must be one of {sorted(INBOUND_CONTENT_TYPES)}")
    if monitor_ws_url is not None:
        _validate_ws_url(monitor_ws_url, allow_insecure=allow_insecure)
    for patt in digits_match_bleg:
        if not _DIGIT_PATTERN.match(patt):
            raise ValueError("digit patterns may only contain 0-9, * and #")
    stream_attrs = {
        "bidirectional": "false",
        "audioTrack": "both",
        "keepCallAlive": "false",
        "contentType": content_type,
    }
    if stream_extra_headers:
        stream_attrs["extraHeaders"] = _format_extra_headers(stream_extra_headers)
    _add_max_retries(stream_attrs, stream_max_retries)
    dial_attrs = {
        "action": action_url,
        "method": "POST",
        "redirect": "true",
        "callbackUrl": callback_url,
        "callbackMethod": "POST",
        "timeout": str(int(timeout_s)),
    }
    if caller_id:
        dial_attrs["callerId"] = caller_id
    if digits_match_bleg:
        dial_attrs["digitsMatchBLeg"] = ",".join(digits_match_bleg)

    response = ET.Element("Response")
    if monitor_ws_url is not None:  # None = AI_BACKGROUND_ASSIST_ENABLED=false: no audio is forked after the transfer
        ET.SubElement(response, "Stream", stream_attrs).text = monitor_ws_url
    dial = ET.SubElement(response, "Dial", dial_attrs)
    for each in ([number] if isinstance(number, str) else list(number)):  # several numbers ring together; the first to answer is bridged
        ET.SubElement(dial, "Number").text = each
    return f"{XML_DECLARATION}\n{ET.tostring(response, encoding='unicode')}"


def build_hangup_xml() -> str:
    return f"{XML_DECLARATION}\n<Response><Hangup /></Response>"
