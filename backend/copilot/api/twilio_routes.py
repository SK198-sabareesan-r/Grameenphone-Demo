"""Twilio-facing endpoints: voice webhook, recording callback and media stream."""

import json
import logging

from fastapi import APIRouter, Depends, Form, Request, Response, WebSocket, WebSocketDisconnect

from copilot.api.deps import get_container, verify_twilio_signature
from copilot.services.pipeline import CallPipeline
from copilot.services.twilio_helpers import build_incoming_twiml

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/twilio", tags=["twilio"])


@router.post("/voice", dependencies=[Depends(verify_twilio_signature)])
async def incoming_call(request: Request, From: str = Form(default="")) -> Response:
    twiml = build_incoming_twiml(get_container(request).settings, caller=From)
    return Response(content=twiml, media_type="application/xml")


@router.post("/recording", dependencies=[Depends(verify_twilio_signature)])
async def recording_ready(
    request: Request, CallSid: str = Form(default=""), RecordingUrl: str = Form(default="")
) -> Response:
    c = get_container(request)
    session = c.registry.get(CallSid)
    if session:
        session.recording_url = RecordingUrl
        await c.hub.broadcast("recording", {"url": RecordingUrl})
    return Response(status_code=204)


@router.websocket("/stream")
async def media_stream(ws: WebSocket) -> None:
    await ws.accept()
    pipeline = CallPipeline(ws.app.state.container)
    try:
        while True:
            await pipeline.handle(json.loads(await ws.receive_text()))
    except WebSocketDisconnect:
        log.info("Twilio stream disconnected")
    finally:
        await pipeline.finish()
