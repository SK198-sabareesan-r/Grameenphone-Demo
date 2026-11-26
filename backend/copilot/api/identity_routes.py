"""Customer identification: the agent confirms the mobile number, and only then is the customer card loaded."""

from fastapi import APIRouter, HTTPException, Request

from copilot.api.deps import get_container, require_session
from copilot.models.schemas import IdentityRequest
from copilot.services.customers import is_plausible_number
from copilot.services.identity import UnknownMobileNumber

router = APIRouter(prefix="/api", tags=["identity"])


@router.get("/identity")
async def get_identity(request: Request) -> dict:
    session = require_session(request)
    return {
        "identified": session.customer is not None,
        "caller_number": session.caller_number,
        "card": session.card(),
    }


@router.post("/identity")
async def confirm_identity(body: IdentityRequest, request: Request) -> dict:
    """The agent has confirmed this mobile number with the customer. Loads the customer card."""
    session = require_session(request)
    if session.ended_at is not None:
        raise HTTPException(status_code=409, detail="The call has ended")
    if not is_plausible_number(body.mobile_number):
        raise HTTPException(status_code=422, detail="Enter the full mobile number (at least 10 digits)")
    try:
        card = await get_container(request).identity.confirm(session, body.mobile_number)
    except UnknownMobileNumber:
        raise HTTPException(status_code=404, detail="No customer found for that mobile number") from None
    await get_container(request).guide.update(session)  # ticks "Confirm the mobile number"
    return {"identified": True, "card": card}


@router.delete("/identity")
async def clear_identity(request: Request) -> dict:
    """Wrong customer picked: hide the card again."""
    session = require_session(request)
    await get_container(request).identity.clear(session)
    await get_container(request).guide.update(session, reset_number=True)  # the number must be confirmed again
    return {"identified": False, "card": None}
