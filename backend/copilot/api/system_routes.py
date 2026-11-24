"""Health (public) and demo data reset (demo controls only)."""

from fastapi import APIRouter, Depends, Request

from copilot.api.deps import get_container, require_demo_controls

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.post("/data/reset", dependencies=[Depends(require_demo_controls)])
async def reset_data(request: Request) -> dict:
    """Clear the current call, tickets and saved call records."""
    c = get_container(request)
    c.registry.reset()
    c.tickets.clear()
    c.storage.clear()
    await c.hub.broadcast("reset", {})
    return {"ok": True}
