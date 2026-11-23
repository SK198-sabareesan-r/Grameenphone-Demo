"""Analysis of the current call: run it now, and read mood and the latest result."""

from fastapi import APIRouter, Request

from copilot.api.deps import get_container, require_session

router = APIRouter(prefix="/api", tags=["analysis"])


@router.post("/analyze")
async def analyze_now(request: Request) -> dict:
    """Run mood and next-best-action analysis on the call as it stands."""
    session = require_session(request)
    await get_container(request).analyzer.run_once(session)
    return {"mood": session.mood, "suggestions": list(session.suggestions.values())}


@router.get("/mood")
async def get_mood(request: Request) -> dict:
    session = require_session(request)
    return {"mood": session.mood, "history": session.mood_history}
