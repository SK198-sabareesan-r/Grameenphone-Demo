"""Where the customer is right now. A network complaint prompts the agent to ask, and the answer is the filter."""

from pathlib import Path

from copilot.services.areas import NetworkAreas, normalize_text
from copilot.services.hub import PanelHub
from copilot.services.sessions import CallSession
from copilot.utils.helpers import load_json


class UnknownArea(Exception):
    """The location the agent entered matches no known area."""


class LocationService:
    def __init__(self, keywords_path: Path, areas: NetworkAreas, hub: PanelHub) -> None:
        words = load_json(keywords_path)
        self._subjects = [normalize_text(w) for w in words["subjects"]]
        self._problems = [normalize_text(w) for w in words["problems"]]
        self._standalone = [normalize_text(w) for w in words["standalone"]]
        self._areas, self._hub = areas, hub

    @staticmethod
    def _has(phrase: str, norm: str, padded: str) -> bool:
        # Latin words must match whole words ("net" must not match "internet"). Bengali is matched inside.
        return (f" {phrase} " in padded) if phrase.isascii() else (phrase in norm)

    def is_network_problem(self, text: str) -> bool:
        norm = normalize_text(text)
        if not norm:
            return False
        padded = f" {norm} "
        if any(self._has(p, norm, padded) for p in self._standalone):
            return True
        return any(self._has(s, norm, padded) for s in self._subjects) and any(
            self._has(p, norm, padded) for p in self._problems
        )

    async def on_customer_turn(self, session: CallSession, text: str) -> None:
        """Prompt the agent, once, to ask where the customer is. Nothing is shown when no problem is reported."""
        if session.location_prompt or session.current_location is not None:
            return
        if not self.is_network_problem(text):
            return
        session.location_prompt = True
        await self._hub.broadcast("location.prompt", {"show": True})

    async def set_current(self, session: CallSession, area_text: str) -> dict:
        """Check the network status at the location the customer gave the agent."""
        name = self._areas.resolve(area_text)
        if name is None:
            raise UnknownArea(area_text)
        status = self._areas.status(name)
        session.set_location(status)
        await self._hub.broadcast("location", {"current_location": status})
        return status
