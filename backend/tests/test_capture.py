"""Auto-capture of the mobile number and the location from the transcript (fake Bedrock, local files)."""
import pytest

from copilot.services.pipeline import CallPipeline, ensure_pipeline
from copilot.services.spoken_number import mobile_numbers
from tests.conftest import settle

RAHIM = "01200000001"


# ---- finding a number in speech ----
@pytest.mark.parametrize(
    "said",
    [
        "amar number 01200000001",
        "০১২০০০০০০০১",
        "0120 000 0001",
        "zero one two zero zero zero zero zero zero zero one",
        "shunno ek dui shunno shunno shunno shunno shunno shunno shunno ek",
        "+880 1200 000001",
        "01200-000001, ji",
    ],
)
def test_a_number_is_found_however_it_was_said(said):
    assert mobile_numbers(said) == [RAHIM]


@pytest.mark.parametrize(
    "said",
    ["", "amar net 2 din dhore kaj korche na", "recharge 200 taka", "012000000", "0120000000012", "ek dui tin char"],
)
def test_other_speech_has_no_number(said):
    assert mobile_numbers(said) == []


DEMO = "9500799295"  # the 10-digit number that calls in the demo


@pytest.mark.parametrize(
    "said",
    [
        "my number is 9500799295",
        "9500 799 295",
        "+91 95007 99295",
        "919500799295",
        "09500799295",
        "nine five zero zero seven nine nine two nine five",
        "nine five double zero seven double nine two nine five",
        "noy pach shunno shunno sat noy noy dui noy pach",
        "নয় পাঁচ জিরো জিরো সাত ডাবল নয় দুই নয় পাঁচ",
        "৯৫০০৭৯৯২৯৫",
    ],
)
def test_the_ten_digit_demo_number_is_found_however_it_was_said(said):
    assert mobile_numbers(said) == [DEMO]


def test_a_ten_digit_number_is_not_cut_out_of_a_longer_number():
    assert mobile_numbers("95007992951") == []
    assert mobile_numbers("1234567890") == []  # does not start with 6 to 9


def test_double_and_triple_only_repeat_the_next_digit():
    assert mobile_numbers("double charge") == []
    assert mobile_numbers("triple one") == []


def test_the_last_number_said_comes_last():
    assert mobile_numbers("01200000002 na, 01200000001") == ["01200000002", "01200000001"]


# ---- the customer card ----
async def say(pipeline, speaker, text):
    await pipeline.ingest(speaker, "final", text)
    await settle(timeout=0.3)


async def test_the_card_loads_when_the_customer_says_the_number(container, events):
    pipeline = await ensure_pipeline(container)
    await say(pipeline, "customer", "Amar number 01200000001")
    assert pipeline.session.customer["id"] == "rahim"
    assert events.of("card")[-1]["customer"]["name"].startswith("Rahim")
    assert events.of("identity.heard")[-1] == {"number": RAHIM, "found": True, "speaker": "customer"}


async def test_the_ten_digit_demo_number_loads_its_card(container):
    pipeline = await ensure_pipeline(container)
    await say(pipeline, "customer", "Amar number nine five zero zero seven nine nine two nine five")
    customer = pipeline.session.customer
    assert customer["id"] == "abdul-rahim" and customer["name"] == "Abdul Rahim" and customer["msisdn"] == DEMO
    assert pipeline.session.heard == {"number": DEMO, "found": True, "speaker": "customer"}
    assert pipeline.session.registered_status["status"] == "maintenance"
    assert [t["id"] for t in customer["open_tickets"]] == ["TKT-10150", "TKT-10187"]

async def test_an_unknown_number_is_reported_and_the_next_one_is_picked_up(container, events):
    pipeline = await ensure_pipeline(container)
    await say(pipeline, "customer", "01299999999")
    assert pipeline.session.customer is None
    assert events.of("identity.heard")[-1]["found"] is False
    await say(pipeline, "human_agent", "Ami number ta abar bolchi: 01299999999")  # same wrong number: no repeat event
    assert len(events.of("identity.heard")) == 1
    await say(pipeline, "customer", "Na na, 01200000001")
    assert pipeline.session.customer["id"] == "rahim"


async def test_speech_from_an_unverified_speaker_loads_nothing(container, events):
    pipeline = await ensure_pipeline(container)
    await say(pipeline, "unknown", "01200000001")
    assert pipeline.session.customer is None and not events.of("identity.heard")


async def test_a_rejected_customer_is_not_loaded_again_until_another_number_is_heard(container):
    pipeline = await ensure_pipeline(container)
    await say(pipeline, "customer", "01200000001")
    await container.identity.clear(pipeline.session)
    await say(pipeline, "human_agent", "01200000001 ta ki thik?")
    assert pipeline.session.customer is None
    await say(pipeline, "customer", "01200000002")
    assert pipeline.session.customer["id"] == "nusrat"


# ---- the location ----
async def network_problem_call(container, number=RAHIM):
    pipeline = await ensure_pipeline(container)
    await say(pipeline, "customer", "Bhai, amar net kaj korche na.")
    await say(pipeline, "customer", number)
    return pipeline


async def test_the_card_is_loaded_and_the_location_prompt_stays_until_the_place_is_confirmed(container, events):
    pipeline = await network_problem_call(container)
    assert pipeline.session.customer["area"] == "Mirpur" and pipeline.session.location_prompt
    assert pipeline.session.current_location is None


async def test_the_customer_naming_a_place_sets_the_location_from_the_table(container, events):
    pipeline = await network_problem_call(container)
    await say(pipeline, "human_agent", "Apni ekhon kothay achen?")
    await say(pipeline, "customer", "Ami Uttara te achi")
    here = pipeline.session.current_location
    assert here["area"] == "Uttara" and here["status"] == "scheduled"
    assert events.of("location")[-1]["current_location"]["area"] == "Uttara"
    assert not pipeline.session.location_prompt


async def test_a_yes_to_the_area_the_agent_named_confirms_it(container, events):
    pipeline = await network_problem_call(container)
    await say(pipeline, "human_agent", "Apni ki Mirpur e achen?")
    assert pipeline.session.pending_area == "Mirpur" and pipeline.session.current_location is None
    await say(pipeline, "customer", "Ji, thik ache")
    assert pipeline.session.current_location["area"] == "Mirpur"
    assert pipeline.session.current_location["status"] == "maintenance"


async def test_a_no_cancels_the_area_the_agent_named(container):
    pipeline = await network_problem_call(container)
    await say(pipeline, "human_agent", "Apni ki Mirpur e achen?")
    await say(pipeline, "customer", "Na")
    await say(pipeline, "customer", "Ji")
    assert pipeline.session.current_location is None


async def test_a_place_name_means_nothing_before_a_network_problem_is_reported(container):
    pipeline = await ensure_pipeline(container)
    await say(pipeline, "customer", "Ami Uttara GP Centre e jabo")
    assert pipeline.session.current_location is None


async def test_the_answer_is_looked_up_again_when_the_location_is_confirmed(container, fake_bedrock):
    pipeline = await network_problem_call(container)
    await say(pipeline, "customer", "Uttara te")

    def lookup_with_new_location() -> bool:  # the live-lookup prompt carries the network status of both places
        return any("Pack catalog" in c["user"] and '"area": "Uttara"' in c["user"] for c in fake_bedrock.calls)

    await settle(lookup_with_new_location, timeout=2.0)
    assert lookup_with_new_location()

# ---- the card from the caller ID, shown "not confirmed" until the customer confirms the number ----
async def call_from(container, caller="+919500799295"):
    pipeline = CallPipeline(container)
    await pipeline.begin(call_sid=None, caller=caller, source="typed")
    return pipeline


async def test_the_card_appears_from_the_caller_id_and_is_not_confirmed(container, events):
    pipeline = await call_from(container)
    session = pipeline.session
    assert session.customer["name"] == "Abdul Rahim" and session.identity_confirmed is False
    assert events.of("card")[-1]["confirmed"] is False
    assert session.card()["registered_status"]["status"] == "maintenance"  # the notice is there from the start


async def test_an_unknown_caller_id_shows_no_card(container, events):
    pipeline = await call_from(container, "+911234567890")
    assert pipeline.session.customer is None and not events.of("card")
    pipeline = await call_from(container, "")
    assert pipeline.session.customer is None


async def test_the_customer_saying_the_number_confirms_the_card(container, events):
    pipeline = await call_from(container)
    await say(pipeline, "customer", "Amar number 9500799295")
    assert pipeline.session.identity_confirmed is True and pipeline.session.customer["id"] == "abdul-rahim"
    assert events.of("card")[-1]["confirmed"] is True
    assert pipeline.session.heard == {"number": DEMO, "found": True, "speaker": "customer"}


async def test_the_agent_reading_the_number_waits_for_the_customers_yes(container):
    pipeline = await call_from(container)
    await say(pipeline, "human_agent", "Apnar number ki 9500799295?")
    assert pipeline.session.identity_confirmed is False and pipeline.session.pending_confirm is True
    await say(pipeline, "customer", "Ji, thik ache")
    assert pipeline.session.identity_confirmed is True and pipeline.session.pending_confirm is False


async def test_a_no_to_the_number_the_agent_read_leaves_the_card_unconfirmed(container):
    pipeline = await call_from(container)
    await say(pipeline, "human_agent", "Apnar number ki 9500799295?")
    await say(pipeline, "customer", "Na")
    await say(pipeline, "customer", "Ji")
    assert pipeline.session.identity_confirmed is False and pipeline.session.pending_confirm is False


async def test_a_yes_means_nothing_when_the_agent_has_not_read_a_number(container):
    pipeline = await call_from(container)
    await say(pipeline, "customer", "Ji")
    assert pipeline.session.identity_confirmed is False


async def test_a_different_number_replaces_the_card_from_the_caller_id(container):
    pipeline = await call_from(container)
    await say(pipeline, "customer", "Ami onno phone theke call korchi, amar number 01200000002")
    assert pipeline.session.customer["id"] == "nusrat" and pipeline.session.identity_confirmed is True


async def test_typing_the_number_confirms_the_card(container):
    pipeline = await call_from(container)
    await container.identity.confirm(pipeline.session, "9500799295")
    assert pipeline.session.identity_confirmed is True
    assert pipeline.session.to_record()["identity_confirmed"] is True


async def test_not_this_customer_hides_the_card_and_it_is_not_loaded_again(container):
    pipeline = await call_from(container)
    await container.identity.clear(pipeline.session)
    assert pipeline.session.customer is None
    await pipeline.set_status("with_human", "human_agent")  # a later hand-off does not bring the same card back
    assert pipeline.session.customer is None


async def test_a_voice_call_is_looked_up_when_it_reaches_the_human_agent(container, events):
    pipeline = CallPipeline(container)
    await pipeline.begin(call_sid="CA-voice-1", caller="+919500799295", source="voice", agent_mode="ai_agent")
    assert pipeline.session.customer is None  # the AI agent is still handling the call
    await pipeline.set_status("with_human", "human_agent")
    assert pipeline.session.customer["id"] == "abdul-rahim" and pipeline.session.identity_confirmed is False
    assert events.of("card")[-1]["confirmed"] is False


async def test_the_start_call_route_shows_the_unconfirmed_card(client):
    assert client.post("/api/calls", json={"caller_number": "12"}).status_code == 422
    response = client.post("/api/calls", json={"caller_number": "+919500799295"})
    assert response.status_code == 201
    card = response.json()["call"]["card"]
    assert card["customer"]["name"] == "Abdul Rahim" and card["confirmed"] is False
    assert client.post("/api/calls", json={"caller_number": ""}).status_code == 409  # one call at a time