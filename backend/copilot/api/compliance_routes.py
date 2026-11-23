"""Compliance reminders for the current call."""

from fastapi import APIRouter, Request

from copilot.api.deps import get_container, require_session

router = APIRouter(prefix="/api", tags=["compliance"])


@router.get("/compliance")
async def get_compliance(request: Request) -> dict:
    session = require_session(request)
    return {"items": get_container(request).compliance.view(session.compliance)}


@router.get("/compliance/rules")
async def list_compliance_rules(request: Request) -> dict:
    rules = get_container(request).compliance.rules
    return {"rules": [{"id": r["id"], "label": r["label"]} for r in rules]}
