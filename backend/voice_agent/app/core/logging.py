"""Structured (JSON) logging setup."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any

# Attributes present on every LogRecord; anything else came from `extra=`.
_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON, including `extra` fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


_TOKEN_RE = re.compile(r"(token=)[^&\s\"']+", re.IGNORECASE)


class RedactTokenFilter(logging.Filter):
    """Mask `token=<value>` in log messages.

    Uvicorn logs the WebSocket request line including its query string, which carries the Vobiz
    per-call stream token.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _TOKEN_RE.sub(r"\1[redacted]", message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger to emit JSON to stdout. Safe to call repeatedly."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactTokenFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Uvicorn's own loggers use their own handlers, so redact at the logger as well.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        if not any(isinstance(f, RedactTokenFilter) for f in uvicorn_logger.filters):
            uvicorn_logger.addFilter(RedactTokenFilter())
    # httpx logs every request URL at INFO; Vobiz URLs contain the account Auth ID.
    logging.getLogger("httpx").setLevel(logging.WARNING)
