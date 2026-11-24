"""Vobiz stream WebSocket endpoint and its internal test/inspection routes.

The WebSocket path is `<VOBIZ_STREAM_WS_PATH>/{call_uuid}` (default /ws/vobiz/stream/{call_uuid}).

The REST routes under /api/vobiz/streams exist to exercise the transport with SYNTHETIC audio
(tones) from a simulated or real stream. They are protected by X-Internal-Token, send no TTS, and
can be removed without affecting the WebSocket endpoint.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.routes.vobiz import require_internal_token
from app.core.config import Settings
from app.integrations.vobiz.media_config import VOBIZ_OUTBOUND_L16_BYTE_ORDER
from app.integrations.vobiz.stream_sender import StreamClosedError
from app.services.audio.formats import AudioFormat, AudioFormatError, outbound_format
from app.services.audio.synthetic import tone
from app.services.voice.call_manager import CallStateManager, StreamSession
from app.services.voice.playback import CheckpointLimitError

logger = logging.getLogger(__name__)


class ToneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_ms: int = Field(default=1000, ge=100, le=5000)
    frequency_hz: float = Field(default=440.0, ge=100, le=3400)
    # L16 byte order is not documented by Vobiz; send both on a real call to hear which is right.
    # Ignored for mu-law, which has 1-byte samples. Default = the verified Vobiz outbound byte order.
    byte_order: Literal["little", "big"] = VOBIZ_OUTBOUND_L16_BYTE_ORDER
    wait_for_playback: bool = False
    # Outbound playAudio format. Omit both to reuse the stream's inbound format. When given, both are
    # required and must be a documented outbound format: audio/x-l16 at 8000/16000/24000 Hz or
    # audio/x-mulaw at 8000 Hz. mu-law avoids the L16 byte-order question entirely.
    content_type: Literal["audio/x-l16", "audio/x-mulaw"] | None = None
    sample_rate: int | None = Field(default=None, ge=1, le=192_000)

    @model_validator(mode="after")
    def _format_pair(self) -> "ToneRequest":
        if (self.content_type is None) != (self.sample_rate is None):
            raise ValueError("content_type and sample_rate must be given together (or both omitted)")
        if self.content_type is not None:
            try:
                outbound_format(self.content_type, self.sample_rate)
            except AudioFormatError as exc:
                raise ValueError(str(exc)) from exc
        return self

    def playback_format(self, stream_format: AudioFormat) -> AudioFormat:
        if self.content_type is None:
            return outbound_format(stream_format.encoding, stream_format.sample_rate)
        return outbound_format(self.content_type, self.sample_rate)


def _active_session(request: Request, call_uuid: str) -> StreamSession:
    manager: CallStateManager = request.app.state.call_manager
    session = manager.current_stream(call_uuid)
    if session is None:
        raise HTTPException(status_code=404, detail="No active stream for this call")
    return session


def create_ws_router(settings: Settings) -> APIRouter:
    router = APIRouter(tags=["vobiz-stream"])
    ws_path = f"{settings.vobiz_stream_ws_path.rstrip('/')}/{{call_uuid}}"

    @router.websocket(ws_path)
    async def vobiz_stream(websocket: WebSocket, call_uuid: str) -> None:
        await websocket.app.state.stream_handler.handle(websocket, call_uuid)

    # M5: monitored streams (opt-in multi-stream, per-track routing). Same authentication as above.
    monitor_path = f"{settings.vobiz_monitor_ws_path.rstrip('/')}/{{call_uuid}}"

    @router.websocket(monitor_path)
    async def vobiz_monitor_stream(websocket: WebSocket, call_uuid: str) -> None:
        await websocket.app.state.stream_handler.handle(websocket, call_uuid, kind="monitor")

    internal = [Depends(require_internal_token)]

    @router.get("/api/vobiz/streams", dependencies=internal)
    async def list_streams(request: Request) -> dict[str, object]:
        """Active calls and per-stream counters. No audio and no phone numbers."""
        manager: CallStateManager = request.app.state.call_manager
        return {
            "connections": request.app.state.stream_handler.connection_count,
            "calls": manager.snapshot(),
            "call_manager": manager.metrics(),
            "transcript_pipeline": request.app.state.transcript_pipeline.snapshot(),  # counters only, no text
        }

    @router.post("/api/vobiz/streams/{call_uuid}/test-tone", dependencies=internal)
    async def send_test_tone(call_uuid: str, body: ToneRequest, request: Request) -> dict[str, object]:
        """Play a synthetic tone on the call's current stream.

        Format: the stream's inbound format unless content_type/sample_rate are given.
        Errors: 404 no active stream, 409 stream closed, 422 bad format, 429 too many unacknowledged
        checkpoints (nothing is sent in that case).
        """
        session = _active_session(request, call_uuid)
        try:
            fmt = body.playback_format(session.audio_format)  # validated before anything is sent
            audio = tone(fmt, body.duration_ms, body.frequency_hz, byte_order=body.byte_order)
            result = await session.playback.play(audio, fmt, wait=body.wait_for_playback)
        except AudioFormatError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except CheckpointLimitError as exc:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"{session.playback.max_pending_checkpoints} checkpoints are still awaiting playedStream; "
                    "nothing was sent. Wait for them to play or time out, or call /clear."
                ),
            ) from exc
        except StreamClosedError as exc:
            raise HTTPException(status_code=409, detail="Stream closed; no further audio can be sent") from exc
        return {
            "call_uuid": call_uuid,
            "stream_id": session.stream_id,
            "audio_format": fmt.label,
            "content_type": fmt.encoding,
            "sample_rate": fmt.sample_rate,
            "stream_inbound_format": session.audio_format.label,
            "byte_order": body.byte_order if fmt.bytes_per_sample == 2 else "n/a",
            "chunks_sent": result.chunks_sent,
            "aborted": result.aborted,
            "checkpoint": result.checkpoint,
            "outcome": result.outcome,
        }

    @router.post("/api/vobiz/streams/{call_uuid}/clear", dependencies=internal)
    async def clear_stream_audio(call_uuid: str, request: Request) -> dict[str, object]:
        """Send clearAudio and report whether Vobiz acknowledged it with clearedAudio."""
        session = _active_session(request, call_uuid)
        try:
            confirmed = await session.playback.clear(wait_confirmed=True)
        except StreamClosedError as exc:
            raise HTTPException(status_code=409, detail="Stream closed while sending") from exc
        return {
            "call_uuid": call_uuid,
            "stream_id": session.stream_id,
            "cleared_audio_confirmed": confirmed,
            "playback_state": session.playback.state.value,
        }

    return router
