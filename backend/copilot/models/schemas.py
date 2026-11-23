"""Request bodies for the REST API."""

from typing import Literal

from pydantic import BaseModel, Field

TICKET_CATEGORIES = ["Network", "Billing", "Pack", "SIM", "Roaming", "Other"]


class TranscriptRequest(BaseModel):
    """One typed line, used when live audio is not available."""

    speaker: Literal["agent", "customer"]
    text: str = Field(min_length=1, max_length=1000)


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)


class TicketRequest(BaseModel):
    call_sid: str = ""
    customer_id: str = ""
    category: str = "Other"
    subject: str = Field(default="", max_length=120)
    problem: str = ""
    checked: str = ""
    outcome: str = ""
    follow_up: str = ""


class ActiveCustomerRequest(BaseModel):
    customer_id: str
