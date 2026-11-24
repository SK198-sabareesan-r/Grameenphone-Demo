"""Tests for mobile-number identification, the current-location network check and tickets. No network, no AWS."""

import asyncio
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config.settings import Settings
from copilot.repo.files import FileRepo
from copilot.services.areas import NetworkAreas
from copilot.services.customers import is_plausible_number, normalize_number
from copilot.services.identity import IdentityService, UnknownMobileNumber
from copilot.services.location import LocationService, UnknownArea
from copilot.services.sessions import CallSession

MOCK = Path(__file__).resolve().parents[1] / "data" / "mock"
RUNTIME = Path(tempfile.mkdtemp())  # tests never touch the real data/runtime folder
RAHIM = "01200000001"


class FakeHub:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    async def broadcast(self, event: str, data: object) -> None:
        self.events.append((event, data))

    def of(self, name: str) -> list:
        return [d for e, d in self.events if e == name]


def build():
    settings = Settings(_env_file=None, demo_phone_map="+8801711111111:rahim")  # never read config/.env
    repo = FileRepo(settings, runtime_dir=RUNTIME)
    areas = NetworkAreas(repo)
    asyncio.run(areas.load())
    hub = FakeHub()
    identity = IdentityService(repo, areas, hub)
    location = LocationService(MOCK / "network_keywords.json", areas, hub)
    return repo, areas, hub, identity, location


def new_session() -> CallSession:
    return CallSession(call_sid="CA1", compliance={"verify_identity": "idle"})


# ---- mobile numbers ----

def test_numbers_are_compared_in_one_format():
    for typed in ["01200000001", "+8801200000001", "880 1200 000001", "1200000001", "01200-000001"]:
        assert normalize_number(typed) == RAHIM, typed
    assert is_plausible_number("+8801200000001") and not is_plausible_number("12345")


def test_customer_is_found_by_number_and_by_team_phone():
    repo, *_ = build()
    assert asyncio.run(repo.get_customer(RAHIM))["id"] == "rahim"
    assert asyncio.run(repo.get_customer("01711111111"))["id"] == "rahim"  # from DEMO_PHONE_MAP
    assert asyncio.run(repo.get_customer("01299999999")) is None


# ---- the card appears only after the agent confirms the number ----

def test_card_is_hidden_until_the_number_is_confirmed():
    _, _, hub, identity, _ = build()
    session = new_session()
    assert session.card() is None and session.customer is None

    card = asyncio.run(identity.confirm(session, "+8801200000001"))

    assert card["customer"]["id"] == "rahim" and card["customer"]["msisdn"] == RAHIM
    assert "mobile_number" not in card["customer"] and "source" not in card["customer"]  # storage-only fields
    assert card["registered_status"]["area"] == "Mirpur"
    assert card["registered_status"]["status"] == "maintenance"  # location status comes with the card
    assert hub.of("card") == [card]


def test_the_card_lists_open_tickets_from_the_tickets_table_oldest_first():
    _, _, _, identity, _ = build()
    card = asyncio.run(identity.confirm(new_session(), "01200000003"))  # Karim
    tickets = card["customer"]["open_tickets"]
    assert [t["id"] for t in tickets] == ["TKT-10101", "TKT-10144", "TKT-10190"]
    assert [t["age"] for t in tickets] == ["9 days", "5 days", "2 days"]
    assert all(t["topic"] == "Slow internet" for t in tickets)


def test_unknown_number_loads_nothing():
    _, _, hub, identity, _ = build()
    session = new_session()
    with pytest.raises(UnknownMobileNumber):
        asyncio.run(identity.confirm(session, "01299999999"))
    assert session.card() is None and hub.events == []


def test_wrong_customer_can_be_cleared():
    _, _, hub, identity, _ = build()
    session = new_session()
    asyncio.run(identity.confirm(session, RAHIM))
    asyncio.run(identity.clear(session))
    assert session.card() is None and session.registered_status is None
    assert hub.events[-1] == ("card", None)


# ---- areas ----

def test_area_names_resolve_in_english_bangla_and_inside_a_sentence():
    _, areas, *_ = build()
    assert areas.resolve("Gazipur") == "Gazipur"
    assert areas.resolve("  gazipur ") == "Gazipur"
    assert areas.resolve("ami ekhon mirpur 10 e achi") == "Mirpur"
    assert areas.resolve("মিরপুর") == "Mirpur"
    assert areas.resolve("আমি এখন মিরপুরে আছি") == "Mirpur"  # Bangla suffix
    assert areas.resolve("ctg") == "Chattogram"
    assert areas.resolve("Paris") is None and areas.resolve("") is None
    assert "Gazipur" in areas.names() and len(areas.names()) == 7


def test_area_without_a_notice_does_not_claim_the_network_is_fine():
    _, areas, *_ = build()
    status = asyncio.run(areas.status("Dhanmondi"))
    assert status["status"] == "no_notice" and "No network notice" in status["message"]
    assert asyncio.run(areas.status("Gazipur"))["status"] == "outage"


def test_a_status_is_read_fresh_so_an_edited_notice_shows_on_the_next_check():
    repo, areas, *_ = build()
    assert asyncio.run(areas.status("Dhanmondi"))["status"] == "no_notice"
    repo._areas["Dhanmondi"]["notice"] = {"status": "outage", "message": "Fibre cut", "until": "9 PM"}
    assert asyncio.run(areas.status("Dhanmondi"))["status"] == "outage"


# ---- location prompt and filter ----

def test_network_problem_prompts_for_the_location_once():
    _, _, hub, _, location = build()
    session = new_session()

    asyncio.run(location.on_customer_turn(session, "Bhai, amar net 2 din dhore kaj korche na."))
    asyncio.run(location.on_customer_turn(session, "Network khub slow, problem hocche"))

    assert session.location_prompt is True
    assert hub.of("location.prompt") == [{"show": True}]  # only once


def test_other_sentences_do_not_prompt():
    _, _, hub, _, location = build()
    session = new_session()
    for text in ["Assalamu alaikum", "Amar internet pack er ki obostha?", "Amar bill e 100 taka extra keno?",
                 "thik ache chalu kore den", "I recharged 200 taka but it is not showing"]:
        asyncio.run(location.on_customer_turn(session, text))
    assert session.location_prompt is False and hub.events == []


def test_bangla_network_complaint_prompts():
    _, _, _, _, location = build()
    assert location.is_network_problem("আমার নেটওয়ার্ক কাজ করছে না")
    assert location.is_network_problem("signal nai")
    assert not location.is_network_problem("আমার বিল কত")


def test_checking_a_location_uses_that_area_and_clears_the_prompt():
    _, _, hub, identity, location = build()
    session = new_session()
    asyncio.run(identity.confirm(session, RAHIM))  # registered in Mirpur
    asyncio.run(location.on_customer_turn(session, "amar net kaj korche na"))

    status = asyncio.run(location.set_current(session, "ami Gazipur e achi"))

    assert status["area"] == "Gazipur" and status["status"] == "outage"
    assert session.location_prompt is False
    assert session.registered_status["area"] == "Mirpur"  # the registered status is kept
    assert session.network_view()["current_location"]["area"] == "Gazipur"
    assert hub.of("location")[0]["current_location"]["area"] == "Gazipur"
    asyncio.run(location.on_customer_turn(session, "net slow"))
    assert len(hub.of("location.prompt")) == 1  # no second prompt after a location was checked


def test_unknown_location_is_rejected():
    _, _, _, _, location = build()
    session = new_session()
    with pytest.raises(UnknownArea):
        asyncio.run(location.set_current(session, "Paris"))
    assert session.current_location is None


# ---- through the real API (the shared signed-in `client` fixture; Bedrock is faked) ----

def test_api_flow_number_then_card_then_current_location(client):
    # The call starts. The customer reports the problem. No card yet, but the agent is prompted.
    assert client.post("/api/identity", json={"mobile_number": RAHIM}).status_code == 409  # no call yet
    client.post("/api/transcript", json={"speaker": "customer", "text": "Bhai, amar net kaj korche na"})
    call = client.get("/api/calls/current").json()["call"]
    assert call["card"] is None and call["location_prompt"] is True

    # Wrong and unusable numbers load nothing.
    assert client.post("/api/identity", json={"mobile_number": "01299999999"}).status_code == 404
    assert client.post("/api/identity", json={"mobile_number": "12345678"}).status_code == 422
    assert client.get("/api/identity").json()["identified"] is False

    # The agent confirms the number: the card arrives with the registered location status.
    body = client.post("/api/identity", json={"mobile_number": "+8801200000001"}).json()
    assert body["card"]["customer"]["name"] == "Rahim Uddin"
    assert body["card"]["registered_status"]["area"] == "Mirpur"

    # The customer is somewhere else right now: that location is checked.
    assert client.put("/api/location", json={"area": "Paris"}).status_code == 404
    result = client.put("/api/location", json={"area": "Gazipur"}).json()
    assert result["current_location"]["status"] == "outage"
    assert result["registered_status"]["area"] == "Mirpur"
    call = client.get("/api/calls/current").json()["call"]
    assert call["current_location"]["area"] == "Gazipur" and call["location_prompt"] is False

    # Wrong customer: the card goes away again.
    client.delete("/api/identity")
    assert client.get("/api/calls/current").json()["call"]["card"] is None
    assert "Gazipur" in client.get("/api/locations").json()["locations"]


def test_health_says_where_the_data_comes_from(client):
    data = client.get("/api/health").json()["data"]
    assert data == {"backend": "files", "fallback_active": False, "reason": None}


def test_tickets_are_numbered_saved_shown_on_the_card_and_cleared_by_reset(runtime_dir, users_file, fake_bedrock):
    from tests.conftest import make_settings, open_client

    with open_client(make_settings(runtime_dir, demo_controls_enabled=True), fake_bedrock) as client:  # reset is a demo control
        _tickets_flow(client)


def _tickets_flow(client):
    client.post("/api/transcript", json={"speaker": "customer", "text": "Amar net slow"})
    draft = {"customer_id": "karim", "category": "Network", "subject": "Slow internet again", "problem": "Still slow"}

    first = client.post("/api/tickets", json=draft).json()
    second = client.post("/api/tickets", json=draft).json()
    assert (first["ticket_id"], second["ticket_id"]) == ("TKT-10234", "TKT-10235")
    assert first["status"] == "open" and first["source"] == "app" and first["customer_id"] == "karim"

    card = client.post("/api/identity", json={"mobile_number": "01200000003"}).json()["card"]
    assert [t["id"] for t in card["customer"]["open_tickets"]][-2:] == ["TKT-10234", "TKT-10235"]
    assert len(client.get("/api/tickets").json()["tickets"]) == 5  # 3 seeded + 2 new

    nobody = client.post("/api/tickets", json={"category": "Other", "subject": "No customer"}).json()
    assert nobody["customer_id"] == "unidentified" and nobody["ticket_id"] == "TKT-10236"

    client.post("/api/data/reset")
    remaining = client.get("/api/tickets").json()["tickets"]
    assert [t["ticket_id"] for t in remaining] == ["TKT-10101", "TKT-10144", "TKT-10190"]  # seeded ones stay


def test_removed_endpoints_stay_removed(client):
    assert client.get("/api/customers").status_code in (404, 405)
    assert client.get("/api/customers/rahim").status_code in (404, 405)
