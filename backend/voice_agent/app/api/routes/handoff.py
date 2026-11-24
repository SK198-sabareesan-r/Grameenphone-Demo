"""Human hand-off routes (M3).

* Control (X-Internal-Token): start a hand-off, read its state, put the customer on hold / resume.
* Vobiz-facing (signed webhooks, same signature check as the answer webhook): the transfer XML, the Dial
  `callbackUrl` events and the Dial `action` result.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.routes.vobiz import require_internal_token
from app.integrations.vobiz.webhooks import enforce_signature, read_payload
from app.services.voice.handoff import HandoffError, HandoffService

logger = logging.getLogger(__name__)


class HandoffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)
    pending_actions: list[str] = Field(default_factory=list, max_length=20)


def _service(request: Request) -> HandoffService:
    return request.app.state.handoff


def _raise(exc: HandoffError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail=str(exc))


def create_router() -> APIRouter:
    router = APIRouter(tags=["handoff"])
    internal = [Depends(require_internal_token)]

    @router.post("/api/vobiz/calls/{call_uuid}/handoff", status_code=202, dependencies=internal)
    async def start_handoff(call_uuid: str, body: HandoffRequest, request: Request) -> dict[str, object]:
        try:
            state = await _service(request).request(
                call_uuid, reason=body.reason, requested_by="api", pending_actions=body.pending_actions
            )
        except HandoffError as exc:
            raise _raise(exc) from exc
        return state.view()

    @router.get("/api/vobiz/calls/{call_uuid}/handoff", dependencies=internal)
    async def handoff_state(call_uuid: str, request: Request) -> dict[str, object]:
        state = _service(request).state(call_uuid)
        if state is None:
            raise HTTPException(status_code=404, detail="No hand-off for this call")
        return state.view()

    @router.post("/api/vobiz/calls/{call_uuid}/hold", dependencies=internal)
    async def hold(call_uuid: str, request: Request) -> dict[str, object]:
        if not await _service(request).hold(call_uuid, source="api"):
            raise HTTPException(status_code=409, detail="The call is not connected to a human agent, or is already on hold")
        return {"call_uuid": call_uuid, "held": True}

    @router.post("/api/vobiz/calls/{call_uuid}/resume", dependencies=internal)
    async def resume(call_uuid: str, request: Request) -> dict[str, object]:
        if not await _service(request).resume(call_uuid, source="api"):
            raise HTTPException(status_code=409, detail="The call is not on hold")
        return {"call_uuid": call_uuid, "held": False}

    # ---- called by Vobiz ----

    @router.post("/api/vobiz/handoff/{call_uuid}/dial")
    async def dial_document(call_uuid: str, request: Request) -> Response:
        enforce_signature(request, request.app.state.settings, endpoint="handoff_dial")
        try:
            xml = _service(request).dial_xml(call_uuid)
        except HandoffError as exc:
            raise _raise(exc) from exc
        return Response(content=xml, media_type="application/xml")

    @router.post("/api/vobiz/handoff/{call_uuid}/dial-events")
    async def dial_events(call_uuid: str, request: Request) -> JSONResponse:
        enforce_signature(request, request.app.state.settings, endpoint="handoff_dial_events")
        payload = await read_payload(request)
        try:
            outcome = await _service(request).on_dial_event(call_uuid, payload)
        except Exception as exc:  # Vobiz retries non-200 callbacks: never fail one because of our own error
            logger.warning("handoff_dial_event_failed", extra={"call_id": call_uuid, "error": type(exc).__name__})
            outcome = "error"
        return JSONResponse({"status": "received", "outcome": outcome})

    @router.post("/api/vobiz/handoff/{call_uuid}/dial-result")
    async def dial_result(call_uuid: str, request: Request) -> Response:
        enforce_signature(request, request.app.state.settings, endpoint="handoff_dial_result")
        payload = await read_payload(request)
        xml = await _service(request).on_dial_result(call_uuid, payload)
        return Response(content=xml, media_type="application/xml")

    return router
