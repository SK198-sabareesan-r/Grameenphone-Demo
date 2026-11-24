"""Configuration diagnostics for the Vobiz integration."""

from __future__ import annotations

from app.core.config import Settings
from app.schemas.vobiz import (
    VobizApiInfo,
    VobizCallInfo,
    VobizConnectivity,
    VobizDiagnostics,
)

from app.integrations.vobiz.phone import mask_number, normalize_e164
from app.integrations.vobiz.xml_builder import INBOUND_CONTENT_TYPES

VERIFIED_BASE_URL = "https://api.vobiz.ai/api/v1"


def mask_auth_id(auth_id: str) -> str | None:
    """Show only the prefix and last 4 characters of the Auth ID."""
    if not auth_id:
        return None
    if len(auth_id) <= 8:
        return "***"
    return f"{auth_id[:3]}...{auth_id[-4:]}"


def _caller_id_e164(s: Settings) -> str | None:
    try:
        return normalize_e164(s.vobiz_caller_id)
    except ValueError:
        return None


def collect_warnings(s: Settings) -> list[str]:
    w: list[str] = []
    if not s.vobiz_auth_id:
        w.append("VOBIZ_AUTH_ID is not set.")
    if not s.vobiz_auth_token.get_secret_value():
        w.append("VOBIZ_AUTH_TOKEN is not set.")
    if s.vobiz_base_url != VERIFIED_BASE_URL:
        w.append(f"VOBIZ_BASE_URL differs from the documented value {VERIFIED_BASE_URL}.")
    if not s.vobiz_base_url.startswith("https://"):
        w.append("VOBIZ_BASE_URL is not https; credentials would be sent unencrypted.")
    if not s.vobiz_caller_id:
        w.append("VOBIZ_CALLER_ID is not set; outbound calls are disabled until it is.")
    elif _caller_id_e164(s) is None:
        w.append("VOBIZ_CALLER_ID is not a valid E.164 number (e.g. +919XXXXXXXXX).")
    elif _caller_id_e164(s).startswith("+91"):
        w.append(
            "VOBIZ_CALLER_ID is an Indian number. Vobiz India rules (media anchoring) allow it to call "
            "destinations in India only; calls to other countries are expected to fail."
        )
    if not s.vobiz_validate_signature:
        w.append("VOBIZ_VALIDATE_SIGNATURE is false; use only for local debugging.")
    if not s.public_base_url:
        w.append("PUBLIC_BASE_URL is not set (needed to build the answer and hangup URLs).")
    elif not s.public_base_url.startswith("https://"):
        w.append("PUBLIC_BASE_URL is not https; Vobiz needs a public https URL for webhooks.")
    if not s.internal_api_token.get_secret_value():
        w.append("INTERNAL_API_TOKEN is not set; POST /api/vobiz/calls is disabled until it is.")
    if s.vobiz_sarvam_stt_enabled:
        w.append("VOBIZ_SARVAM_STT_ENABLED=true: every Vobiz stream opens its own Sarvam realtime STT session.")
    if s.vobiz_dev_capture_enabled:
        w.append(
            "VOBIZ_DEV_CAPTURE_ENABLED=true: inbound audio of registered TEST calls is saved to disk "
            f"(active only when APP_ENV=development; APP_ENV is '{s.app_env}'). Disable after testing."
        )
    if s.vobiz_inbound_content_type not in INBOUND_CONTENT_TYPES:
        w.append(
            f"VOBIZ_INBOUND_CONTENT_TYPE '{s.vobiz_inbound_content_type}' is not one of "
            f"{sorted(INBOUND_CONTENT_TYPES)}."
        )
    return w


def build_diagnostics(
    s: Settings, connectivity: VobizConnectivity | None = None
) -> VobizDiagnostics:
    return VobizDiagnostics(
        api=VobizApiInfo(
            base_url=s.vobiz_base_url,
            auth_id_hint=mask_auth_id(s.vobiz_auth_id),
            credentials_configured=bool(
                s.vobiz_auth_id and s.vobiz_auth_token.get_secret_value()
            ),
            request_timeout_s=s.vobiz_request_timeout_s,
        ),
        calls=VobizCallInfo(
            caller_id_configured=bool(s.vobiz_caller_id),
            caller_id_valid_e164=_caller_id_e164(s) is not None,
            caller_id_hint=mask_number(s.vobiz_caller_id) or None,
            validate_signature=s.vobiz_validate_signature,
            public_base_url_configured=bool(s.public_base_url),
            internal_api_token_configured=bool(s.internal_api_token.get_secret_value()),
            answer_url=f"{s.public_base_url}{s.vobiz_answer_webhook_path}" if s.public_base_url else None,
            hangup_url=f"{s.public_base_url}{s.vobiz_hangup_webhook_path}" if s.public_base_url else None,
            stream_content_type=s.vobiz_inbound_content_type,
        ),
        warnings=collect_warnings(s),
        connectivity=connectivity
        or VobizConnectivity(checked=False, note="Pass ?verify=true to test the credentials."),
    )
