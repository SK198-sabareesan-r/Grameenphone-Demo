"""Call state: the current call and ending it. (The live panel WebSocket is in panel_routes.py.)"""

from fastapi import APIRouter, Depends, HTTPException, Request

from copilot.api.deps import get_container, require_user
from copilot.models.internal_events import NoteIn
from copilot.models.schemas import CallStartRequest
from copilot.services import conversation_store as store, voice_calls
from copilot.services.auth import User
from copilot.services.customers import is_plausible_number
from copilot.services.pipeline import CallPipeline, session_view

router = APIRouter(prefix="/api", tags=["calls"])


@router.get("/calls/current")
async def current_call(request: Request) -> dict:
    c = get_container(request)
    return {"call": session_view(c, c.registry.current) if c.registry.current else None}


@router.post("/calls", status_code=201)
async def start_call(body: CallStartRequest, request: Request) -> dict:
    """Start a call on the panel without telephony. With a caller ID, the matching customer card is shown at
    once, marked as not confirmed, exactly as for a phone call that reaches the human agent."""
    c = get_container(request)
    if c.registry.current and c.registry.current.ended_at is None:
        raise HTTPException(status_code=409, detail="A call is already on screen. End it first.")
    caller = body.caller_number.strip()
    if caller and not is_plausible_number(caller):
        raise HTTPException(status_code=422, detail="Enter the full mobile number (at least 10 digits)")
    pipeline = CallPipeline(c)
    await pipeline.begin(call_sid=None, caller=caller, source="typed")
    return {"call": session_view(c, pipeline.session)}


@router.post("/calls/current/end")
async def end_current_call(request: Request) -> dict:
    """Close the call on screen. The summary is written in the background."""
    c = get_container(request)
    session, pipeline = c.registry.current, c.registry.pipeline
    if not session or not pipeline or session.ended_at:
        raise HTTPException(status_code=409, detail="No active call")
    await pipeline.finish()
    return {"ok": True}

@router.get("/calls/{call_id}/context")
async def call_context(call_id: str, request: Request) -> dict:
    """Hand-off package for the human agent: customer, intent, the AI conversation, the reason for the
    transfer, a short summary, the live conversation so far, notes, hold events and pending actions."""
    c = get_container(request)
    try:
        if not await c.conversations.exists(call_id):
            raise HTTPException(status_code=404, detail="No stored conversation for this call")
        return await c.conversations.handoff_package(call_id)
    except store.InvalidIdentifier as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/calls/{call_id}/notes", status_code=201)
async def add_call_note(call_id: str, body: NoteIn, request: Request, user: User = Depends(require_user)) -> dict:
    """A human-agent note. Stored in that call's human_agent_context.json."""
    c = get_container(request)
    try:
        if not await c.conversations.exists(call_id):
            raise HTTPException(status_code=404, detail="No stored conversation for this call")
        return await voice_calls.add_note(c, call_id, body.text, user.username)
    except store.InvalidIdentifier as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except store.StorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
