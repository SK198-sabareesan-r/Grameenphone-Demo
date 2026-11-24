"""Phone-number validation helpers."""

from __future__ import annotations

import re

# E.164: up to 15 digits, no leading zero, optional leading "+". No separators, no "<" (which
# Vobiz treats as a bulk-dial separator), no SIP URIs.
_E164_RE = re.compile(r"^\+?[1-9]\d{6,14}$")


def normalize_e164(value: str) -> str:
    """Return the number as '+<digits>' or raise ValueError."""
    candidate = (value or "").strip()
    if not _E164_RE.match(candidate):
        raise ValueError("must be an E.164 phone number such as +8801XXXXXXXXX")
    return candidate if candidate.startswith("+") else f"+{candidate}"


def mask_number(value: str | None) -> str:
    """Keep only the last 4 digits, for logs and API responses."""
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    return f"***{digits[-4:]}" if len(digits) > 4 else "***"
