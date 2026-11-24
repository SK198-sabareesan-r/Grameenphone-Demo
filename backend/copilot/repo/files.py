"""The mock data from the local JSON files. For tests, for offline work, and as the fallback.

Reference data comes from the same seed builders the DynamoDB seed script uses. What the app writes
(its tickets and the calls) is kept in memory and mirrored to files in the runtime folder.
"""

import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from config.settings import Settings
from copilot.repo.seed import build_seed
from copilot.utils.dynamo import from_dynamo

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


class FileRepo:
    name = "files"

    def __init__(self, settings: Settings, runtime_dir: Path | None = None) -> None:
        seed = build_seed(settings, datetime.now(timezone.utc))
        self._customers = {c["mobile_number"]: from_dynamo(c) for c in seed["customers"]}
        self._areas = {a["area"]: from_dynamo(a) for a in seed["network-areas"]}
        self._packs = {p["pack_id"]: from_dynamo(p) for p in seed["packs"]}
        self._centres = [from_dynamo(c) for c in seed["gp-centres"]]
        self._seed_tickets = [from_dynamo(t) for t in seed["tickets"]]
        self._dir = runtime_dir if runtime_dir is not None else settings.runtime_dir
        self._tickets_file = self._dir / "tickets.json"
        self._calls_dir = self._dir / "calls"
        self._calls_dir.mkdir(parents=True, exist_ok=True)
        self._app_tickets: list[dict] = self._read_tickets()
        self._calls: dict[str, dict] = self._read_calls()

    # ---- persistence ----

    def _read_tickets(self) -> list[dict]:
        if not self._tickets_file.exists():
            return []
        return json.loads(self._tickets_file.read_text(encoding="utf-8"))

    def _read_calls(self) -> dict[str, dict]:
        calls = {}
        for path in self._calls_dir.glob("*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            calls[record["call_id"]] = record
        return calls

    def _write_tickets(self) -> None:
        self._tickets_file.write_text(json.dumps(self._app_tickets, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- the Repo interface ----

    async def get_customer(self, mobile_number: str) -> dict | None:
        return copy.deepcopy(self._customers.get(mobile_number))

    async def list_areas(self) -> list[dict]:
        return copy.deepcopy(list(self._areas.values()))

    async def get_area(self, area: str) -> dict | None:
        return copy.deepcopy(self._areas.get(area))

    async def list_packs(self) -> list[dict]:
        return copy.deepcopy(list(self._packs.values()))

    async def get_pack(self, pack_id: str) -> dict | None:
        return copy.deepcopy(self._packs.get(pack_id))

    async def list_centres(self, area: str) -> list[dict]:
        return copy.deepcopy([c for c in self._centres if c["area"] == area])

    async def list_tickets(self) -> list[dict]:
        return copy.deepcopy(self._seed_tickets + self._app_tickets)

    async def list_open_tickets(self, customer_id: str) -> list[dict]:
        tickets = await self.list_tickets()
        return [t for t in tickets if t["customer_id"] == customer_id and t.get("status") == "open"]

    async def create_ticket(self, ticket: dict) -> bool:
        key = (ticket["customer_id"], ticket["ticket_id"])
        if any((t["customer_id"], t["ticket_id"]) == key for t in await self.list_tickets()):
            return False
        self._app_tickets.append(copy.deepcopy(ticket))
        self._write_tickets()
        return True

    async def save_call(self, record: dict) -> None:
        self._calls[record["call_id"]] = copy.deepcopy(record)
        path = self._calls_dir / f"{_UNSAFE.sub('_', record['call_id'])}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    async def list_calls(self) -> list[dict]:
        return copy.deepcopy(list(self._calls.values()))

    async def clear_runtime(self) -> None:
        self._app_tickets = []
        self._tickets_file.unlink(missing_ok=True)
        self._calls = {}
        for path in self._calls_dir.glob("*.json"):
            path.unlink()

    def status(self) -> dict:
        return {"backend": self.name, "fallback_active": False, "reason": None}
