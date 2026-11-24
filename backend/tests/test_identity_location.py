"""Tests for mobile-number identification and the current-location network check. No network, no AWS."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config.settings import Settings
from copilot.services.areas import NetworkAreas
from copilot.services.customers import CustomerDirectory, is_plausible_number, normalize_number
from copilot.services.identity import IdentityService, UnknownMobileNumber
from copilot.services.location import LocationService, UnknownArea
from copilot.services.sessions import CallSession

MOCK = Path(__file__).resolve().parents[1] / "data" / "mock"
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
    customers = CustomerDirectory(settings)
    areas = NetworkAreas(MOCK / "network_areas.json")
    hub = FakeHub()
    identity = IdentityService(customers, areas, hub)
    location = LocationService(MOCK / "network_keywords.json", areas, hub)
    return customers, areas, hub, identity, location


def new_session() -> CallSession:
    return CallSession(call_sid="CA1", compliance={"verify_identity": "idle"})


# ---- mobile numbers ----

def test_numbers_are_compared_in_one_format():
    for typed in ["01200000001", "+8801200000001", "880 1200 000001", "1200000001", "01200-000001"]:
        assert normalize_number(typed) == RAHIM, typed
    assert is_plausible_number("+8801200000001") and not is_plausible_number("12345")


def test_customer_is_found_by_number_and_by_team_phone():
    customers, *_ = build()
    assert customers.find_by_number("+88 01200 000001")["id"] == "rahim"
    assert customers.find_by_number("01711111111")["id"] == "rahim"  # from DEMO_PHONE_MAP
    assert customers.find_by_number("01299999999") is None


# ---- the card appears only after the agent confirms the number ----

def test_card_is_hidden_until_the_number_is_confirmed():
    _, _, hub, identity, _ = build()
    session = new_session()
    assert session.card() is None and session.customer is None

    card = asyncio.run(identity.confirm(session, "+8801200000001"))

    assert card["customer"]["id"] == "rahim"
    assert card["registered_status"]["area"] == "Mirpur"
    assert card["registered_status"]["status"] == "maintenance"  # location status comes with the card
    assert hub.of("card") == [card]


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


def test_area_without_a_notice_does_not_claim_the_network_is_fine():
    _, areas, *_ = build()
    status = areas.status("Dhanmondi")
    assert status["status"] == "no_notice" and "No network notice" in status["message"]
    assert areas.status("Gazipur")["status"] == "outage"


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


def test_removed_endpoints_stay_removed(client):
    assert client.get("/api/customers").status_code in (404, 405)
    assert client.get("/api/customers/rahim").status_code in (404, 405)
