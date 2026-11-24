"""M1: Copilot sign-in, bearer protection, one-time WebSocket tickets, roles and demo controls."""

import json
import re
import time

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from copilot.api.deps import require_role
from copilot.app_factory import create_app
from copilot.services.auth import AuthService, User, hash_password, load_users, verify_password
from scripts import hash_password as hash_script
from tests.conftest import (
    AGENT, DISABLED, SESSION_SECRET, SUPERVISOR, AuthedTestClient, make_settings, open_client, write_users,
)

PUBLIC = {("get", "/api/health"), ("post", "/api/auth/login")}
NO_AUTH = {"Authorization": ""}


def _api_routes(client) -> list[tuple[str, str]]:
    paths = client.get("/openapi.json").json()["paths"]
    return [(method, path) for path, ops in paths.items() for method in ops]


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def _assert_ws_rejected(client, url: str) -> None:
    with pytest.raises(WebSocketDisconnect) as info:
        with TestClient.websocket_connect(client, url) as ws:
            ws.receive_text()
    assert info.value.code == 4401


# --- public routes and blanket protection --------------------------------------------------------


def test_health_and_login_are_public(client):
    assert client.get("/api/health", headers=NO_AUTH).json() == {"status": "ok"}
    response = client.post("/api/auth/login", headers=NO_AUTH,
                           json={"username": AGENT["username"], "password": AGENT["password"]})
    assert response.status_code == 200


def test_every_other_api_route_rejects_anonymous_requests(client):
    routes = [r for r in _api_routes(client) if r not in PUBLIC and not r[1].startswith("/api/v1/internal/")]  # internal routes: token header, see test_internal_api.py
    assert len(routes) >= 20  # guard against the route listing silently shrinking
    for method, path in routes:
        response = client.request(method.upper(), _concrete(path), headers=NO_AUTH, json={})
        assert response.status_code == 401, f"{method.upper()} {path} -> {response.status_code}"
        assert response.headers.get("www-authenticate") == "Bearer"


@pytest.mark.parametrize("header", [
    "Bearer not-a-token",
    "Bearer a.b",
    "Basic dGFuaWE6cGFzcw==",
    "Bearer ",
    "tania",
])
def test_malformed_credentials_are_rejected(client, header):
    assert client.get("/api/calls/current", headers={"Authorization": header}).status_code == 401


def test_tampered_and_foreign_tokens_are_rejected(client, settings):
    token = client.headers["Authorization"].split(" ", 1)[1]
    body, signature = token.split(".")
    flipped = signature[:-1] + ("A" if signature[-1] != "A" else "B")
    assert client.get("/api/calls/current", headers={"Authorization": f"Bearer {body}.{flipped}"}).status_code == 401
    # Same user, signed with a different secret.
    other = AuthService(make_settings(settings.runtime_dir, copilot_session_secret="another-secret"))
    foreign, _ = other.issue_token(User("tania", "Tania", "agent"))
    assert client.get("/api/calls/current", headers={"Authorization": f"Bearer {foreign}"}).status_code == 401
    # A payload edited to claim the supervisor role fails the signature check.
    payload = json.loads(__import__("base64").urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["role"] = "supervisor"
    forged = __import__("base64").urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    assert client.get("/api/calls/current", headers={"Authorization": f"Bearer {forged}.{signature}"}).status_code == 401


def test_expired_tokens_are_rejected(client, settings):
    auth = client.app.state.container.auth
    assert client.get("/api/calls/current").status_code == 200
    ttl = settings.copilot_session_ttl_minutes * 60
    auth._clock = lambda: time.time() + ttl + 1
    assert client.get("/api/calls/current").status_code == 401


def test_openapi_and_docs_are_off_by_default(runtime_dir, users_file, fake_bedrock):
    settings = make_settings(runtime_dir, copilot_api_docs_enabled=False)
    with open_client(settings, fake_bedrock, person=None) as c:
        assert c.get("/openapi.json").status_code == 404
        assert c.get("/docs").status_code == 404


# --- login and current user --------------------------------------------------------------------


def test_login_returns_a_token_and_the_public_user(client):
    body = client.sign_in(SUPERVISOR)
    assert body["token_type"] == "bearer" and body["expires_at"] > time.time()
    assert body["user"] == {"username": "lead", "name": "Team lead", "role": "supervisor"}
    me = client.get("/api/auth/me").json()
    assert me == {"user": body["user"], "demo_controls_enabled": False}
    assert "password" not in json.dumps(body) and "scrypt" not in json.dumps(me)


def test_login_is_case_insensitive_on_the_username(client):
    response = client.post("/api/auth/login", json={"username": "  TANIA ", "password": AGENT["password"]})
    assert response.status_code == 200


def test_wrong_password_and_unknown_user_get_the_same_answer(client):
    wrong = client.post("/api/auth/login", json={"username": "tania", "password": "nope"})
    unknown = client.post("/api/auth/login", json={"username": "nobody", "password": "nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "Wrong username or password"}


def test_disabled_users_cannot_sign_in(client):
    response = client.post("/api/auth/login", json={"username": DISABLED["username"], "password": DISABLED["password"]})
    assert response.status_code == 401


def test_login_validation(client):
    assert client.post("/api/auth/login", json={"username": "", "password": "x"}).status_code == 422
    assert client.post("/api/auth/login", json={"username": "tania"}).status_code == 422


def test_repeated_failures_are_throttled_per_username(client):
    for _ in range(5):
        assert client.post("/api/auth/login", json={"username": "tania", "password": "bad"}).status_code == 401
    blocked = client.post("/api/auth/login", json={"username": "tania", "password": AGENT["password"]})
    assert blocked.status_code == 429
    other = client.post("/api/auth/login", json={"username": "lead", "password": SUPERVISOR["password"]})
    assert other.status_code == 200
    auth = client.app.state.container.auth
    auth._clock = lambda: time.time() + 301  # the window passes
    assert client.post("/api/auth/login", json={"username": "tania", "password": AGENT["password"]}).status_code == 200


def test_sign_in_is_refused_without_a_session_secret(runtime_dir, users_file, fake_bedrock):
    settings = make_settings(runtime_dir, copilot_session_secret="")
    with open_client(settings, fake_bedrock, person=None) as c:
        response = c.post("/api/auth/login", json={"username": "tania", "password": AGENT["password"]})
        assert response.status_code == 503
        assert c.get("/api/calls/current").status_code == 401
        assert c.get("/api/health").status_code == 200


def test_a_missing_users_file_means_nobody_can_sign_in(runtime_dir, fake_bedrock):
    settings = make_settings(runtime_dir, copilot_users_file=str(runtime_dir.parent / "absent.json"))
    with open_client(settings, fake_bedrock, person=None) as c:
        assert c.post("/api/auth/login", json={"username": "tania", "password": AGENT["password"]}).status_code == 401


def test_removing_or_disabling_a_user_revokes_their_tokens_after_restart(settings, users_file):
    token, _ = AuthService(settings).issue_token(User("tania", "Tania", "agent"))
    assert AuthService(settings).verify_token(token) is not None
    write_users(users_file, AGENT, SUPERVISOR, disabled=("tania",))
    assert AuthService(settings).verify_token(token) is None
    write_users(users_file, SUPERVISOR)
    assert AuthService(settings).verify_token(token) is None


def test_role_comes_from_the_users_file_not_the_token(settings, users_file):
    token, _ = AuthService(settings).issue_token(User("tania", "Tania", "agent"))
    promoted = dict(AGENT, role="supervisor")
    write_users(users_file, promoted, SUPERVISOR)
    assert AuthService(settings).verify_token(token).role == "supervisor"


# --- WebSocket tickets -------------------------------------------------------------------------


def test_ws_ticket_requires_sign_in(client):
    assert client.post("/api/auth/ws-ticket", headers=NO_AUTH).status_code == 401
    body = client.post("/api/auth/ws-ticket").json()
    assert set(body) == {"ticket", "expires_in"} and body["expires_in"] == 60


def test_panel_stream_rejects_missing_unknown_and_empty_tickets(client):
    _assert_ws_rejected(client, "/api/panel/stream")
    _assert_ws_rejected(client, "/api/panel/stream?ticket=")
    _assert_ws_rejected(client, "/api/panel/stream?ticket=made-up")


def test_a_ticket_works_once(client):
    ticket = client.ws_ticket()
    with client.websocket_connect(f"/api/panel/stream?ticket={ticket}") as ws:
        client.post("/api/transcript", json={"speaker": "customer", "text": "Hello"})
        assert ws.receive_json()["type"] == "call.started"
    _assert_ws_rejected(client, f"/api/panel/stream?ticket={ticket}")


def test_expired_tickets_are_rejected(client):
    ticket = client.ws_ticket()
    client.app.state.container.auth._clock = lambda: time.time() + 61
    _assert_ws_rejected(client, f"/api/panel/stream?ticket={ticket}")


def test_a_ticket_stops_working_when_its_user_is_disabled(settings, users_file):
    auth = AuthService(settings)
    ticket, _ = auth.issue_ws_ticket(User("tania", "Tania", "agent"))
    auth._users.pop("tania")  # as if the user disappeared from the file
    assert auth.consume_ws_ticket(ticket) is None


# --- roles and demo controls -------------------------------------------------------------------


def test_role_checks(settings, fake_bedrock):
    app = create_app(settings, bedrock=fake_bedrock, serve_frontend=False)

    async def supervisor_only() -> dict:
        return {"ok": True}

    app.add_api_route("/api/test/supervisor", supervisor_only, dependencies=[Depends(require_role("supervisor"))])
    with AuthedTestClient(app) as c:
        assert c.get("/api/test/supervisor").status_code == 401
        c.sign_in(AGENT)
        assert c.get("/api/test/supervisor").status_code == 403
        c.sign_in(SUPERVISOR)
        assert c.get("/api/test/supervisor").json() == {"ok": True}
        assert c.get("/api/calls/current").status_code == 200  # supervisors include the agent role
    with pytest.raises(ValueError):
        require_role("admin")


def test_demo_controls_are_disabled_by_default(client):
    assert client.post("/api/data/reset").status_code == 403
    assert client.post("/api/data/reset", headers=NO_AUTH).status_code == 401


def test_demo_controls_work_when_enabled(runtime_dir, users_file, fake_bedrock):
    settings = make_settings(runtime_dir, demo_controls_enabled=True)
    with open_client(settings, fake_bedrock) as c:
        assert c.get("/api/auth/me").json()["demo_controls_enabled"] is True
        c.post("/api/transcript", json={"speaker": "customer", "text": "Hello"})
        assert c.get("/api/calls/current").json()["call"] is not None
        assert c.post("/api/data/reset").json() == {"ok": True}
        assert c.get("/api/calls/current").json() == {"call": None}
        assert c.post("/api/data/reset", headers=NO_AUTH).status_code == 401


# --- the existing Copilot features after sign-in -----------------------------------------------


def test_signed_in_agent_can_run_a_typed_call_end_to_end(client):
    with client.websocket_connect("/api/panel/stream") as ws:
        assert client.post("/api/transcript", json={"speaker": "customer", "text": "Net kaj korche na."}).status_code == 200
        assert ws.receive_json()["type"] == "call.started"
        assert client.post("/api/transcript", json={"speaker": "agent", "text": "Ami dekhchi."}).json()["turns"] == 2
        assert client.post("/api/calls/current/end").json() == {"ok": True}
        while ws.receive_json()["type"] != "summary":
            pass
    assert client.get("/api/summary").json()["summary"]["category"] == "Network"
    assert client.get("/api/dashboard").json()["live"]["calls"] == 1
    assert client.post("/api/ask", json={"question": "zzqx plorp vrumm"}).status_code == 200


# --- password hashing and the users file -------------------------------------------------------


def test_password_hash_format_and_verification():
    encoded = hash_password("correct horse")
    assert encoded.startswith("scrypt$16384$8$1$") and encoded.count("$") == 5
    assert verify_password("correct horse", encoded) is True
    assert verify_password("wrong", encoded) is False
    assert hash_password("correct horse") != encoded  # random salt
    with pytest.raises(ValueError):
        hash_password("")


@pytest.mark.parametrize("bad", ["", "plain-text", "scrypt$x$8$1$aa$bb", "bcrypt$1$2$3$4$5", "scrypt$16384$8$1$!!$??"])
def test_malformed_hashes_never_match(bad):
    assert verify_password("anything", bad) is False


def test_invalid_user_entries_are_skipped(tmp_path):
    path = tmp_path / "users.json"
    path.write_text(json.dumps({"users": [
        {"username": "ok", "role": "agent", "password_hash": hash_password("pw-123456789")},
        {"username": "admin", "role": "admin", "password_hash": hash_password("pw-123456789")},
        {"username": "plain", "role": "agent", "password_hash": "pw-123456789"},
        {"role": "agent", "password_hash": hash_password("pw-123456789")},
    ]}), encoding="utf-8")
    assert set(load_users(path)) == {"ok"}


def test_hash_password_script_adds_and_updates_users_without_printing_the_password(tmp_path, capsys, runtime_dir):
    path = tmp_path / "users.json"
    answers = iter(["s3cret-password", "s3cret-password"])
    assert hash_script.main(["--username", "Rafi", "--name", "Rafi", "--file", str(path)],
                            prompt=lambda _: next(answers)) == 0
    out = capsys.readouterr()
    assert "s3cret-password" not in out.out + out.err and "scrypt" not in out.out
    settings = make_settings(runtime_dir, copilot_users_file=str(path))
    assert AuthService(settings).authenticate("rafi", "s3cret-password") == User("rafi", "Rafi", "agent")

    answers = iter(["another-password", "another-password"])
    hash_script.main(["--username", "rafi", "--role", "supervisor", "--file", str(path)], prompt=lambda _: next(answers))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["users"]) == 1 and data["users"][0]["role"] == "supervisor"
    assert AuthService(settings).authenticate("rafi", "s3cret-password") is None


def test_hash_password_script_rejects_short_or_mismatched_passwords(tmp_path):
    path = tmp_path / "users.json"
    assert hash_script.main(["--username", "a", "--file", str(path)], prompt=lambda _: "short") == 1
    answers = iter(["long-enough-1", "long-enough-2"])
    assert hash_script.main(["--username", "a", "--file", str(path)], prompt=lambda _: next(answers)) == 1
    assert not path.exists()


def test_the_session_secret_is_never_part_of_the_token(client):
    token = client.headers["Authorization"].split(" ", 1)[1]
    assert SESSION_SECRET not in token
