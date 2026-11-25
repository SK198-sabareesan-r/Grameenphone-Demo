"""Reads the call as it happens and fills in what the agent would otherwise type.

Two things are picked up from the live transcript:
* The customer's mobile number. When one is heard (said by the customer or read back by the agent) the
  customer card is loaded from the customers table. If no customer has that number, the panel says so and
  keeps listening, so the agent only has to ask again by voice.
* The customer's location. After a network problem is reported, the panel asks the agent to confirm the
  area on the card. The customer's reply sets the location: a place name, or "yes" after the agent named
  the area. The status for that area is then read from the network-areas table.
Each capture re-runs the lookup for the question it affects, so the answer on screen is updated.
"""
import logging
from pathlib import Path

from copilot.models.participants import AI_AGENT, CUSTOMER, HUMAN_AGENT, UNKNOWN
from copilot.services.areas import NetworkAreas, normalize_text
from copilot.services.assist import LiveAssist
from copilot.services.customers import normalize_number
from copilot.services.hub import PanelHub
from copilot.services.identity import IdentityService, UnknownMobileNumber
from copilot.services.location import LocationService
from copilot.services.sessions import CallSession, Turn
from copilot.services.spoken_number import mobile_numbers
from copilot.utils.helpers import load_json

log = logging.getLogger(__name__)
MAX_REPLY_WORDS = 6  # a longer reply is a new question, not a yes or no


class AutoCapture:
    def __init__(
        self,
        identity: IdentityService,
        location: LocationService,
        areas: NetworkAreas,
        assist: LiveAssist,
        hub: PanelHub,
        keywords_path: Path,
    ) -> None:
        self._identity, self._location, self._areas = identity, location, areas
        self._assist, self._hub = assist, hub
        words = load_json(keywords_path)
        self._affirm = {normalize_text(w) for w in words["affirm"]}
        self._negate = {normalize_text(w) for w in words["negate"]}

    async def on_turn(self, session: CallSession, turn: Turn) -> None:
        """Look at one finished line. Never raises: a failed capture must not stop the transcript."""
        if turn.speaker == UNKNOWN:  # speech from an unverified speaker is never acted on
            return
        try:
            await self._number(session, turn)
            await self._place(session, turn)
        except Exception:
            log.exception("Auto-capture failed for call %s", session.call_sid)

    # ---- mobile number ----
    async def _number(self, session: CallSession, turn: Turn) -> None:
        heard = mobile_numbers(turn.text)
        if not heard:
            return
        number = normalize_number(heard[-1])  # the last one said is the one being confirmed
        if number == session.ignored_number or self._already_handled(session, number):
            return
        previous = (session.customer or {}).get("id")
        try:
            card = await self._identity.confirm(session, number)
            found = True
        except UnknownMobileNumber:
            card, found = None, False
        session.heard = {"number": number, "found": found, "speaker": turn.speaker}
        await self._hub.broadcast("identity.heard", session.heard)
        if found:
            if previous and previous != card["customer"]["id"]:  # a different customer: forget the old place
                session.current_location = session.pending_area = None
            await self._refresh(session)

    @staticmethod
    def _already_handled(session: CallSession, number: str) -> bool:
        if session.customer and normalize_number(session.customer["msisdn"]) == number:
            return True
        return bool(session.heard and session.heard["number"] == number and not session.heard["found"])

    # ---- location ----
    async def _place(self, session: CallSession, turn: Turn) -> None:
        if not (session.location_prompt or session.current_location is not None):
            return  # no network problem has been reported, so a place name is just conversation
        await self._areas.ensure_loaded()
        area = self._areas.resolve(turn.text)
        if turn.speaker in (HUMAN_AGENT, AI_AGENT):
            if area:
                session.pending_area = area  # the agent is asking "are you in <area>?"
            return
        if turn.speaker != CUSTOMER:
            return
        if area is None and session.pending_area and self._says(turn.text, self._affirm):
            area = session.pending_area
        elif area is None and self._says(turn.text, self._negate):
            session.pending_area = None
        if area is None:
            return
        session.pending_area = None
        if area != (session.current_location or {}).get("area"):
            await self._location.set_current(session, area)
            await self._refresh(session)

    def _says(self, text: str, words: set[str]) -> bool:
        """A short reply that starts with one of the words (or is one), for example "ji, thik ache"."""
        tokens = normalize_text(text).split()
        if not tokens or len(tokens) > MAX_REPLY_WORDS:
            return False
        return tokens[0] in words or normalize_text(text) in words

    # ---- update the answer ----
    async def _refresh(self, session: CallSession) -> None:
        """Look the question up again, now that the card or the location is known."""
        turn = next(
            (t for t in reversed(session.turns) if t.speaker == CUSTOMER and self._location.is_network_problem(t.text)),
            None,
        )
        if turn is None and session.answers:
            turn = next((t for t in session.turns if t.id == session.answers[-1]["turn_id"]), None)
        if turn is not None:
            self._assist.schedule(session, turn)
