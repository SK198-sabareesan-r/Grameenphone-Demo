"""Sarvam configuration diagnostics."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.schemas.sarvam import AuthCheck, SarvamDiagnostics
from app.services.sarvam.client import SarvamClient
from app.services.sarvam.diagnostics import build_diagnostics

router = APIRouter(prefix="/api/sarvam", tags=["sarvam"])


@router.get("/diagnostics", response_model=SarvamDiagnostics)
async def diagnostics(request: Request, verify: bool = False) -> SarvamDiagnostics:
    """Show the effective (non-secret) Sarvam config. With `verify=true`, also test the API key.

    Sarvam failures (bad key, no credits, timeout) are returned as structured errors by the
    app-level SarvamError handler.
    """
    settings = request.app.state.settings
    if not verify:
        return build_diagnostics(settings)

    async with SarvamClient(settings) as client:
        result = await client.verify_auth()
    return build_diagnostics(
        settings,
        AuthCheck(
            checked=True,
            ok=True,
            request_id=result.get("request_id"),
            note="API key accepted. STT/TTS model access is not verified by this check.",
        ),
    )
