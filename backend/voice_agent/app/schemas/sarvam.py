"""Schemas for Sarvam endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class SttConfigInfo(BaseModel):
    model: str
    default_language: str
    stream_type: str
    rest_endpoint: str
    realtime_endpoint: str
    # Query parameters the realtime client sends (the API key is a header, never a parameter).
    realtime_params: dict[str, str] = {}
    realtime_config_valid: bool = True


class TtsConfigInfo(BaseModel):
    model: str
    language_code: str
    speaker: str | None
    sample_rate: int


class AuthCheck(BaseModel):
    checked: bool
    ok: bool | None = None
    request_id: str | None = None
    note: str | None = None


class SarvamDiagnostics(BaseModel):
    """Non-secret view of the Sarvam configuration. The API key is never included."""

    api_key_configured: bool
    stt: SttConfigInfo
    tts: TtsConfigInfo
    warnings: list[str]
    auth: AuthCheck
