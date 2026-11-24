"""Mock ticket system. Production would call GP's ticketing API instead."""

import re
from datetime import datetime, timezone

from config.settings import Settings
from copilot.repo.base import Repo

UNIDENTIFIED = "unidentified"  # the key for tickets raised on a call where no customer was confirmed
MAX_ATTEMPTS = 5
_NUMBER = re.compile(r"(\d+)$")


class TicketService:
    def __init__(self, settings: Settings, repo: Repo) -> None:
        self._start = settings.ticket_start_number
        self._repo = repo

    def _next_number(self, existing: list[dict]) -> int:
        numbers = [int(m.group(1)) for t in existing if (m := _NUMBER.search(t.get("ticket_id", "")))]
        return max([self._start] + [n + 1 for n in numbers])

    async def create(self, draft: dict) -> dict:
        """Number and save a new ticket. If two are created at once, the loser takes the next number."""
        for _ in range(MAX_ATTEMPTS):
            number = self._next_number(await self._repo.list_tickets())
            ticket = {
                **draft,
                "customer_id": draft.get("customer_id") or UNIDENTIFIED,
                "ticket_id": f"TKT-{number}",
                "status": "open",
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "source": "app",
            }
            if await self._repo.create_ticket(ticket):
                return ticket
        raise RuntimeError("Could not find a free ticket number")

    async def all(self) -> list[dict]:
        return sorted(await self._repo.list_tickets(), key=lambda t: t.get("created_at", ""))
