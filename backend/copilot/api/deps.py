"""Shared FastAPI dependencies."""

from collections.abc import Callable

from fastapi import Depends, HTTPException, Request

from copilot.container import Container
from copilot.services.auth import ROLE_RANK, User
from copilot.services.sessions import CallSession

BEARER = "bearer "


def get_container(request: Request) -> Container:
    return request.app.state.container


def require_session(request: Request) -> CallSession:
    """The call that is on screen, or a 409 when there is none."""
    session = get_container(request).registry.current
    if session is None:
        raise HTTPException(status_code=409, detail="No call in progress")
    return session


def require_user(request: Request) -> User:
    """A valid `Authorization: Bearer <token>`, or 401."""
    header = request.headers.get("authorization", "")
    token = header[len(BEARER):].strip() if header.lower().startswith(BEARER) else ""
    user = get_container(request).auth.verify_token(token)
    if user is None:
        raise HTTPException(
            status_code=401, detail="Sign in required", headers={"WWW-Authenticate": "Bearer"}
        )
    request.state.user = user
    return user


def require_role(minimum: str) -> Callable[..., User]:
    """Dependency factory: the signed-in user must have at least `minimum` (agent < supervisor)."""
    if minimum not in ROLE_RANK:
        raise ValueError(f"unknown role {minimum!r}")

    def check(user: User = Depends(require_user)) -> User:
        if not user.has_role(minimum):
            raise HTTPException(status_code=403, detail="Not allowed for your role")
        return user

    return check


def require_demo_controls(request: Request, user: User = Depends(require_user)) -> User:
    """Demo-only operations (customer picker, data reset). Off unless DEMO_CONTROLS_ENABLED=true."""
    if not get_container(request).settings.demo_controls_enabled:
        raise HTTPException(status_code=403, detail="Demo controls are disabled")
    return user
