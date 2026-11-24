"""Shared fixtures for the Copilot tests.

Guarantees for every test:
* config/.env is never read: Settings are built with `_env_file=None` and explicit values.
* Runtime data (call records, tickets) goes to a temporary folder, never backend/data/runtime.
* No real AWS/Bedrock call can happen: the real client's network path raises if reached.
"""

import asyncio
import json
from functools import lru_cache
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config.settings import BASE_DIR, Settings
from copilot.app_factory import create_app
from copilot.container import Container, build_container
from copilot.services.auth import hash_password

FAST_ARN = "arn:aws:bedrock:ap-south-1:000000000000:inference-profile/test-fast"
SUMMARY_ARN = "arn:aws:bedrock:ap-south-1:000000000000:inference-profile/test-summary"
REAL_RUNTIME_DIR = BASE_DIR / "data" / "runtime"
SESSION_SECRET = "test-session-secret-not-for-production"
INTERNAL_TOKEN = "test-internal-token-not-real"
# Test-only credentials (never real ones).
AGENT = {"username": "tania", "name": "Tania", "role": "agent", "password": "agent-pass-123"}
SUPERVISOR = {"username": "lead", "name": "Team lead", "role": "supervisor", "password": "lead-pass-456"}
DISABLED = {"username": "gone", "name": "Former agent", "role": "agent", "password": "gone-pass-789"}


@lru_cache
def _hash(password: str) -> str:
    return hash_password(password)  # cached: scrypt is deliberately slow


def write_users(path: Path, *people: dict, disabled: tuple[str, ...] = ()) -> Path:
    users = [
        {"username": p["username"], "name": p["name"], "role": p["role"],
         "password_hash": _hash(p["password"]), "disabled": p["username"] in disabled}
        for p in people
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"users": users}), encoding="utf-8")
    return path


class FakeBedrock:
    """Stands in for copilot.services.bedrock.BedrockClient.

    `responses` maps a model id to a dict (returned as the parsed JSON reply), an Exception
    (raised), or a callable(user_prompt) -> dict. Unknown model ids raise, like a missing ARN.
    Every call is recorded in `calls`.
    """

    def __init__(self, responses: dict | None = None) -> None:
        self.responses: dict = dict(responses or {})
        self.calls: list[dict] = []

    async def complete_json(
        self, model_id: str, system: str, user: str, max_tokens: int = 500, temperature: float = 0.1
    ) -> dict:
        self.calls.append({"model_id": model_id, "system": system, "user": user, "max_tokens": max_tokens})
        if not model_id:
            raise RuntimeError("Bedrock inference profile ARN is not set in config/.env")
        if model_id not in self.responses:
            raise RuntimeError(f"FakeBedrock has no response for {model_id}")
        reply = self.responses[model_id]
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(user)
        return dict(reply)

    def calls_for(self, model_id: str) -> list[dict]:
        return [c for c in self.calls if c["model_id"] == model_id]


DEFAULT_ANALYSIS = {"customer_mood": "neutral", "agent_mood": "neutral", "tip": None, "suggestion_id": None}
DEFAULT_SUMMARY = {
    "problem": "Internet not working for 2 days.",
    "checked": "Area network notice.",
    "outcome": "Explained maintenance until 6 pm.",
    "follow_up": "None.",
    "category": "Network",
    "subject": "Internet down in Mirpur",
}


def make_settings(runtime_dir: Path, **overrides) -> Settings:
    values = {
        "public_base_url": "",
        "cors_origins": "",
        "aws_region": "",
        "s3_bucket": "",
        "bedrock_inference_profile_arn_fast": FAST_ARN,
        "bedrock_inference_profile_arn_summary": SUMMARY_ARN,
        "sarvam_api_key": "",
        "demo_default_customer_id": "rahim",
        "demo_phone_map": "",
        "ticket_start_number": 10234,
        "data_backend": "files",  # never reach DynamoDB from a test
        "copilot_runtime_dir": str(runtime_dir),
        "copilot_conversations_dir": str(runtime_dir.parent / "conversations"),
        "internal_api_token": INTERNAL_TOKEN,
        "log_level": "WARNING",
        "copilot_session_secret": SESSION_SECRET,
        "copilot_users_file": str(runtime_dir.parent / "users.json"),
        "copilot_api_docs_enabled": True,  # the contract tests read /openapi.json
        "demo_controls_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _snapshot(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {str(p.relative_to(path)) + f":{p.stat().st_size}:{p.stat().st_mtime_ns}" for p in path.rglob("*")}


@pytest.fixture(scope="session", autouse=True)
def real_runtime_untouched():
    """Fail the run if any test wrote into backend/data/runtime."""
    before = _snapshot(REAL_RUNTIME_DIR)
    yield
    assert _snapshot(REAL_RUNTIME_DIR) == before, "a test wrote into backend/data/runtime"


@pytest.fixture(autouse=True)
def no_real_bedrock(monkeypatch):
    """If any code path reaches the real Bedrock client, fail instead of calling AWS."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("a test tried to create a real AWS/Bedrock client")

    monkeypatch.setattr("copilot.services.bedrock.BedrockClient._get_client", refuse)
    monkeypatch.setattr("copilot.services.storage.CallStorage._upload", refuse)


@pytest.fixture
def runtime_dir(tmp_path) -> Path:
    return tmp_path / "runtime"


@pytest.fixture
def users_file(runtime_dir) -> Path:
    return write_users(runtime_dir.parent / "users.json", AGENT, SUPERVISOR, DISABLED, disabled=("gone",))


@pytest.fixture
def settings(runtime_dir, users_file) -> Settings:
    return make_settings(runtime_dir)


@pytest.fixture
def fake_bedrock() -> FakeBedrock:
    return FakeBedrock({FAST_ARN: DEFAULT_ANALYSIS, SUMMARY_ARN: DEFAULT_SUMMARY})


@pytest.fixture
def container(settings, fake_bedrock) -> Container:
    return build_container(settings, bedrock=fake_bedrock)


class EventLog:
    """Records every hub.broadcast(event, data) made through a container."""

    def __init__(self) -> None:
        self.items: list[tuple[str, object]] = []

    def names(self) -> list[str]:
        return [name for name, _ in self.items]

    def of(self, name: str) -> list[object]:
        return [data for event, data in self.items if event == name]


@pytest.fixture
def events(container, monkeypatch) -> EventLog:
    log = EventLog()
    original = container.hub.broadcast

    async def recording(event: str, data: object) -> None:
        log.items.append((event, data))
        await original(event, data)

    monkeypatch.setattr(container.hub, "broadcast", recording)
    return log


class AuthedTestClient(TestClient):
    """TestClient that signs in once and then sends the bearer token on every request.

    `websocket_connect("/api/panel/stream")` fetches a fresh one-time ticket automatically unless the
    URL already has one. Pass `headers={"Authorization": ""}` to make an anonymous request.
    """

    def sign_in(self, person: dict) -> dict:
        response = self.post("/api/auth/login", json={"username": person["username"], "password": person["password"]})
        assert response.status_code == 200, response.text
        body = response.json()
        self.headers["Authorization"] = f"Bearer {body['token']}"
        return body

    def ws_ticket(self) -> str:
        response = self.post("/api/auth/ws-ticket")
        assert response.status_code == 200, response.text
        return response.json()["ticket"]

    def websocket_connect(self, url: str, *args, **kwargs):
        if url.startswith("/api/panel/stream") and "ticket=" not in url:
            url = f"{url}{'&' if '?' in url else '?'}ticket={self.ws_ticket()}"
        return super().websocket_connect(url, *args, **kwargs)


def open_client(settings: Settings, bedrock, person: dict | None = AGENT):
    """Context manager: app + client, signed in as `person` (None = anonymous)."""
    app = create_app(settings, bedrock=bedrock, serve_frontend=False)

    class _Ctx:
        def __enter__(self):
            self.client = AuthedTestClient(app).__enter__()
            if person is not None:
                self.client.sign_in(person)
            return self.client

        def __exit__(self, *exc):
            return self.client.__exit__(*exc)

    return _Ctx()


@pytest.fixture
def client(settings, fake_bedrock):
    """Signed in as the agent (Tania)."""
    with open_client(settings, fake_bedrock) as test_client:
        yield test_client


async def settle(predicate=None, timeout: float = 2.0) -> None:
    """Let background tasks (analysis, summary) run. With a predicate, wait until it is true."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        for _ in range(20):
            await asyncio.sleep(0)
        if predicate is None or predicate():
            return
        if loop.time() > deadline:
            raise AssertionError("condition not reached before timeout")
        await asyncio.sleep(0.01)
