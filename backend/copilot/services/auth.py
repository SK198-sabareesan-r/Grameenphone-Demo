"""Copilot sign-in: local users file, scrypt password hashes, signed session tokens and one-time
WebSocket tickets.

* Users live in a JSON file (default config/users.json, git-ignored):
    {"users": [{"username": "tania", "name": "Tania", "role": "agent", "password_hash": "scrypt$..."}]}
  Create entries with `python -m scripts.hash_password`.
* A session token is `<payload>.<signature>`: base64url JSON payload, HMAC-SHA256 over it with
  COPILOT_SESSION_SECRET. Tokens are checked against the users file on every request, so removing
  or disabling a user (and restarting) revokes their tokens.
* Browsers cannot send an Authorization header on a WebSocket, so the panel first exchanges its
  bearer token for a short-lived, single-use ticket and passes it as ?ticket=.
* Nothing here logs passwords, hashes, tokens or tickets.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from config.settings import Settings

log = logging.getLogger(__name__)

# Higher roles include the lower ones.
ROLE_RANK = {"agent": 1, "supervisor": 2}
ROLES = tuple(ROLE_RANK)

SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN = 2**14, 8, 1, 32
TOKEN_VERSION = 1
MAX_TICKETS = 10_000
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_S = 300.0


# --- password hashing ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> str:
    """Return `scrypt$<n>$<r>$<p>$<salt>$<hash>` (salt and hash base64url)."""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=SCRYPT_DKLEN)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check. A malformed hash never matches (and never raises)."""
    try:
        scheme, n, r, p, salt, expected = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected_bytes = _unb64(expected)
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
            dklen=len(expected_bytes),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected_bytes)


# Compared against when the username is unknown, so both cases cost one scrypt run.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


# --- users --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class User:
    username: str
    name: str
    role: str

    def public(self) -> dict:
        return {"username": self.username, "name": self.name, "role": self.role}

    def has_role(self, minimum: str) -> bool:
        return ROLE_RANK.get(self.role, 0) >= ROLE_RANK[minimum]


@dataclass(frozen=True)
class _UserRecord:
    user: User
    password_hash: str
    disabled: bool


def load_users(path: Path) -> dict[str, _UserRecord]:
    """Read the users file. Invalid entries are skipped with a warning (no secrets logged)."""
    if not path.exists():
        log.warning("Users file %s not found: nobody can sign in to the Copilot.", path.name)
        return {}
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    users: dict[str, _UserRecord] = {}
    for index, entry in enumerate(raw.get("users", [])):
        username = str(entry.get("username", "")).strip().lower()
        role = entry.get("role", "")
        password_hash = entry.get("password_hash", "")
        if not username or role not in ROLE_RANK or not str(password_hash).startswith("scrypt$"):
            log.warning("Skipping invalid user entry #%d in %s", index, path.name)
            continue
        users[username] = _UserRecord(
            user=User(username=username, name=str(entry.get("name") or username), role=role),
            password_hash=password_hash,
            disabled=bool(entry.get("disabled", False)),
        )
    return users


# --- service ------------------------------------------------------------------------------------


class AuthError(Exception):
    """Raised for sign-in problems the caller should report (not for a wrong password)."""


class AuthService:
    def __init__(self, settings: Settings, *, clock: Callable[[], float] = time.time) -> None:
        self._secret = settings.copilot_session_secret.get_secret_value().encode("utf-8")
        self._session_ttl_s = max(60, settings.copilot_session_ttl_minutes * 60)
        self._ticket_ttl_s = max(5, settings.copilot_ws_ticket_ttl_s)
        self._users = load_users(settings.users_file)
        self._clock = clock
        self._tickets: dict[str, tuple[str, float]] = {}  # ticket -> (username, expires_at)
        self._failures: dict[str, list[float]] = {}  # username -> recent failed attempts
        if not self._secret:
            log.warning("COPILOT_SESSION_SECRET is not set: Copilot sign-in is disabled.")

    @property
    def configured(self) -> bool:
        return bool(self._secret)

    @property
    def user_count(self) -> int:
        return len(self._users)

    # login

    def authenticate(self, username: str, password: str) -> User | None:
        """Return the user for a correct username and password, else None."""
        if not self.configured:
            raise AuthError("Sign-in is not configured (COPILOT_SESSION_SECRET is empty).")
        key = username.strip().lower()
        if self._throttled(key):
            raise AuthError("Too many failed sign-in attempts. Try again in a few minutes.")
        record = self._users.get(key)
        ok = verify_password(password, record.password_hash if record else _DUMMY_HASH)
        if not ok or record is None or record.disabled:
            self._failures.setdefault(key, []).append(self._clock())
            return None
        self._failures.pop(key, None)
        return record.user

    def _throttled(self, key: str) -> bool:
        now = self._clock()
        recent = [t for t in self._failures.get(key, []) if now - t < LOGIN_WINDOW_S]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return len(recent) >= LOGIN_MAX_FAILURES

    # session tokens

    def issue_token(self, user: User) -> tuple[str, int]:
        now = int(self._clock())
        expires_at = now + self._session_ttl_s
        payload = {"v": TOKEN_VERSION, "sub": user.username, "role": user.role, "iat": now, "exp": expires_at}
        body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        return f"{body}.{self._sign(body)}", expires_at

    def verify_token(self, token: str) -> User | None:
        if not self.configured or not token or token.count(".") != 1:
            return None
        body, signature = token.split(".")
        if not hmac.compare_digest(signature, self._sign(body)):
            return None
        try:
            payload = json.loads(_unb64(body))
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict) or payload.get("v") != TOKEN_VERSION:
            return None
        if not isinstance(payload.get("exp"), int) or payload["exp"] <= self._clock():
            return None
        return self._current_user(payload.get("sub"))

    def _sign(self, body: str) -> str:
        return _b64(hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest())

    def _current_user(self, username: object) -> User | None:
        """The user as the users file describes them now (role changes apply, removals revoke)."""
        record = self._users.get(username) if isinstance(username, str) else None
        if record is None or record.disabled:
            return None
        return record.user

    # WebSocket tickets

    def issue_ws_ticket(self, user: User) -> tuple[str, int]:
        self._prune_tickets()
        if len(self._tickets) >= MAX_TICKETS:
            raise AuthError("Too many pending WebSocket tickets.")
        ticket = secrets.token_urlsafe(32)
        self._tickets[ticket] = (user.username, self._clock() + self._ticket_ttl_s)
        return ticket, self._ticket_ttl_s

    def consume_ws_ticket(self, ticket: str | None) -> User | None:
        """Single use: the ticket is removed whether or not it is still valid."""
        if not ticket:
            return None
        entry = self._tickets.pop(ticket, None)
        if entry is None:
            return None
        username, expires_at = entry
        if expires_at <= self._clock():
            return None
        return self._current_user(username)

    def _prune_tickets(self) -> None:
        now = self._clock()
        for ticket in [t for t, (_, exp) in self._tickets.items() if exp <= now]:
            del self._tickets[ticket]
