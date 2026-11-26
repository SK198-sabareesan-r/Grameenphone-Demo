"""Vobiz call control and webhooks.

Webhook paths come from settings (VOBIZ_ANSWER_WEBHOOK_PATH / VOBIZ_HANGUP_WEBHOOK_PATH), so the
router is built per app instance. The WebSocket server for the stream URL is NOT part of this
milestone; the answer webhook only returns the XML that points Vobiz at it.
"""

from __future__ import annotations

import hmac
import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from app.core.config import Settings
from app.integrations.vobiz.call_control import CallService
from app.integrations.vobiz.client import VobizClient
from app.integrations.vobiz.errors import VobizConfigError
from app.integrations.vobiz.phone import mask_number
from app.integrations.vobiz.stream_token import issue_stream_token, token_to_header_safe
from app.integrations.vobiz.urls import https_to_wss, join_public_url, validate_public_https_url
from app.integrations.vobiz.webhooks import enforce_signature, read_payload
from app.integrations.vobiz.xml_builder import build_bidirectional_stream_xml
from app.schemas.vobiz import (
    OutboundCallRequest,
    OutboundCallResponse,
    VobizAnswerWebhook,
    VobizHangupWebhook,
)

logger = logging.getLogger(__name__)

INTERNAL_TOKEN_HEADER = "X-Internal-Token"


async def require_internal_token(request: Request) -> None:
    """Protect our own control routes. Fails closed when INTERNAL_API_TOKEN is not configured."""
    expected = request.app.state.settings.internal_api_token.get_secret_value()
    if not expected:
        raise HTTPException(status_code=503, detail="INTERNAL_API_TOKEN is not configured")
    supplied = request.headers.get(INTERNAL_TOKEN_HEADER, "")
    if not hmac.compare_digest(expected.encode(), supplied.encode("utf-8", "replace")):
        logger.warning("internal_auth_failed", extra={"path": request.url.path})
        raise HTTPException(status_code=401, detail="Missing or invalid internal token")


def _check_path(name: str, path: str) -> str:
    if not path.startswith("/") or any(ch.isspace() or ch in "?#" for ch in path):
        raise ValueError(f"{name} must start with '/' and contain no spaces, '?' or '#': {path!r}")
    return path


def create_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="", tags=["vobiz"])
    answer_path = _check_path("VOBIZ_ANSWER_WEBHOOK_PATH", settings.vobiz_answer_webhook_path)
    hangup_path = _check_path("VOBIZ_HANGUP_WEBHOOK_PATH", settings.vobiz_hangup_webhook_path)

    @router.post(
        "/api/vobiz/calls",
        status_code=202,
        response_model=OutboundCallResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def create_call(body: OutboundCallRequest, request: Request) -> OutboundCallResponse:
        """Place one outbound call. The answer URL is built server-side from PUBLIC_BASE_URL."""
        app_settings: Settings = request.app.state.settings
        async with VobizClient(app_settings) as client:
            service = CallService(client, app_settings, request.app.state.call_guard)
            result = await service.create_outbound_call(body.to)
        dev_capture = getattr(request.app.state, "dev_capture", None)
        if dev_capture is not None:  # development only: mark calls to the configured test phone
            dev_capture.register_if_test_call(result.call_uuid, body.to)
        return OutboundCallResponse(
            call_uuid=result.call_uuid,
            api_id=result.api_id,
            message=result.message,
            to=mask_number(body.to),
        )

    @router.post(answer_path)
    async def answer(request: Request) -> Response:
        """Answer webhook (Event=StartApp). Returns VobizXML that starts a bidirectional stream."""
        app_settings: Settings = request.app.state.settings
        enforce_signature(request, app_settings, endpoint="answer")

        payload = await read_payload(request)
        try:
            call = VobizAnswerWebhook.model_validate(payload)
        except ValidationError as exc:
            logger.warning(
                "vobiz_answer_invalid_payload",
                extra={"invalid_fields": sorted({str(e["loc"][-1]) for e in exc.errors()})},
            )
            raise HTTPException(status_code=400, detail="Invalid Vobiz answer payload") from exc

        dev_capture = getattr(request.app.state, "dev_capture", None)
        if dev_capture is not None and call.direction == "outbound":  # development only
            dev_capture.register_if_test_call(call.call_uuid, call.to_number)

        base = validate_public_https_url(app_settings.public_base_url)
        token = issue_stream_token(app_settings.vobiz_auth_token.get_secret_value(), call.call_uuid,
                                   ttl_s=app_settings.vobiz_stream_token_ttl_s)
        ws_path = f"{app_settings.vobiz_stream_ws_path.rstrip('/')}/{quote(call.call_uuid, safe='')}"
        ws_base = https_to_wss(join_public_url(base, ws_path))
        if app_settings.vobiz_ws_auth_mode == "start_event":
            # Token travels in <Stream extraHeaders> instead of the URL query string.
            ws_url, extra_headers = ws_base, {"token": token_to_header_safe(token)}
        else:
            ws_url, extra_headers = f"{ws_base}?token={token}", None
        try:
            xml = build_bidirectional_stream_xml(
                ws_url,
                content_type=app_settings.vobiz_inbound_content_type,
                extra_headers=extra_headers,
                max_retries=app_settings.vobiz_stream_max_retries,
            )
        except ValueError as exc:
            raise VobizConfigError(f"Cannot build stream XML: {exc}") from exc

        logger.info(
            "vobiz_answer_received",
            extra={
                "call_uuid": call.call_uuid,
                "direction": call.direction,
                "event": call.event,
                "call_status": call.call_status,
                "from": mask_number(call.from_number),
                "to": mask_number(call.to_number),
                "stream_content_type": app_settings.vobiz_inbound_content_type,
            },
        )
        copilot = getattr(request.app.state, "copilot", None)
        if copilot is not None:  # M3: tell the Copilot a call started. Non-blocking; never affects the call.
            # The customer is the caller on inbound calls and the destination on outbound ones.
            customer_number = call.from_number if (call.direction or "").lower() == "inbound" else call.to_number
            copilot.emit_call_event(
                call.call_uuid, "call_started", {"from_number": customer_number, "direction": call.direction}
            )
        return Response(content=xml, media_type="application/xml")

    @router.post(hangup_path)
    async def hangup(request: Request) -> JSONResponse:
        """Hangup callback. Reports the A-leg only; Dial B-leg events come from <Dial callbackUrl>."""
        app_settings: Settings = request.app.state.settings
        enforce_signature(request, app_settings, endpoint="hangup")

        payload = await read_payload(request)
        try:
            event = VobizHangupWebhook.model_validate(payload)
        except ValidationError as exc:
            logger.warning(
                "vobiz_hangup_invalid_payload",
                extra={"invalid_fields": sorted({str(e["loc"][-1]) for e in exc.errors()})},
            )
            raise HTTPException(status_code=400, detail="Invalid Vobiz hangup payload") from exc

        # Vobiz retries non-200 callbacks up to 3 times, so duplicates are expected and harmless.
        key = f"{event.call_uuid}:{event.event or 'Hangup'}"
        duplicate = request.app.state.hangup_deduper.is_duplicate(key)
        logger.info(
            "vobiz_hangup_duplicate" if duplicate else "vobiz_hangup_received",
            extra={
                "call_uuid": event.call_uuid,
                "direction": event.direction,
                "event": event.event,
                "call_status": event.call_status,
                "hangup_cause": event.hangup_cause,
                "hangup_cause_code": event.hangup_cause_code,
                "hangup_source": event.hangup_source,
                "duration_s": event.duration,
                "bill_duration_s": event.bill_duration,
                "from": mask_number(event.from_number),
                "to": mask_number(event.to_number),
                "leg": "a_leg",
            },
        )
        # The hangup webhook is the authoritative end of the CALL: close any stream that is still open.
        # Idempotent, so a retried callback or a socket that already closed is harmless.
        try:
            await request.app.state.call_manager.on_hangup(event.call_uuid)
        except Exception as exc:  # never fail the webhook (Vobiz would retry) because of cleanup
            logger.warning(
                "vobiz_hangup_cleanup_failed",
                extra={"call_uuid": event.call_uuid, "error": type(exc).__name__},
            )
        return JSONResponse({"status": "received", "duplicate": duplicate})

    return router
