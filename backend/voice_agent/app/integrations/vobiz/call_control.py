"""Outbound call creation.

Calls POST Account/{auth_id}/Call/ once. There is no retry and no idempotency key in the Vobiz
API, so duplicates are prevented locally by DuplicateCallGuard (per process; a multi-worker
deployment would need a shared store).
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.core.config import Settings
from app.integrations.vobiz.client import VobizClient
from app.integrations.vobiz.errors import (
    VobizAuthError,
    VobizConfigError,
    VobizConnectionError,
    VobizDuplicateCallError,
    VobizPaymentError,
    VobizRateLimitError,
    VobizRequestError,
    VobizServerError,
    VobizTimeoutError,
)
from app.integrations.vobiz.phone import mask_number, normalize_e164
from app.integrations.vobiz.urls import join_public_url, validate_public_https_url

logger = logging.getLogger(__name__)

DEFAULT_DUPLICATE_WINDOW_S = 30.0

# Failures where Vobiz definitely did not create a call, so the reservation can be released.
_NOT_CREATED = (VobizAuthError, VobizPaymentError, VobizRateLimitError, VobizRequestError)
# Failures where the call may or may not exist. The reservation is kept and nothing is retried.
_OUTCOME_UNKNOWN = (VobizTimeoutError, VobizConnectionError, VobizServerError)


class DuplicateCallGuard:
    """Blocks a second call to the same destination within `window_s` seconds.

    Keys are SHA-256 hashes, so raw phone numbers are not kept in memory longer than needed.
    """

    def __init__(
        self, window_s: float = DEFAULT_DUPLICATE_WINDOW_S, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._window_s = window_s
        self._clock = clock
        self._entries: dict[str, tuple[float, str | None]] = {}

    @staticmethod
    def key_for(destination: str) -> str:
        return hashlib.sha256(destination.encode()).hexdigest()

    def _prune(self) -> None:
        now = self._clock()
        for key in [k for k, (expires, _) in self._entries.items() if expires <= now]:
            del self._entries[key]

    def reserve(self, key: str) -> None:
        self._prune()
        if key in self._entries:
            raise VobizDuplicateCallError(
                f"A call to this destination was already submitted in the last {int(self._window_s)}s. "
                "Not sending another."
            )
        self._entries[key] = (self._clock() + self._window_s, None)

    def confirm(self, key: str, call_uuid: str) -> None:
        self._entries[key] = (self._clock() + self._window_s, call_uuid)

    def release(self, key: str) -> None:
        self._entries.pop(key, None)


@dataclass(frozen=True)
class OutboundCallResult:
    call_uuid: str  # Vobiz request_uuid; equals the CallUUID in webhooks and stream events
    api_id: str | None
    message: str | None


class CallService:
    def __init__(self, client: VobizClient, settings: Settings, guard: DuplicateCallGuard) -> None:
        self._client = client
        self._settings = settings
        self._guard = guard

    def build_payload(self, to: str) -> dict[str, str]:
        """Validate configuration and build the documented request body."""
        s = self._settings
        if not s.vobiz_caller_id:
            raise VobizConfigError("VOBIZ_CALLER_ID is not set. Use a number owned by your Vobiz account.")
        try:
            caller_id = normalize_e164(s.vobiz_caller_id)
            destination = normalize_e164(to)
        except ValueError as exc:
            raise VobizConfigError(f"Invalid phone number: {exc}") from exc

        base = validate_public_https_url(s.public_base_url)
        return {
            "from": caller_id,
            "to": destination,
            "answer_url": join_public_url(base, s.vobiz_answer_webhook_path),
            "answer_method": "POST",
            "hangup_url": join_public_url(base, s.vobiz_hangup_webhook_path),
            "hangup_method": "POST",
        }

    async def create_outbound_call(self, to: str) -> OutboundCallResult:
        payload = self.build_payload(to)
        key = self._guard.key_for(payload["to"])
        self._guard.reserve(key)

        try:
            data = await self._client.request(
                "POST",
                self._client.account_path("Call/"),
                operation="create_call",
                json=payload,
            )
        except _NOT_CREATED as exc:
            self._guard.release(key)
            if isinstance(exc, VobizRequestError) and "not owned" in exc.message.lower():
                exc.message += (
                    " Check VOBIZ_CALLER_ID: it must be a number on this account "
                    "(new numbers take about 2 minutes to register)."
                )
            raise
        except _OUTCOME_UNKNOWN as exc:
            logger.warning(
                "vobiz_call_outcome_unknown",
                extra={"to": mask_number(payload["to"]), "error": type(exc).__name__},
            )
            exc.message += " The call may or may not have been created. It was not retried."
            raise

        call_uuid = data.get("request_uuid")
        if not isinstance(call_uuid, str) or not call_uuid:
            logger.error("vobiz_call_response_missing_uuid", extra={"to": mask_number(payload["to"])})
            raise VobizServerError(
                "Vobiz accepted the request but returned no request_uuid. "
                "The call may exist; it was not retried."
            )

        self._guard.confirm(key, call_uuid)
        logger.info(
            "vobiz_call_created",
            extra={"call_uuid": call_uuid, "to": mask_number(payload["to"]), "api_id": data.get("api_id")},
        )
        return OutboundCallResult(
            call_uuid=call_uuid, api_id=data.get("api_id"), message=data.get("message")
        )
