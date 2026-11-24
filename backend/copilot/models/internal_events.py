"""Request bodies for the internal voice-agent routes (/api/v1/internal/*).

The voice agent is the only caller. Field names follow the M3 contract: `transcript_text`,
`transcript_type`, `sequence_number`, `call_status`, `agent_mode`.
"""

import json
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$"
_SECRET_KEY = re.compile(r"token|secret|password|api[_-]?key|authorization|credential", re.IGNORECASE)

Speaker = Literal["customer", "ai_agent", "human_agent", "unknown"]
AgentMode = Literal["ai_agent", "human_agent"]
CallStatus = Literal[
    "in_progress", "ai_resolution_attempt", "handoff_requested", "dialing_human", "with_human", "on_hold",
    "human_disconnected", "handoff_failed", "ended",
]
CallEventType = Literal[
    "call_started",
    "ai_agent_connected",
    "ai_resolution_attempt",
    "human_handoff_requested",
    "human_agent_dialing",
    "human_agent_connected",
    "human_handoff_failed",
    "customer_put_on_hold",
    "customer_resumed",
    "ai_agent_assist_activated",
    "human_agent_disconnected",
    "call_ended",
]


class TranscriptEventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(pattern=ID_PATTERN)
    call_id: str = Field(pattern=ID_PATTERN)
    session_id: str = Field(pattern=ID_PATTERN)
    customer_id: str | None = Field(default=None, pattern=ID_PATTERN)
    speaker: Speaker
    # How the speaker was decided (for example "primary_inbound_track"). "unknown" must say why.
    speaker_basis: str | None = Field(default=None, max_length=64)
    transcript_text: str = Field(min_length=1, max_length=4000)
    transcript_type: Literal["partial", "final"]
    timestamp: datetime
    sequence_number: int = Field(ge=0)
    call_status: CallStatus
    agent_mode: AgentMode
    language: str | None = Field(default=None, max_length=16)

    @field_validator("timestamp")
    @classmethod
    def _needs_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must include a timezone (for example Z)")
        return value


class CallEventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(pattern=ID_PATTERN)
    call_id: str = Field(pattern=ID_PATTERN)
    session_id: str = Field(pattern=ID_PATTERN)
    customer_id: str | None = Field(default=None, pattern=ID_PATTERN)
    type: CallEventType
    timestamp: datetime
    source: str = Field(default="voice_agent", max_length=32)
    # Event details. Examples: reason and pending_actions (hand-off), agent_ref (human agent, already
    # masked), hold source, from_number (call_started only; never stored as given).
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def _needs_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must include a timezone (for example Z)")
        return value

    @field_validator("data")
    @classmethod
    def _bounded_and_clean(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 20 or len(json.dumps(value, default=str)) > 8192:
            raise ValueError("data is too large")
        return {k: v for k, v in value.items() if not _SECRET_KEY.search(k)}  # never keep credentials


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
