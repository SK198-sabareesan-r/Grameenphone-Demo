"""FastAPI application entry point.

Run from the backend folder:  uvicorn copilot.main:app --host 0.0.0.0 --port 8000
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config.settings import get_settings
from copilot.api import (
    analysis_routes,
    answer_routes,
    ask_routes,
    call_routes,
    compliance_routes,
    dashboard_routes,
    identity_routes,
    location_routes,
    suggestion_routes,
    summary_routes,
    system_routes,
    ticket_routes,
    transcript_routes,
    twilio_routes,
)
from copilot.container import build_container

ROUTERS = (
    system_routes,
    twilio_routes,
    call_routes,
    transcript_routes,
    analysis_routes,
    answer_routes,
    compliance_routes,
    suggestion_routes,
    summary_routes,
    ticket_routes,
    ask_routes,
    identity_routes,
    location_routes,
    dashboard_routes,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app.state.container = build_container(settings)
    yield


settings = get_settings()
app = FastAPI(title="Grameenphone AI Copilot", lifespan=lifespan)

if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )

for module in ROUTERS:
    app.include_router(module.router)

# The built React app is served from here when frontend/dist exists. During development
# run the Vite dev server instead; it proxies /api to this backend.
if settings.frontend_dist_dir.exists():
    app.mount("/", StaticFiles(directory=settings.frontend_dist_dir, html=True), name="frontend")
