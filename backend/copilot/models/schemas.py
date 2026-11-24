"""Request bodies for the REST API."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from copilot.models.participants import normalize_speaker

TICKET_CATEGORIES = ["Network", "Billing", "Pack", "SIM", "Roaming", "Other"]


class TranscriptRequest(BaseModel):
    """One typed line, used when live audio is not available.

    speaker: customer | human_agent | ai_agent. The older "agent" is accepted and means human_agent.
    """

    speaker: Literal["customer", "human_agent", "ai_agent", "agent"]
    text: str = Field(min_length=1, max_length=1000)

    @field_validator("speaker")
    @classmethod
    def _to_role(cls, value: str) -> str:
        return normalize_speaker(value)


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


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
