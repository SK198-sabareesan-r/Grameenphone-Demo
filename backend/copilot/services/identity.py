"""Customer identification.

The card can be loaded in two ways, and each has its own state:
* From the caller ID, when the call reaches the human agent (`preload`). The card is shown at once, marked
  "not confirmed yet", because a caller ID can be wrong or borrowed.
* From a mobile number confirmed with the customer (`confirm`): said on the call and picked up automatically,
  or typed by the agent. This loads the card, or confirms the one already shown.
"""
import asyncio
import logging
from datetime import datetime, timezone

from copilot.repo.base import Repo
from copilot.services.areas import NetworkAreas
from copilot.services.customers import customer_view, normalize_number
from copilot.services.hub import PanelHub
from copilot.services.sessions import CallSession

log = logging.getLogger(__name__)


class UnknownMobileNumber(Exception):
    """No customer has that mobile number."""


class IdentityService:
    def __init__(self, repo: Repo, areas: NetworkAreas, hub: PanelHub) -> None:
        self._repo, self._areas, self._hub = repo, areas, hub

    async def _load(self, session: CallSession, mobile_number: str, confirmed: bool) -> dict:
        """Read the customer, their open tickets and the network status of their registered area."""
        item = await self._repo.get_customer(normalize_number(mobile_number))
        if item is None:
            raise UnknownMobileNumber(mobile_number)
        tickets, status = await asyncio.gather(
            self._repo.list_open_tickets(item["id"]), self._areas.status(item["area"])
        )
        session.identify(customer_view(item, tickets, datetime.now(timezone.utc)), status, confirmed)
        session.ignored_number = None
        card = session.card()
        await self._hub.broadcast("card", card)
        return card

    async def confirm(self, session: CallSession, mobile_number: str) -> dict:
        """Load the card for a number the customer confirmed, or confirm the card that is already shown."""
        shown = session.customer
        if shown and normalize_number(shown["msisdn"]) == normalize_number(mobile_number):
            return await self.mark_confirmed(session)
        return await self._load(session, mobile_number, confirmed=True)

    async def mark_confirmed(self, session: CallSession) -> dict:
        """The customer confirmed the number of the card that was found from the caller ID."""
        session.identity_confirmed = True
        session.pending_confirm = False
        card = session.card()
        await self._hub.broadcast("card", card)
        return card

    async def preload(self, session: CallSession) -> dict | None:
        """Show the card for the caller ID, not yet confirmed. Does nothing when there is no caller ID, a card
        is already shown, or no customer has that number. A failure here must never disturb the call."""
        number = normalize_number(session.caller_number)
        if not number or session.customer is not None or number == session.ignored_number:
            return None
        try:
            return await self._load(session, number, confirmed=False)
        except UnknownMobileNumber:
            log.info("No customer for the caller ID of call %s", session.call_sid)
        except Exception:
            log.exception("Could not look up the caller ID of call %s", session.call_sid)
        return None

    async def clear(self, session: CallSession) -> None:
        """The agent picked the wrong customer: hide the card again."""
        if session.customer:
            session.ignored_number = normalize_number(session.customer["msisdn"])
        session.clear_identity()
        await self._hub.broadcast("card", None)
