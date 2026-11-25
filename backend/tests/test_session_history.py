"""Session history: a finished call is saved with its card, answers and suggestions, and shows up again."""
from copilot.services.pipeline import CallPipeline
from copilot.services.session_history import list_items


def record(call_id: str, started: float, **extra) -> dict:
    return {"call_id": call_id, "started_at": started, "turns": [{"id": 1, "speaker": "customer", "text": "hi", "ts": started}],
            **extra}


def test_list_is_newest_first_and_leaves_out_calls_without_a_transcript():
    items = list_items([record("a", 1), record("b", 3), {"call_id": "c", "started_at": 5, "turns": []}, record("d", 2)])
    assert [i["call_id"] for i in items] == ["b", "d", "a"]


def test_list_search_matches_name_number_ticket_and_subject():
    records = [
        record("a", 1, customer_name="Abdul Rahim", mobile_number="9500799295"),
        record("b", 2, summary={"subject": "Slow internet"}, ticket={"ticket_id": "TKT-10200"}),
    ]
    assert [i["call_id"] for i in list_items(records, search="rahim")] == ["a"]
    assert [i["call_id"] for i in list_items(records, search="95007")] == ["a"]
    assert [i["call_id"] for i in list_items(records, search="10200")] == ["b"]
    assert [i["call_id"] for i in list_items(records, search="slow")] == ["b"]


async def test_a_summarised_call_comes_back_with_its_customer_card(client, container):
    pipeline = CallPipeline(container)
    await pipeline.begin(call_sid=None, caller="+919500799295", source="typed")
    client.app.state.container = container
    session = pipeline.session
    session.add_turn("customer", "My internet is slow")
    await pipeline.finish()
    await container.summarizer.run(session)

    listed = client.get("/api/sessions").json()["sessions"]
    assert [s["call_id"] for s in listed] == [session.call_sid]
    assert listed[0]["customer_name"] == "Abdul Rahim"

    call = client.get(f"/api/sessions/{session.call_sid}").json()["call"]
    assert call["ended"] is True and call["turns"][0]["text"] == "My internet is slow"
    assert call["card"]["customer"]["name"] == "Abdul Rahim"
    assert call["card"]["registered_status"] is not None


def test_an_unknown_session_is_404(client):
    assert client.get("/api/sessions/nope").status_code == 404
