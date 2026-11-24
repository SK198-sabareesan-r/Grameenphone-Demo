"""FastAPI application entry point.

Run from the backend folder:  uvicorn copilot.main:app --host 0.0.0.0 --port 8000
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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
from copilot.repo import DataUnavailable

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
    container = app.state.container = build_container(settings)
    try:
        await container.areas.load()  # the area names the agent can type, ready before the first call
    except Exception:
        log.exception("Could not load the network areas at startup. They are loaded on first use.")
    yield


async def data_unavailable_handler(_: Request, error: DataUnavailable) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": f"The data store is not available ({error})"})


log = logging.getLogger(__name__)
settings = get_settings()
app = FastAPI(title="Grameenphone AI Copilot", lifespan=lifespan)
app.add_exception_handler(DataUnavailable, data_unavailable_handler)

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
