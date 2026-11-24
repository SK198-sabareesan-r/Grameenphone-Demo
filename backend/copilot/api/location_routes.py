"""Where the customer is right now, and the network status there."""

from fastapi import APIRouter, HTTPException, Request

from copilot.api.deps import get_container, require_session
from copilot.models.schemas import LocationRequest
from copilot.services.location import UnknownArea

router = APIRouter(prefix="/api", tags=["location"])


@router.get("/locations")
async def list_locations(request: Request) -> dict:
    """Areas the agent can pick from."""
    areas = get_container(request).areas
    await areas.ensure_loaded()
    return {"locations": areas.names()}


@router.get("/location")
async def get_location(request: Request) -> dict:
    session = require_session(request)
    return {
        "registered_status": session.registered_status,
        "current_location": session.current_location,
        "prompt": session.location_prompt,
    }


@router.put("/location")
async def set_location(body: LocationRequest, request: Request) -> dict:
    """The customer told the agent where they are. Checks the network status at that location."""
    session = require_session(request)
    if session.ended_at is not None:
        raise HTTPException(status_code=409, detail="The call has ended")
    try:
        status = await get_container(request).location.set_current(session, body.area)
    except UnknownArea:
        raise HTTPException(
            status_code=404, detail="No known area matches that location. Pick one from the list."
        ) from None
    return {"current_location": status, "registered_status": session.registered_status}
