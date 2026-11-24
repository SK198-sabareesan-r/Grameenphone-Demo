"""Customers (mock GP directory) and which customer the next call belongs to."""

from fastapi import APIRouter, HTTPException, Request

from fastapi import Depends

from copilot.api.deps import get_container, require_demo_controls
from copilot.models.schemas import ActiveCustomerRequest

router = APIRouter(prefix="/api/customers", tags=["customers"])


@router.get("")
async def list_customers(request: Request) -> dict:
    c = get_container(request)
    return {"customers": [{"id": x["id"], "name": x["name"]} for x in c.customers.all()]}


# Declared before "/{customer_id}" so "active" is not read as an id.
@router.get("/active")
async def get_active_customer(request: Request) -> dict:
    c = get_container(request)
    return {"active": c.customers.override_id or c.settings.demo_default_customer_id}


@router.put("/active", dependencies=[Depends(require_demo_controls)])
async def set_active_customer(body: ActiveCustomerRequest, request: Request) -> dict:
    c = get_container(request)
    if not c.customers.get(body.customer_id):
        raise HTTPException(status_code=404, detail="Unknown customer")
    c.customers.override_id = body.customer_id
    return {"active": body.customer_id}


@router.get("/{customer_id}")
async def get_customer(customer_id: str, request: Request) -> dict:
    customer = get_container(request).customers.get(customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Unknown customer")
    return customer
