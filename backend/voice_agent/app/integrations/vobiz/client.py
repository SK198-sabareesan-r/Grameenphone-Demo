"""Async HTTP client for the Vobiz REST API.

Auth is the X-Auth-ID / X-Auth-Token header pair. Credentials are never logged and never put in
exception messages. There are no automatic retries: later call-creation requests must not be
repeated blindly, so retry policy belongs to the caller.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import Settings
from app.integrations.vobiz.errors import (
    VobizAuthError,
    VobizConfigError,
    VobizConnectionError,
    VobizError,
    VobizPaymentError,
    VobizRateLimitError,
    VobizRequestError,
    VobizServerError,
    VobizTimeoutError,
)

logger = logging.getLogger(__name__)

AUTH_ID_HEADER = "X-Auth-ID"
AUTH_TOKEN_HEADER = "X-Auth-Token"
_CURRENCY_RE = re.compile(r"^[A-Za-z]{3}$")


def _parse_error(response: httpx.Response) -> tuple[str, str | None, str | None]:
    """Return (message, code, request_id).

    Handles the documented shape {"status":"error","error":{code,message},"requestId"} and the
    simpler shapes seen in endpoint docs: {"error":"..."} and {"message":"..."}.
    """
    try:
        body = response.json()
    except ValueError:
        return (response.text[:200] or response.reason_phrase, None, None)
    if not isinstance(body, dict):
        return (response.reason_phrase, None, None)

    request_id = body.get("requestId") or body.get("request_id") or body.get("api_id")
    err = body.get("error")
    if isinstance(err, dict):
        return (
            str(err.get("message") or response.reason_phrase)[:500],
            err.get("code"),
            request_id,
        )
    if isinstance(err, str):
        return (err[:500], None, request_id)
    if body.get("message"):
        return (str(body["message"])[:500], None, request_id)
    return (response.reason_phrase, None, request_id)


class VobizClient:
    """Thin wrapper over httpx.AsyncClient. Use as an async context manager or call aclose().

    Pass `http_client` to inject a mocked transport in tests; an injected client is not closed here.
    """

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = http_client is None
        # Trailing slash makes httpx append relative paths to the /api/v1 prefix.
        self._http = http_client or httpx.AsyncClient(
            base_url=settings.vobiz_base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(settings.vobiz_request_timeout_s),
        )

    async def __aenter__(self) -> "VobizClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    @property
    def has_credentials(self) -> bool:
        return bool(
            self._settings.vobiz_auth_id and self._settings.vobiz_auth_token.get_secret_value()
        )

    def _headers(self) -> dict[str, str]:
        if not self.has_credentials:
            raise VobizConfigError(
                "VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN must both be set. Add them to .env and restart."
            )
        return {
            AUTH_ID_HEADER: self._settings.vobiz_auth_id,
            AUTH_TOKEN_HEADER: self._settings.vobiz_auth_token.get_secret_value(),
            "Accept": "application/json",
        }

    def account_path(self, suffix: str = "") -> str:
        """Relative path under the configured account, e.g. account_path('balance/INR')."""
        auth_id = quote(self._settings.vobiz_auth_id, safe="")
        return f"Account/{auth_id}/{suffix.lstrip('/')}"

    async def request(
        self, method: str, path: str, *, operation: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Send a request and return the parsed JSON body, or raise a VobizError.

        `operation` is a short label used in logs instead of the URL (which contains the account ID).
        """
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        try:
            response = await self._http.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            logger.warning("vobiz_timeout", extra={"operation": operation})
            raise VobizTimeoutError("Vobiz API request timed out.") from exc
        except httpx.HTTPError as exc:
            logger.warning(
                "vobiz_connection_error",
                extra={"operation": operation, "error": type(exc).__name__},
            )
            raise VobizConnectionError("Could not reach the Vobiz API.") from exc

        if response.is_success:
            if not response.content:
                return {}
            try:
                data = response.json()
            except ValueError as exc:
                raise VobizServerError(
                    "Vobiz returned a non-JSON success response.",
                    upstream_status=response.status_code,
                ) from exc
            return data if isinstance(data, dict) else {"data": data}

        raise self._to_error(response, operation)

    @staticmethod
    def _to_error(response: httpx.Response, operation: str) -> VobizError:
        message, code, request_id = _parse_error(response)
        status = response.status_code
        info = {"upstream_status": status, "upstream_code": code, "request_id": request_id}
        logger.warning(
            "vobiz_api_error",
            extra={"operation": operation, "status": status, "code": code, "request_id": request_id},
        )
        if status in (401, 403):
            return VobizAuthError(f"Vobiz rejected the credentials: {message}", **info)
        if status == 402:
            return VobizPaymentError(f"Vobiz account balance too low: {message}", **info)
        if status == 429:
            raw = response.headers.get("Retry-After")
            try:
                retry_after_s = float(raw) if raw else None
            except ValueError:
                retry_after_s = None
            return VobizRateLimitError(
                f"Vobiz rate limit exceeded: {message}", retry_after_s=retry_after_s, **info
            )
        if status >= 500:
            return VobizServerError(f"Vobiz server error: {message}", **info)
        return VobizRequestError(f"Vobiz rejected the request: {message}", **info)

    async def check_balance(self, currency: str = "INR") -> dict[str, Any]:
        """Read-only credential and connectivity check: GET Account/{auth_id}/balance/{currency}.

        Places no call and changes nothing. Returns only non-sensitive fields; amounts are dropped.
        """
        if not _CURRENCY_RE.match(currency):
            raise VobizConfigError("currency must be a 3-letter code such as INR or USD.")
        data = await self.request(
            "GET",
            self.account_path(f"balance/{currency.upper()}"),
            operation="check_balance",
        )
        return {"currency": data.get("currency", currency.upper()), "status": data.get("status")}
