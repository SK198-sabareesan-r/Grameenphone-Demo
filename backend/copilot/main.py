"""FastAPI application entry point.

Run from the backend folder:  uvicorn copilot.main:app --host 0.0.0.0 --port 8000
"""

from copilot.app_factory import ROUTERS, create_app

__all__ = ["ROUTERS", "app", "create_app"]

app = create_app()
