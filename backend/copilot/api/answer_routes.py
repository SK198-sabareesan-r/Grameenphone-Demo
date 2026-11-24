"""Automatic lookups for the customer's sentences: read them, or run one on request."""

from fastapi import APIRouter, HTTPException, Request

from copilot.api.deps import get_container, require_session

router = APIRouter(prefix="/api", tags=["answers"])


@router.get("/answers")
async def list_answers(request: Request) -> dict:
    """Lookups made so far for the current call, oldest first."""
    session = require_session(request)
    return {"answers": session.answers}


@router.post("/answers")
async def run_answer(request: Request) -> dict:
    """Look up the customer's latest sentence now, even if it looked like small talk."""
    session = require_session(request)
    entry = await get_container(request).assist.run_now(session)
    if entry is None:
        raise HTTPException(status_code=409, detail="The customer has not said anything yet")
    return entry
