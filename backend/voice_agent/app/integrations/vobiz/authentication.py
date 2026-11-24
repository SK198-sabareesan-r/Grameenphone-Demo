"""Vobiz webhook signature validation (HMAC-SHA256, per docs.vobiz.ai "Validating callbacks").

Documented scheme:
  V2:  base64(HMAC-SHA256(key=auth_token, msg=baseURL + nonce))        headers X-Vobiz-Signature-V2 / -V2-Nonce
  V3:  base64(HMAC-SHA256(key=auth_token, msg=baseURL + "." + nonce))  headers X-Vobiz-Signature-V3 / -V3-Nonce
  MA-V2 / MA-V3: same, signed with the parent (main) account token  headers X-Vobiz-Signature-MA-V2 / -MA-V3
baseURL is the callback URL without its query string. The request body is NOT signed.

Assumptions not stated in the docs: the MA variants reuse the V2/V3 nonce headers. V1 (HMAC-SHA1,
legacy) is deliberately not accepted.

Unresolved in the docs: signature headers are said to be emitted only when "the callback URL has
auth credentials configured on it", with no explanation of how to configure that. A missing
signature is therefore reported as "missing", separately from "invalid".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

# (signature header, nonce header, separator between baseURL and nonce)
_SCHEMES: tuple[tuple[str, str, str], ...] = (
    ("x-vobiz-signature-v3", "x-vobiz-signature-v3-nonce", "."),
    ("x-vobiz-signature-v2", "x-vobiz-signature-v2-nonce", ""),
    ("x-vobiz-signature-ma-v3", "x-vobiz-signature-v3-nonce", "."),
    ("x-vobiz-signature-ma-v2", "x-vobiz-signature-v2-nonce", ""),
)


@dataclass(frozen=True)
class SignatureResult:
    status: Literal["valid", "invalid", "missing"]
    scheme: str | None = None  # header name that validated
    present_headers: tuple[str, ...] = field(default_factory=tuple)  # Vobiz signature header names seen

    @property
    def ok(self) -> bool:
        return self.status == "valid"


def base_url(url: str) -> str:
    """Strip query string and fragment."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def compute_signature(auth_token: str, url: str, nonce: str, *, separator: str) -> str:
    message = f"{base_url(url)}{separator}{nonce}".encode()
    digest = hmac.new(auth_token.encode(), message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def verify_signature(auth_token: str, url: str, headers: Mapping[str, str]) -> SignatureResult:
    """Check the signature headers against the callback URL. Never raises."""
    lowered = {k.lower(): v for k, v in headers.items()}
    present = tuple(name for name, _, _ in _SCHEMES if name in lowered)
    if not present:
        return SignatureResult("missing")
    if not auth_token:
        return SignatureResult("invalid", present_headers=present)

    for sig_header, nonce_header, separator in _SCHEMES:
        received = lowered.get(sig_header)
        nonce = lowered.get(nonce_header)
        if not received or not nonce:
            continue
        expected = compute_signature(auth_token, url, nonce, separator=separator)
        if hmac.compare_digest(expected.encode(), received.encode("utf-8", "replace")):
            return SignatureResult("valid", scheme=sig_header, present_headers=present)
    return SignatureResult("invalid", present_headers=present)
