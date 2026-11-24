"""Shared FastAPI dependencies."""

import hmac
import logging
from collections.abc import Callable

from fastapi import Depends, HTTPException, Request

from copilot.container import Container
from copilot.services.auth import ROLE_RANK, User
from copilot.services.sessions import CallSession

log = logging.getLogger(__name__)
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


INTERNAL_TOKEN_HEADER = "X-Internal-Token"


def require_internal_token(request: Request) -> None:
    """Service-to-service calls from the voice agent: `X-Internal-Token` must equal INTERNAL_API_TOKEN.

    Same header and variable name the voice agent already uses for its own control routes. Fails closed
    (503) while the token is not configured. The token is never logged.
    """
    expected = get_container(request).settings.internal_api_token.get_secret_value()
    if not expected:
        raise HTTPException(status_code=503, detail="INTERNAL_API_TOKEN is not configured")
    supplied = request.headers.get(INTERNAL_TOKEN_HEADER, "")
    if not hmac.compare_digest(expected.encode(), supplied.encode("utf-8", "replace")):
        log.warning("internal_auth_failed path=%s", request.url.path)
        raise HTTPException(status_code=401, detail="Missing or invalid internal token")
