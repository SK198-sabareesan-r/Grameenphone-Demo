"""Next-best-action suggestions for the current call."""

from fastapi import APIRouter, HTTPException, Request

from copilot.api.deps import get_container, require_session

router = APIRouter(prefix="/api", tags=["suggestions"])


@router.get("/suggestions")
async def list_suggestions(request: Request) -> dict:
    session = require_session(request)
    return {"suggestions": list(session.suggestions.values())}


@router.post("/suggestions/{suggestion_id}/use")
async def use_suggestion(suggestion_id: str, request: Request) -> dict:
    c = get_container(request)
    session = require_session(request)
    if not c.nba.mark_used(session, suggestion_id):
        raise HTTPException(status_code=404, detail="Suggestion not found or already used")
    await c.hub.broadcast("suggestions", list(session.suggestions.values()))
    return {"ok": True}
