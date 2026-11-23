"""Health, browser softphone token and data reset."""

from fastapi import APIRouter, HTTPException, Request

from copilot.api.deps import get_container
from copilot.services.twilio_helpers import make_access_token

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


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
    """Clear the current call, tickets and saved call records."""
    c = get_container(request)
    c.registry.reset()
    c.tickets.clear()
    c.storage.clear()
    await c.hub.broadcast("reset", {})
    return {"ok": True}
