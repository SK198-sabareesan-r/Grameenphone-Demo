"""Internal diagnostics for the AI voice conversation (counters and per-call phase; never text)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.routes.vobiz import require_internal_token

router = APIRouter(tags=["conversation"])


@router.get("/api/v1/ai/conversations", dependencies=[Depends(require_internal_token)])
async def conversation_status(request: Request) -> dict[str, object]:
    """Metrics and per-call state of the AI voice conversation. Needs the X-Internal-Token header."""
    manager = getattr(request.app.state, "conversation", None)
    return manager.snapshot() if manager is not None else {"enabled": False}
