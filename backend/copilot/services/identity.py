"""Customer identification. Nothing about the customer is loaded until the agent confirms the mobile number."""

import asyncio
from datetime import datetime, timezone

from copilot.repo.base import Repo
from copilot.services.areas import NetworkAreas
from copilot.services.customers import customer_view, normalize_number
from copilot.services.hub import PanelHub
from copilot.services.sessions import CallSession


class UnknownMobileNumber(Exception):
    """No customer has that mobile number."""


class IdentityService:
    def __init__(self, repo: Repo, areas: NetworkAreas, hub: PanelHub) -> None:
        self._repo, self._areas, self._hub = repo, areas, hub

    async def confirm(self, session: CallSession, mobile_number: str) -> dict:
        """Load the customer card for a number the agent has confirmed with the customer.

        The card has the customer, their open tickets, and the network status at the location registered
        with that number.
        """
        item = await self._repo.get_customer(normalize_number(mobile_number))
        if item is None:
            raise UnknownMobileNumber(mobile_number)
        tickets, status = await asyncio.gather(
            self._repo.list_open_tickets(item["id"]), self._areas.status(item["area"])
        )
        session.identify(customer_view(item, tickets, datetime.now(timezone.utc)), status)
        card = session.card()
        await self._hub.broadcast("card", card)
        return card

    async def clear(self, session: CallSession) -> None:
        """The agent picked the wrong customer: hide the card again."""
        session.clear_identity()
        await self._hub.broadcast("card", None)
