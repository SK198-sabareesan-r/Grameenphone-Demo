"""Transcript: read the call so far, or add a typed line when live audio is not available."""

from fastapi import APIRouter, Request

from copilot.api.deps import get_container, require_session
from copilot.models.schemas import TranscriptRequest
from copilot.services.pipeline import ensure_pipeline

router = APIRouter(prefix="/api", tags=["transcript"])


@router.get("/transcript")
async def get_transcript(request: Request) -> dict:
    session = require_session(request)
    return {"call_sid": session.call_sid, "turns": [t.__dict__ for t in session.turns]}


@router.post("/transcript")
async def add_transcript_line(body: TranscriptRequest, request: Request) -> dict:
    """Add one line. It goes through the same steps as live speech."""
    pipeline = await ensure_pipeline(get_container(request))
    await pipeline.ingest(body.speaker, "final", body.text)
    return {"call_sid": pipeline.session.call_sid, "turns": len(pipeline.session.turns)}
