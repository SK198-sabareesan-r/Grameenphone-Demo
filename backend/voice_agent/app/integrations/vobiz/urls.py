"""URL helpers for the webhook and WebSocket URLs we hand to Vobiz."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from app.integrations.vobiz.errors import VobizConfigError

_BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home.arpa")


def validate_public_https_url(url: str, *, name: str = "PUBLIC_BASE_URL") -> str:
    """Return the URL without a trailing slash, or raise VobizConfigError.

    This is a syntax check only: https, a hostname, and not localhost / private / loopback /
    link-local addresses. It cannot prove the host is reachable from the Vobiz network.
    """
    if not url:
        raise VobizConfigError(f"{name} is not set. Set it to your public https URL (e.g. ngrok).")
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise VobizConfigError(f"{name} contains whitespace or control characters.")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise VobizConfigError(f"{name} must use https; Vobiz cannot reach plain http or local URLs.")
    host = (parts.hostname or "").lower()
    if not host or parts.username or parts.password:
        raise VobizConfigError(f"{name} must be a plain https://host URL without credentials.")
    single_label = "." not in host and ":" not in host  # e.g. "myserver"
    if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES) or single_label:
        raise VobizConfigError(f"{name} must be a public hostname, not '{host}'.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not ip.is_global:
            raise VobizConfigError(f"{name} must be a public address, not '{host}'.")
    return url.rstrip("/")


def join_public_url(base_url: str, path: str) -> str:
    if not path.startswith("/") or any(ch.isspace() or ch in "?#" for ch in path):
        raise VobizConfigError(f"Webhook path '{path}' must start with '/' and contain no spaces, '?' or '#'.")
    return f"{base_url.rstrip('/')}{path}"


def https_to_wss(url: str) -> str:
    if not url.startswith("https://"):
        raise VobizConfigError("A wss:// URL can only be derived from an https:// URL.")
    return "wss://" + url[len("https://") :]
