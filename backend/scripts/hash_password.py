"""Add or update a Copilot user in the users file (scrypt password hash).

Run from the backend folder:
    python -m scripts.hash_password --username tania --name "Tania" --role agent
    python -m scripts.hash_password --username lead --role supervisor --file config/users.json

The password is read with a hidden prompt (twice). It is never printed or logged; only the hash is
written to the file. The default file is config/users.json, which is git-ignored.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from copilot.services.auth import ROLES, hash_password

DEFAULT_FILE = Path(__file__).resolve().parents[1] / "config" / "users.json"
MIN_PASSWORD_LENGTH = 10


def upsert_user(path: Path, username: str, name: str, role: str, password: str) -> str:
    """Write the user into `path` (created if missing). Returns "added" or "updated"."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    username = username.strip().lower()
    if not username:
        raise ValueError("username must not be empty")
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {"users": []}
    users = data.setdefault("users", [])
    entry = {"username": username, "name": name or username, "role": role,
             "password_hash": hash_password(password), "disabled": False}
    for index, existing in enumerate(users):
        if str(existing.get("username", "")).strip().lower() == username:
            users[index] = entry
            outcome = "updated"
            break
    else:
        users.append(entry)
        outcome = "added"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return outcome


def main(argv: list[str] | None = None, *, prompt=getpass.getpass) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--username", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--role", choices=ROLES, default="agent")
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    args = parser.parse_args(argv)

    password = prompt("Password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
        return 1
    if prompt("Repeat password: ") != password:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    outcome = upsert_user(args.file, args.username, args.name, args.role, password)
    print(f"User {args.username.strip().lower()!r} {outcome} in {args.file}. Restart the Copilot to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
