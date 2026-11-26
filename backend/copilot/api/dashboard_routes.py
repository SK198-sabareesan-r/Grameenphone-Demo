"""Adoption dashboard."""

from datetime import date

from fastapi import APIRouter, HTTPException, Query, Request

from copilot.api.deps import get_container

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/dashboard")
async def get_dashboard(
    request: Request,
    from_date: date | None = Query(None, description="First day, inclusive (Bangladesh date), YYYY-MM-DD"),
    to_date: date | None = Query(None, description="Last day, inclusive (Bangladesh date), YYYY-MM-DD"),
) -> dict:
    if from_date and to_date and from_date > to_date:
        raise HTTPException(status_code=422, detail="The start date is after the end date")
    return await get_container(request).dashboard.build(from_date=from_date, to_date=to_date)
