"""Customer identification. Nothing about the customer is loaded until the agent confirms the mobile number."""

from copilot.services.areas import NetworkAreas
from copilot.services.customers import CustomerDirectory
from copilot.services.hub import PanelHub
from copilot.services.sessions import CallSession


class UnknownMobileNumber(Exception):
    """No customer has that mobile number."""


class IdentityService:
    def __init__(self, customers: CustomerDirectory, areas: NetworkAreas, hub: PanelHub) -> None:
        self._customers, self._areas, self._hub = customers, areas, hub

    async def confirm(self, session: CallSession, mobile_number: str) -> dict:
        """Load the customer card for a number the agent has confirmed with the customer.

        The card includes the network status at the location registered with that number.
        """
        customer = self._customers.find_by_number(mobile_number)
        if customer is None:
            raise UnknownMobileNumber(mobile_number)
        session.identify(customer, self._areas.status(customer["area"]))
        card = session.card()
        await self._hub.broadcast("card", card)
        return card

    async def clear(self, session: CallSession) -> None:
        """The agent picked the wrong customer: hide the card again."""
        session.clear_identity()
        await self._hub.broadcast("card", None)
