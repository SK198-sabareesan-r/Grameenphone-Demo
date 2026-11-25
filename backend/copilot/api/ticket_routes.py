"""Tickets in the mock ticket system: create them, and list them with filters."""
from datetime import date

from fastapi import APIRouter, HTTPException, Query, Request

from copilot.api.deps import get_container
from copilot.models.schemas import TICKET_CATEGORIES, TicketRequest
from copilot.services.ticket_filter import ALL, DEFAULT_LIMIT, DEFAULT_STATUS, STATUSES, filter_tickets

router = APIRouter(prefix="/api/tickets", tags=["tickets"])


@router.get("/categories")
async def ticket_categories() -> dict:
    return {"categories": TICKET_CATEGORIES}


@router.get("/statuses")
async def ticket_statuses() -> dict:
    return {"statuses": list(STATUSES), "default": DEFAULT_STATUS}


@router.get("")
async def list_tickets(
    request: Request,
    mobile: str = Query("", max_length=24, description="The customer's mobile number, or some digits of it"),
    ticket: str = Query("", max_length=24, description="The ticket number, or part of it"),
    from_date: date | None = Query(None, description="First day, inclusive (Bangladesh date), YYYY-MM-DD"),
    to_date: date | None = Query(None, description="Last day, inclusive (Bangladesh date), YYYY-MM-DD"),
    status: str = Query(DEFAULT_STATUS, description="open (the default), in_progress, resolved, closed or all"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=1000),
) -> dict:
    """The tickets that match every filter given, newest first. Without a status filter only open tickets are listed."""
    if status != ALL and status not in STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of: {', '.join((ALL, *STATUSES))}")
    if from_date and to_date and from_date > to_date:
        raise HTTPException(status_code=422, detail="The start date is after the end date")
    tickets, total = filter_tickets(
        await get_container(request).tickets.all(),
        mobile=mobile, ticket_no=ticket, date_from=from_date, date_to=to_date, status=status, limit=limit,
    )
    return {"tickets": tickets, "count": len(tickets), "total": total}


@router.post("")
async def create_ticket(body: TicketRequest, request: Request) -> dict:
    c = get_container(request)
    if body.category not in TICKET_CATEGORIES:
        raise HTTPException(status_code=422, detail="Unknown ticket category")
    session = c.registry.get(body.call_sid) or c.registry.current
    draft = body.model_dump()
    customer = session.customer if session else None
    if customer and body.customer_id == customer["id"]:
        # Keep the customer's own number and name on the ticket, for the tickets list and its filters.
        draft.update(customer_id=customer["id"], mobile_number=customer["msisdn"], customer_name=customer["name"])
    if session and session.caller_number:
        draft["caller_number"] = session.caller_number
    ticket = await c.tickets.create(draft)
    if session:
        session.ticket = ticket
        await c.storage.save(session.to_record())
    await c.hub.broadcast("ticket.created", ticket)
    return ticket
