from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_health_returns_ok() -> None:
    client = TestClient(create_app(Settings()))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_settings_do_not_leak_secrets() -> None:
    settings = Settings(sarvam_api_key="super-secret")  # type: ignore[arg-type]
    assert "super-secret" not in repr(settings)
    assert "super-secret" not in settings.model_dump_json()


def test_settings_defaults_match_spec() -> None:
    settings = Settings()
    assert settings.sarvam_model == "saaras:v4"
    assert settings.sarvam_language == "auto"
    assert settings.sarvam_stream_type == "fast"
