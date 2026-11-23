"""Call state: the live event stream, the current call and ending it."""

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from copilot.api.deps import get_container
from copilot.services.pipeline import session_view

router = APIRouter(prefix="/api", tags=["calls"])


@router.websocket("/panel/stream")
async def panel_stream(ws: WebSocket) -> None:
    c = ws.app.state.container
    await c.hub.connect(ws)
    try:
        if c.registry.current:
            await c.hub.send(ws, "snapshot", session_view(c, c.registry.current))
        while True:
            await ws.receive_text()  # the panel only listens; this detects disconnects
    except WebSocketDisconnect:
        pass
    finally:
        c.hub.disconnect(ws)


@router.get("/calls/current")
async def current_call(request: Request) -> dict:
    c = get_container(request)
    return {"call": session_view(c, c.registry.current) if c.registry.current else None}


@router.post("/calls/current/end")
async def end_current_call(request: Request) -> dict:
    """Close the call on screen. The summary is written in the background."""
    c = get_container(request)
    session, pipeline = c.registry.current, c.registry.pipeline
    if not session or not pipeline or session.ended_at:
        raise HTTPException(status_code=409, detail="No active call")
    await pipeline.finish()
    return {"ok": True}
