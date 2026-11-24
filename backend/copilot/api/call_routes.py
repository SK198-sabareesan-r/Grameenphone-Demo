"""Call state: the current call and ending it. (The live panel WebSocket is in panel_routes.py.)"""

from fastapi import APIRouter, HTTPException, Request

from copilot.api.deps import get_container
from copilot.services.pipeline import session_view

router = APIRouter(prefix="/api", tags=["calls"])


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
