"""After-call summary and ticket draft."""

from fastapi import APIRouter, HTTPException, Request

from copilot.api.deps import get_container, require_session

router = APIRouter(prefix="/api", tags=["summary"])


@router.get("/summary")
async def get_summary(request: Request) -> dict:
    session = require_session(request)
    if session.summary is None:
        raise HTTPException(status_code=404, detail="Summary is not ready yet")
    return {"summary": session.summary}


@router.post("/summary")
async def generate_summary(request: Request) -> dict:
    """Write the summary for the call as it stands. Also used to regenerate it."""
    session = require_session(request)
    await get_container(request).summarizer.run(session)
    return {"summary": session.summary}
