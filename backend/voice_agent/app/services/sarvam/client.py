"""Reusable async HTTP client for the Sarvam REST API.

Auth is the `api-subscription-key` header. Failures map to the SarvamError hierarchy.
The key is never logged, and never placed in exception messages.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import Settings
from app.services.sarvam.constants import AUTH_HEADER, AUTH_PROBE_PATH
from app.services.sarvam.errors import (
    SarvamAuthError,
    SarvamConfigError,
    SarvamConnectionError,
    SarvamError,
    SarvamQuotaError,
    SarvamRateLimitError,
    SarvamRequestError,
    SarvamServerError,
    SarvamTimeoutError,
)

logger = logging.getLogger(__name__)


def _parse_error(response: httpx.Response) -> tuple[str, str | None, str | None]:
    """Return (message, code, request_id) from either documented error shape."""
    try:
        body = response.json()
    except ValueError:
        return (response.text[:200] or response.reason_phrase, None, None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return (
                str(err.get("message") or response.reason_phrase),
                err.get("code"),
                err.get("request_id"),
            )
        if "detail" in body:  # deprecated/unsupported model responses use {"detail": ...}
            return (str(body["detail"])[:500], None, None)
    return (response.reason_phrase, None, None)


class SarvamClient:
    """Thin wrapper over httpx.AsyncClient. Use as an async context manager or call aclose()."""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            base_url=settings.sarvam_base_url,
            timeout=httpx.Timeout(settings.sarvam_request_timeout_s),
        )

    async def __aenter__(self) -> "SarvamClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    @property
    def has_api_key(self) -> bool:
        return bool(self._settings.sarvam_api_key.get_secret_value())

    def _headers(self) -> dict[str, str]:
        key = self._settings.sarvam_api_key.get_secret_value()
        if not key:
            raise SarvamConfigError("SARVAM_API_KEY is not set. Add it to .env and restart.")
        return {AUTH_HEADER: key}

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Send a request and return the parsed JSON body, or raise a SarvamError."""
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        try:
            response = await self._http.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            logger.warning("sarvam_timeout", extra={"path": path})
            raise SarvamTimeoutError("Sarvam API request timed out.") from exc
        except httpx.HTTPError as exc:
            logger.warning("sarvam_connection_error", extra={"path": path, "error": type(exc).__name__})
            raise SarvamConnectionError("Could not reach the Sarvam API.") from exc

        if response.is_success:
            try:
                return response.json()
            except ValueError as exc:
                raise SarvamServerError(
                    "Sarvam returned a non-JSON success response.",
                    upstream_status=response.status_code,
                ) from exc

        raise self._to_error(response, path)

    @staticmethod
    def _to_error(response: httpx.Response, path: str) -> SarvamError:
        message, code, request_id = _parse_error(response)
        status = response.status_code
        info = {"upstream_status": status, "upstream_code": code, "request_id": request_id}
        logger.warning(
            "sarvam_api_error",
            extra={"path": path, "status": status, "code": code, "request_id": request_id},
        )
        if status == 403:
            return SarvamAuthError(f"Sarvam rejected the API key: {message}", **info)
        if status == 402:
            return SarvamQuotaError(f"Sarvam credits exhausted: {message}", **info)
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                retry_after_s = float(retry_after) if retry_after else None
            except ValueError:
                retry_after_s = None
            return SarvamRateLimitError(
                f"Sarvam rate limit or capacity: {message}", retry_after_s=retry_after_s, **info
            )
        if status >= 500:
            return SarvamServerError(f"Sarvam server error: {message}", **info)
        return SarvamRequestError(f"Sarvam rejected the request: {message}", **info)

    async def verify_auth(self) -> dict[str, Any]:
        """Validate the API key with a minimal text-only call (/text-lid on a single word).

        Does not exercise STT/TTS, so it does not prove those models are enabled for the account.
        """
        data = await self.request("POST", AUTH_PROBE_PATH, json={"input": "hello"})
        return {"request_id": data.get("request_id")}
