"""Mock ticket system. Production would call GP's ticketing API instead."""

import json
import time

from config.settings import Settings


class TicketStore:
    def __init__(self, settings: Settings) -> None:
        self._path = settings.runtime_dir / "tickets.json"
        self._start = settings.ticket_start_number
        self._tickets: list[dict] = []
        if self._path.exists():
            self._tickets = json.loads(self._path.read_text(encoding="utf-8"))

    def create(self, draft: dict) -> dict:
        number = self._start + len(self._tickets)
        ticket = {"ticket_id": f"TKT-{number}", "created_at": time.time(), **draft}
        self._tickets.append(ticket)
        self._path.write_text(json.dumps(self._tickets, ensure_ascii=False, indent=2), encoding="utf-8")
        return ticket

    def all(self) -> list[dict]:
        return list(self._tickets)

    def clear(self) -> None:
        self._tickets = []
        self._path.unlink(missing_ok=True)
