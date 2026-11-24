"""VobizXML builder. Pure functions, no I/O.

Syntax follows the Vobiz <Stream> reference: the WebSocket URL is the element's text content, and
attributes are bidirectional, audioTrack, keepCallAlive and contentType.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
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


def build_bidirectional_stream_xml(
    ws_url: str,
    *,
    content_type: str = "audio/x-l16;rate=16000",
    keep_call_alive: bool = True,
    allow_insecure: bool = False,
    extra_headers: Mapping[str, str] | None = None,
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

    response = ET.Element("Response")
    stream = ET.SubElement(response, "Stream", attributes)
    stream.text = ws_url
    return f"{XML_DECLARATION}\n{ET.tostring(response, encoding='unicode')}"
