"""Sign-in: login (public), current user, and one-time tickets for the panel WebSocket."""

from fastapi import APIRouter, Depends, HTTPException, Request

from copilot.api.deps import get_container, require_user
from copilot.models.schemas import LoginRequest
from copilot.services.auth import AuthError, User

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
async def login(body: LoginRequest, request: Request) -> dict:
    auth = get_container(request).auth
    try:
        user = auth.authenticate(body.username, body.password)
    except AuthError as exc:
        status = 503 if not auth.configured else 429
        raise HTTPException(status_code=status, detail=str(exc)) from None
    if user is None:
        raise HTTPException(status_code=401, detail="Wrong username or password")
    token, expires_at = auth.issue_token(user)
    return {"token": token, "token_type": "bearer", "expires_at": expires_at, "user": user.public()}


@router.get("/me")
async def me(request: Request, user: User = Depends(require_user)) -> dict:
    settings = get_container(request).settings
    return {"user": user.public(), "demo_controls_enabled": settings.demo_controls_enabled}


@router.post("/ws-ticket")
async def ws_ticket(request: Request, user: User = Depends(require_user)) -> dict:
    try:
        ticket, expires_in = get_container(request).auth.issue_ws_ticket(user)
    except AuthError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return {"ticket": ticket, "expires_in": expires_in}
