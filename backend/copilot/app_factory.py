"""Builds the FastAPI application.

Kept separate from copilot.main so that importing the factory (for example in tests) does not
read config/.env. copilot.main calls create_app() once to produce the served app.

Access rules:
* Public: GET /api/health, POST /api/auth/login, and the built frontend (static files).
* Everything else under /api needs `Authorization: Bearer <token>` (agent role or higher),
  applied here per router so a new route cannot be added unprotected by accident.
* The panel WebSocket checks a one-time ticket itself (see panel_routes.py).
* Demo-only routes additionally need DEMO_CONTROLS_ENABLED=true.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config.settings import Settings, get_settings
from copilot.api import (
    analysis_routes,
    answer_routes,
    ask_routes,
    auth_routes,
    call_routes,
    compliance_routes,
    customer_routes,
    dashboard_routes,
    panel_routes,
    suggestion_routes,
    summary_routes,
    system_routes,
    ticket_routes,
    transcript_routes,
)
from copilot.api.deps import require_role
from copilot.container import build_container
from copilot.services.bedrock import BedrockClient

# Routers that protect themselves: public routes, or routes with their own checks.
SELF_PROTECTED_ROUTERS = (
    system_routes,  # /api/health public; /api/data/reset requires demo controls + sign-in
    auth_routes,  # /api/auth/login public; /me and /ws-ticket require sign-in
    panel_routes,  # WebSocket: one-time ticket
)
# Every route in these needs a signed-in agent (or higher).
AGENT_ROUTERS = (
    call_routes,
    transcript_routes,
    analysis_routes,
    answer_routes,
    compliance_routes,
    suggestion_routes,
    summary_routes,
    ticket_routes,
    ask_routes,
    customer_routes,
    dashboard_routes,
)
ROUTERS = SELF_PROTECTED_ROUTERS + AGENT_ROUTERS


def create_app(
    settings: Settings | None = None,
    *,
    bedrock: BedrockClient | None = None,
    serve_frontend: bool = True,
) -> FastAPI:
    """Create the app. `settings` and `bedrock` can be injected (tests); None uses config/.env and
    the real Bedrock client. The container is built at startup, exactly as before."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(
            level=settings.log_level.upper(),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        app.state.container = build_container(settings, bedrock=bedrock)
        yield

    docs = settings.copilot_api_docs_enabled
    app = FastAPI(
        title="Grameenphone AI Copilot",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    for module in SELF_PROTECTED_ROUTERS:
        app.include_router(module.router)
    agent = [Depends(require_role("agent"))]
    for module in AGENT_ROUTERS:
        app.include_router(module.router, dependencies=agent)

    # The built React app is served from here when frontend/dist exists. During development
    # run the Vite dev server instead; it proxies /api to this backend.
    if serve_frontend and settings.frontend_dist_dir.exists():
        app.mount("/", StaticFiles(directory=settings.frontend_dist_dir, html=True), name="frontend")

    return app
