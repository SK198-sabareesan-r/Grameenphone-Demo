"""Vobiz error hierarchy. Messages never contain credentials."""

from __future__ import annotations


class VobizError(Exception):
    """Base class. `http_status` is what our own API returns to its caller."""

    http_status = 502
    code = "vobiz_error"

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


class VobizConfigError(VobizError):
    """Local misconfiguration (missing credentials, bad input). No request was sent."""

    http_status = 503
    code = "vobiz_config_error"


class VobizDuplicateCallError(VobizError):
    """A call to the same destination was submitted recently. Nothing was sent to Vobiz."""

    http_status = 409
    code = "vobiz_duplicate_call"


class VobizAuthError(VobizError):
    """Vobiz rejected the credentials (HTTP 401 or 403)."""

    code = "vobiz_auth_error"


class VobizPaymentError(VobizError):
    """HTTP 402: account balance too low. Retrying will not help."""

    code = "vobiz_payment_required"


class VobizRateLimitError(VobizError):
    """HTTP 429. `retry_after_s` is set when Vobiz sent Retry-After."""

    http_status = 429
    code = "vobiz_rate_limited"

    def __init__(self, message: str, *, retry_after_s: float | None = None, **kw) -> None:
        super().__init__(message, **kw)
        self.retry_after_s = retry_after_s


class VobizRequestError(VobizError):
    """Vobiz rejected the request (400, 404, 409, 422)."""

    http_status = 400
    code = "vobiz_request_error"


class VobizServerError(VobizError):
    """HTTP 5xx from Vobiz."""

    code = "vobiz_server_error"


class VobizTimeoutError(VobizError):
    http_status = 504
    code = "vobiz_timeout"


class VobizConnectionError(VobizError):
    code = "vobiz_connection_error"
