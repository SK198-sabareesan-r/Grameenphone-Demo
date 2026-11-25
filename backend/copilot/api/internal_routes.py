"""Internal routes for the voice agent: live transcript events and call lifecycle events.

Not for browsers. Every route needs the X-Internal-Token header (see deps.require_internal_token), so this
router protects itself and is listed with the self-protected routers in app_factory.py.

Status codes: 201 stored, 200 duplicate (same event_id, safe to retry), 401 bad token, 409 the session_id
or sequence_number conflicts with what is stored for the call, 422 invalid body, 503 token not configured
or the JSON file could not be written (the voice agent retries those).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from copilot.api.deps import get_container, require_internal_token
from copilot.models.internal_events import CallEventIn, TranscriptEventIn
from copilot.services import conversation_store as store
from copilot.services import voice_calls

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/internal", tags=["internal"], dependencies=[Depends(require_internal_token)]
)


def _http_error(exc: store.StoreError) -> HTTPException:
    if isinstance(exc, (store.SessionMismatch, store.SequenceConflict)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, store.InvalidIdentifier):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=503, detail=str(exc), headers={"Retry-After": "2"})


class KnowledgeSearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=3, ge=1, le=5)


@router.post("/knowledge/search")
async def search_knowledge(body: KnowledgeSearchIn, request: Request) -> dict:
    """Approved-article search for the voice agent's AI replies (Bedrock Knowledge Base, local fallback)."""
    articles = await get_container(request).retriever.search(body.query, body.limit)
    return {
        "articles": [
            {"id": a.id, "title": a.title, "type": a.type, "body": a.body[:1500]} for a in articles
        ]
    }


@router.post("/transcripts/events", status_code=201)
async def receive_transcript_event(body: TranscriptEventIn, request: Request, response: Response) -> dict:
    try:
        result = await voice_calls.handle_transcript_event(get_container(request), body)
    except store.StoreError as exc:
        raise _http_error(exc) from exc
    if result == "duplicate":
        response.status_code = 200
    return {"status": result, "event_id": body.event_id}


@router.post("/calls/events", status_code=201)
async def receive_call_event(body: CallEventIn, request: Request, response: Response) -> dict:
    try:
        result = await voice_calls.handle_call_event(get_container(request), body)
    except store.StoreError as exc:
        raise _http_error(exc) from exc
    if result == "duplicate":
        response.status_code = 200
    return {"status": result, "event_id": body.event_id}
