"""A suggestion that appears after the agent has already said it is shown as used."""
import asyncio
from pathlib import Path

from copilot.services.nba import NbaCatalog
from copilot.services.sessions import CallSession

RULES = Path(__file__).resolve().parents[1] / "data" / "mock" / "nba_rules.json"


class NoRepo:
    async def get_pack(self, pack_id):
        return None


def show(session: CallSession, suggestion_id: str) -> dict:
    asyncio.run(NbaCatalog(RULES, NoRepo()).show(session, suggestion_id))
    return session.suggestions[suggestion_id]


def test_offer_already_said_by_the_agent_is_marked_used():
    session = CallSession(call_sid="CA1", customer=None, compliance={})
    session.add_turn("human_agent", "Apni boro pack ta nite paren.")
    assert show(session, "nba-bigger-pack")["used"] is True


def test_offer_the_customer_said_is_not_counted():
    session = CallSession(call_sid="CA1", customer=None, compliance={})
    session.add_turn("customer", "boro pack ta nite chai")
    assert show(session, "nba-bigger-pack")["used"] is False


def test_old_lines_are_not_checked():
    session = CallSession(call_sid="CA1", customer=None, compliance={})
    session.add_turn("human_agent", "boro pack ta nite paren.")
    for _ in range(5):
        session.add_turn("customer", "ok")
    assert show(session, "nba-bigger-pack")["used"] is False
