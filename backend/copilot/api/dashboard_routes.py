"""Adoption dashboard."""

from fastapi import APIRouter, Request

from copilot.api.deps import get_container

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/dashboard")
async def get_dashboard(request: Request) -> dict:
    return get_container(request).dashboard.build()
