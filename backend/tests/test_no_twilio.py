"""M1: Twilio is gone from the Copilot backend, its config and the frontend source."""

import importlib.util

import pytest
from starlette.websockets import WebSocketDisconnect

from config.settings import BASE_DIR, Settings

REPO = BASE_DIR.parent
# Active code, configuration and docs. Historical plans and the npm lock file are not scanned.
SCANNED = [
    *(BASE_DIR / "copilot").rglob("*.py"),
    *(BASE_DIR / "config").glob("*.py"),
    BASE_DIR / "config" / ".env.example",
    BASE_DIR / "requirements.txt",
    BASE_DIR / "pyproject.toml",
    *(BASE_DIR / "scripts").glob("*.py"),
    *(REPO / "frontend" / "src").rglob("*.js"),
    *(REPO / "frontend" / "src").rglob("*.jsx"),
    REPO / "frontend" / "package.json",
    REPO / "frontend" / "index.html",
    REPO / "frontend" / "vite.config.js",
    REPO / "README.md",
]


@pytest.mark.parametrize("path", SCANNED, ids=lambda p: str(p.relative_to(REPO)))
def test_no_twilio_reference(path):
    assert "twilio" not in path.read_text(encoding="utf-8").lower()


def test_twilio_modules_and_settings_are_gone():
    assert importlib.util.find_spec("copilot.api.twilio_routes") is None
    assert importlib.util.find_spec("copilot.services.twilio_helpers") is None
    assert not [name for name in Settings.model_fields if "twilio" in name]
    assert not (REPO / "frontend" / "src" / "hooks" / "useSoftphone.js").exists()


def test_twilio_routes_no_longer_exist(client):
    assert client.post("/api/twilio/voice").status_code == 404
    assert client.post("/api/twilio/recording").status_code == 404
    assert client.get("/api/softphone/token").status_code == 404
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/twilio/stream") as ws:
            ws.receive_text()
