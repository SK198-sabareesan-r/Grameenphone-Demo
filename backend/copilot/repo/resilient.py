"""Use DynamoDB, and switch to the local files when DynamoDB cannot be reached.

When a DynamoDB call fails, the same call is answered from the files, and DynamoDB is not tried again for
retry_after_s seconds, so a dead connection does not slow every request. Anything written while DynamoDB
was down stays in the local files only. The status is shown by GET /api/health.
"""

import logging
import time
from collections.abc import Callable

from copilot.repo.base import DataUnavailable, Repo

log = logging.getLogger(__name__)


class ResilientRepo:
    name = "dynamodb"

    def __init__(
        self, primary: Repo, fallback: Repo, retry_after_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._primary, self._fallback = primary, fallback
        self._retry_after_s = retry_after_s
        self._clock = clock
        self._down_until = 0.0
        self._reason: str | None = None

    async def _call(self, method: str, *args):
        if self._clock() >= self._down_until:
            try:
                result = await getattr(self._primary, method)(*args)
            except DataUnavailable as error:
                self._down_until = self._clock() + self._retry_after_s
                if self._reason != str(error):
                    log.warning("DynamoDB unavailable (%s). Using the local files for %.0f s.", error, self._retry_after_s)
                self._reason = str(error)
            else:
                if self._reason is not None:
                    log.info("DynamoDB is available again.")
                    self._reason = None
                return result
        return await getattr(self._fallback, method)(*args)

    async def get_customer(self, mobile_number: str) -> dict | None:
        return await self._call("get_customer", mobile_number)

    async def list_areas(self) -> list[dict]:
        return await self._call("list_areas")

    async def get_area(self, area: str) -> dict | None:
        return await self._call("get_area", area)

    async def list_packs(self) -> list[dict]:
        return await self._call("list_packs")

    async def get_pack(self, pack_id: str) -> dict | None:
        return await self._call("get_pack", pack_id)

    async def list_centres(self, area: str) -> list[dict]:
        return await self._call("list_centres", area)

    async def list_open_tickets(self, customer_id: str) -> list[dict]:
        return await self._call("list_open_tickets", customer_id)

    async def list_tickets(self) -> list[dict]:
        return await self._call("list_tickets")

    async def create_ticket(self, ticket: dict) -> bool:
        return await self._call("create_ticket", ticket)

    async def save_call(self, record: dict) -> None:
        await self._call("save_call", record)

    async def list_calls(self) -> list[dict]:
        return await self._call("list_calls")

    async def clear_runtime(self) -> None:
        await self._call("clear_runtime")
        await self._fallback.clear_runtime()  # also wipe anything kept locally while DynamoDB was down

    def status(self) -> dict:
        active = self._clock() < self._down_until
        return {"backend": "files" if active else "dynamodb", "fallback_active": active, "reason": self._reason if active else None}
