"""Health (public) and demo data reset (demo controls only)."""

from fastapi import APIRouter, Depends, Request

from copilot.api.deps import get_container, require_demo_controls

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health(request: Request) -> dict:
    """Also shows where the data comes from. fallback_active means DynamoDB could not be reached."""
    return {"status": "ok", "data": get_container(request).repo.status()}


@router.post("/data/reset", dependencies=[Depends(require_demo_controls)])
async def reset_data(request: Request) -> dict:
    """Clear the current call, the tickets the app created, and all saved calls. Seeded data is kept."""
    c = get_container(request)
    c.registry.reset()
    await c.repo.clear_runtime()
    await c.hub.broadcast("reset", {})
    return {"ok": True}
