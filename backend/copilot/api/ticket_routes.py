"""Tickets in the mock ticket system."""

from fastapi import APIRouter, HTTPException, Request

from copilot.api.deps import get_container
from copilot.models.schemas import TICKET_CATEGORIES, TicketRequest

router = APIRouter(prefix="/api/tickets", tags=["tickets"])


@router.get("/categories")
async def ticket_categories() -> dict:
    return {"categories": TICKET_CATEGORIES}


@router.get("")
async def list_tickets(request: Request) -> dict:
    return {"tickets": await get_container(request).tickets.all()}


@router.post("")
async def create_ticket(body: TicketRequest, request: Request) -> dict:
    c = get_container(request)
    if body.category not in TICKET_CATEGORIES:
        raise HTTPException(status_code=422, detail="Unknown ticket category")
    ticket = await c.tickets.create(body.model_dump())
    session = c.registry.get(body.call_sid) or c.registry.current
    if session:
        session.ticket = ticket
        await c.storage.save(session.to_record())
    await c.hub.broadcast("ticket.created", ticket)
    return ticket
