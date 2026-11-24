"""Health, browser softphone token and data reset."""

from fastapi import APIRouter, HTTPException, Request

from copilot.api.deps import get_container
from copilot.services.twilio_helpers import make_access_token

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health(request: Request) -> dict:
    """Also shows where the data and the knowledge come from. fallback_active means that source could not be reached."""
    c = get_container(request)
    return {"status": "ok", "data": c.repo.status(), "knowledge": c.retriever.status()}


@router.get("/softphone/token")
async def softphone_token(request: Request) -> dict:
    settings = get_container(request).settings
    missing = settings.missing(
        "twilio_account_sid", "twilio_api_key_sid", "twilio_api_key_secret", "twilio_twiml_app_sid"
    )
    if missing:
        raise HTTPException(status_code=503, detail=f"Missing in config/.env: {', '.join(missing)}")
    return {"token": make_access_token(settings), "identity": settings.twilio_agent_identity}


@router.post("/data/reset")
async def reset_data(request: Request) -> dict:
    """Clear the current call, the tickets the app created, and all saved calls. Seeded data is kept."""
    c = get_container(request)
    c.registry.reset()
    await c.repo.clear_runtime()
    await c.hub.broadcast("reset", {})
    return {"ok": True}
