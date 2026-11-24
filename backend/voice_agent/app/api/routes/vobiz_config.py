"""Vobiz configuration diagnostics."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.integrations.vobiz.client import VobizClient
from app.integrations.vobiz.diagnostics import build_diagnostics
from app.schemas.vobiz import VobizConnectivity, VobizDiagnostics

router = APIRouter(prefix="/api/v1/vobiz", tags=["vobiz"])


@router.get("/diagnostics", response_model=VobizDiagnostics)
async def diagnostics(
    request: Request, verify: bool = False, currency: str = "INR"
) -> VobizDiagnostics:
    """Show the effective (non-secret) Vobiz config.

    With `verify=true`, also makes one read-only balance request to confirm the credentials work.
    No call is placed. Failures are returned as structured errors by the VobizError handler.
    """
    settings = request.app.state.settings
    if not verify:
        return build_diagnostics(settings)

    async with VobizClient(settings) as client:
        result = await client.check_balance(currency)
    return build_diagnostics(
        settings,
        VobizConnectivity(
            checked=True,
            ok=True,
            currency=result["currency"],
            account_status=result["status"],
            note="Credentials accepted. Balance amounts are intentionally not returned.",
        ),
    )
