"""Shared FastAPI dependencies."""

from fastapi import HTTPException, Request
from twilio.request_validator import RequestValidator

from copilot.container import Container
from copilot.services.sessions import CallSession


def get_container(request: Request) -> Container:
    return request.app.state.container


def require_session(request: Request) -> CallSession:
    """The call that is on screen, or a 409 when there is none."""
    session = get_container(request).registry.current
    if session is None:
        raise HTTPException(status_code=409, detail="No call in progress")
    return session


async def verify_twilio_signature(request: Request) -> None:
    """Reject webhook calls not signed by Twilio, when enabled in config/.env."""
    settings = request.app.state.container.settings
    if not settings.twilio_validate_signature:
        return
    form = await request.form()
    url = settings.public_base_url.rstrip("/") + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    validator = RequestValidator(settings.twilio_auth_token.get_secret_value())
    signature = request.headers.get("X-Twilio-Signature", "")
    if not validator.validate(url, dict(form), signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
