"""Session history: finished calls with their transcript, customer card, suggested answers, summary and ticket."""
from fastapi import APIRouter, HTTPException, Query, Request

from copilot.api.deps import get_container
from copilot.services.session_history import DEFAULT_LIMIT, list_items, record_view

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("")
async def list_sessions(
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=200),
    q: str = Query("", max_length=60, description="Part of a name, mobile number, call id, ticket number or subject"),
) -> dict:
    """Saved calls, newest first."""
    records = await get_container(request).storage.all_records()
    return {"sessions": list_items(records, limit=limit, search=q)}


@router.get("/{call_id}")
async def get_session(call_id: str, request: Request) -> dict:
    """One saved call, shaped like the live call so the panel can show it again."""
    c = get_container(request)
    for record in await c.storage.all_records():
        if record.get("call_id") == call_id:
            return {"call": record_view(c, record)}
    raise HTTPException(status_code=404, detail="No saved session with that id")
