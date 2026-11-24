"""Shared helpers for Vobiz webhook endpoints: payload reading, signature enforcement, dedupe."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request

from app.core.config import Settings
from app.integrations.vobiz.authentication import verify_signature
from app.integrations.vobiz.errors import VobizConfigError

logger = logging.getLogger(__name__)


async def read_payload(request: Request) -> dict[str, Any]:
    """Return the callback parameters. Vobiz sends form-encoded data (GET would use the query)."""
    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        try:
            body = await request.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}
    if request.method == "GET":
        return dict(request.query_params)
    form = await request.form()
    return {key: str(value) for key, value in form.items()}


def enforce_signature(request: Request, settings: Settings, *, endpoint: str) -> None:
    """Reject the request unless its Vobiz signature validates.

    With VOBIZ_VALIDATE_SIGNATURE=false the check is skipped and a warning is logged each time.
    The signed URL is built from PUBLIC_BASE_URL because a tunnel/proxy changes the URL the app sees.
    Logs contain header NAMES only, never signature values or the auth token.
    """
    if not settings.vobiz_validate_signature:
        logger.warning("vobiz_signature_validation_disabled", extra={"endpoint": endpoint})
        return

    token = settings.vobiz_auth_token.get_secret_value()
    if not settings.public_base_url or not token:
        raise VobizConfigError(
            "Webhook signature validation needs PUBLIC_BASE_URL and VOBIZ_AUTH_TOKEN "
            "(or set VOBIZ_VALIDATE_SIGNATURE=false for local debugging only)."
        )

    url = f"{settings.public_base_url}{request.url.path}"
    result = verify_signature(token, url, request.headers)
    if result.ok:
        return

    logger.warning(
        f"vobiz_signature_{result.status}",
        extra={
            "endpoint": endpoint,
            "vobiz_signature_headers": list(result.present_headers),
            "request_header_names": sorted(k.lower() for k in request.headers.keys()),
            "signed_url_path": request.url.path,
        },
    )
    raise HTTPException(status_code=403, detail=f"Vobiz signature {result.status}")


class EventDeduper:
    """Remembers recently seen event keys. Bounded in size and age; per process."""

    def __init__(
        self,
        max_entries: int = 10_000,
        ttl_s: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_entries
        self._ttl = ttl_s
        self._clock = clock
        self._seen: OrderedDict[str, float] = OrderedDict()

    def is_duplicate(self, key: str) -> bool:
        """True if `key` was seen within the TTL. Otherwise records it and returns False."""
        now = self._clock()
        while self._seen:
            oldest_key, seen_at = next(iter(self._seen.items()))
            if now - seen_at <= self._ttl and len(self._seen) <= self._max:
                break
            del self._seen[oldest_key]
        if key in self._seen:
            return True
        self._seen[key] = now
        return False
