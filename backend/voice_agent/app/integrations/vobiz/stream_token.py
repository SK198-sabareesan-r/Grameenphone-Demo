"""Per-call WebSocket tokens.

The answer webhook puts a token in the stream URL; the WebSocket server (Milestone 3) verifies it
on connect. Tokens are stateless: `<expiry>.<nonce>.<HMAC-SHA256>` keyed with a key derived from
VOBIZ_AUTH_TOKEN (domain-separated), bound to the call UUID. Rotating the auth token invalidates
outstanding tokens.

Vobiz documents no authentication for its own WebSocket connection, so this URL token is our
design, not a Vobiz feature. It relies on Vobiz connecting to exactly the URL we return.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from app.integrations.vobiz.errors import VobizConfigError

DEFAULT_TTL_S = 900
_DOMAIN = b"vobiz-stream-token-v1"


def _key(auth_token: str) -> bytes:
    if not auth_token:
        raise VobizConfigError("VOBIZ_AUTH_TOKEN is required to sign WebSocket stream tokens.")
    return hmac.new(auth_token.encode(), _DOMAIN, hashlib.sha256).digest()


def _sign(auth_token: str, call_uuid: str, expiry: int, nonce: str) -> str:
    message = f"{call_uuid}.{expiry}.{nonce}".encode()
    digest = hmac.new(_key(auth_token), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def issue_stream_token(
    auth_token: str, call_uuid: str, *, ttl_s: int = DEFAULT_TTL_S, now: float | None = None
) -> str:
    expiry = int((time.time() if now is None else now) + ttl_s)
    nonce = secrets.token_urlsafe(9)
    return f"{expiry}.{nonce}.{_sign(auth_token, call_uuid, expiry, nonce)}"


def check_stream_token(
    auth_token: str, call_uuid: str, token: str | None, *, now: float | None = None
) -> str:
    """Return "valid", "expired", "invalid" or "missing". The signature is checked before expiry,
    so an attacker cannot learn anything from a forged token's expiry field."""
    if not token:
        return "missing"
    try:
        expiry_s, nonce, signature = token.split(".")
        expiry = int(expiry_s)
    except (ValueError, AttributeError):
        return "invalid"
    expected = _sign(auth_token, call_uuid, expiry, nonce)
    if not hmac.compare_digest(expected.encode(), signature.encode("utf-8", "replace")):
        return "invalid"
    return "expired" if expiry < (time.time() if now is None else now) else "valid"


def verify_stream_token(
    auth_token: str, call_uuid: str, token: str, *, now: float | None = None
) -> bool:
    return check_stream_token(auth_token, call_uuid, token, now=now) == "valid"


# <Stream extraHeaders> values may only contain [A-Za-z0-9], so for "start_event" auth mode the
# token is hex-encoded on the way out and decoded on the way in.
def token_to_header_safe(token: str) -> str:
    return token.encode().hex()


def token_from_header_safe(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return bytes.fromhex(value).decode()
    except (ValueError, UnicodeDecodeError):
        return None
