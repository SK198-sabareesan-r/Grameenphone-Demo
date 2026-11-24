"""Sarvam error hierarchy. Messages never contain the API key."""

from __future__ import annotations


class SarvamError(Exception):
    """Base class. `http_status` is what our API should return to its own caller."""

    http_status = 502
    code = "sarvam_error"

    def __init__(
        self,
        message: str,
        *,
        upstream_status: int | None = None,
        upstream_code: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.upstream_status = upstream_status
        self.upstream_code = upstream_code
        self.request_id = request_id

    def to_dict(self) -> dict[str, object]:
        return {
            "success": False,
            "error": {
                "type": self.code,
                "message": self.message,
                "upstream_status": self.upstream_status,
                "upstream_code": self.upstream_code,
                "request_id": self.request_id,
            },
        }


class SarvamConfigError(SarvamError):
    """Local misconfiguration (e.g. missing API key). No request was sent."""

    http_status = 503
    code = "sarvam_config_error"


class SarvamAuthError(SarvamError):
    """Sarvam rejected the API key (HTTP 403 invalid_api_key_error)."""

    code = "sarvam_auth_error"


class SarvamQuotaError(SarvamError):
    """Credit balance empty (HTTP 402). Retrying will not help."""

    code = "sarvam_quota_error"


class SarvamRateLimitError(SarvamError):
    """HTTP 429. `retry_after_s` is set when Sarvam sent Retry-After."""

    http_status = 429
    code = "sarvam_rate_limited"

    def __init__(self, message: str, *, retry_after_s: float | None = None, **kw) -> None:
        super().__init__(message, **kw)
        self.retry_after_s = retry_after_s


class SarvamRequestError(SarvamError):
    """Sarvam rejected the request (HTTP 400/413/422): bad model, language, speaker, audio."""

    http_status = 400
    code = "sarvam_request_error"


class SarvamServerError(SarvamError):
    """HTTP 5xx from Sarvam. Safe to retry with backoff."""

    code = "sarvam_server_error"


class SarvamTimeoutError(SarvamError):
    http_status = 504
    code = "sarvam_timeout"


class SarvamConnectionError(SarvamError):
    code = "sarvam_connection_error"


class SarvamInvalidAudioError(SarvamError):
    """Sarvam answered 200 but the audio is missing, empty, or not decodable into playable PCM."""

    code = "sarvam_invalid_audio"


class SarvamStreamClosedError(SarvamError):
    """A realtime WebSocket closed (e.g. inactivity timeout, normal close) without a more specific cause."""

    code = "sarvam_stream_closed"

    def __init__(self, message: str, *, close_code: int | None = None, retryable: bool = False, **kw) -> None:
        super().__init__(message, **kw)
        self.close_code = close_code
        self.retryable = retryable
