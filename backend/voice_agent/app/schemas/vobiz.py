"""Schemas for Vobiz endpoints."""

from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class VobizApiInfo(BaseModel):
    base_url: str
    auth_id_hint: str | None  # masked, e.g. "MA_...AB12"
    credentials_configured: bool
    request_timeout_s: float


class VobizCallInfo(BaseModel):
    caller_id_configured: bool
    caller_id_valid_e164: bool = False
    caller_id_hint: str | None = None  # last 4 digits only, e.g. ***5535
    validate_signature: bool
    public_base_url_configured: bool
    internal_api_token_configured: bool = False
    answer_url: str | None = None  # URL to configure/send to Vobiz; None until PUBLIC_BASE_URL is set
    hangup_url: str | None = None
    stream_content_type: str = ""


class VobizConnectivity(BaseModel):
    checked: bool
    ok: bool | None = None
    currency: str | None = None
    account_status: str | None = None
    note: str | None = None


class VobizDiagnostics(BaseModel):
    """Non-secret view of the Vobiz configuration. Credentials are never included."""

    api: VobizApiInfo
    calls: VobizCallInfo
    warnings: list[str]
    connectivity: VobizConnectivity


# --- outbound calls --------------------------------------------------------------------------


class OutboundCallRequest(BaseModel):
    """Body of POST /api/v1/vobiz/calls. The answer URL is never accepted from callers."""

    model_config = ConfigDict(extra="forbid")

    to: str = Field(description="Destination in E.164, e.g. +8801XXXXXXXXX")

    @field_validator("to")
    @classmethod
    def _valid_destination(cls, value: str) -> str:
        from app.integrations.vobiz.phone import normalize_e164

        return normalize_e164(value)


class OutboundCallResponse(BaseModel):
    success: bool = True
    call_uuid: str
    api_id: str | None = None
    message: str | None = None
    to: str  # masked, e.g. ***1234
    note: str = "Call accepted and queued by Vobiz; it has not necessarily been answered."


# --- webhooks --------------------------------------------------------------------------------

_DIRECTIONS = {"inbound", "outbound"}


class _VobizWebhookBase(BaseModel):
    """Common fields of Vobiz form-encoded callbacks. Unknown fields are ignored."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    call_uuid: str = Field(alias="CallUUID", min_length=1, max_length=128)
    event: str | None = Field(default=None, alias="Event", max_length=64)
    direction: str = Field(alias="Direction")
    from_number: str | None = Field(default=None, alias="From", max_length=128)
    to_number: str | None = Field(default=None, alias="To", max_length=128)

    @field_validator("call_uuid")
    @classmethod
    def _no_whitespace(cls, value: str) -> str:
        if any(ch.isspace() or ord(ch) < 32 for ch in value):
            raise ValueError("must not contain whitespace or control characters")
        return value

    @field_validator("direction")
    @classmethod
    def _known_direction(cls, value: str) -> str:
        lowered = value.strip().lower()
        if lowered not in _DIRECTIONS:
            raise ValueError("must be 'inbound' or 'outbound'")
        return lowered


class VobizAnswerWebhook(_VobizWebhookBase):
    """Sent to answer_url (Event=StartApp, CallStatus=in-progress for outbound calls)."""

    from_number: str = Field(alias="From", min_length=1, max_length=128)  # type: ignore[assignment]
    to_number: str = Field(alias="To", min_length=1, max_length=128)  # type: ignore[assignment]
    call_status: str | None = Field(default=None, alias="CallStatus", max_length=32)


class VobizHangupWebhook(_VobizWebhookBase):
    """Sent to hangup_url. Reports the A-leg only, never B-leg events."""

    call_status: str | None = Field(
        default=None, validation_alias=AliasChoices("CallStatus", "Status"), max_length=32
    )
    hangup_cause: str | None = Field(default=None, alias="HangupCause", max_length=128)
    hangup_cause_code: str | None = Field(default=None, alias="HangupCauseCode", max_length=16)
    hangup_cause_name: str | None = Field(default=None, alias="HangupCauseName", max_length=128)
    hangup_source: str | None = Field(default=None, alias="HangupSource", max_length=64)
    duration: str | None = Field(default=None, alias="Duration", max_length=16)
    bill_duration: str | None = Field(default=None, alias="BillDuration", max_length=16)
    start_time: str | None = Field(default=None, alias="StartTime", max_length=64)
    answer_time: str | None = Field(default=None, alias="AnswerTime", max_length=64)
    end_time: str | None = Field(default=None, alias="EndTime", max_length=64)
