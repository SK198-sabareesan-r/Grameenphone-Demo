"""Typed-line calls through CallPipeline: the same path live speech uses (no audio, no providers)."""

import json

from copilot.services.pipeline import CallPipeline, ensure_pipeline
from tests.conftest import FAST_ARN, SUMMARY_ARN, settle


async def test_a_typed_line_starts_a_local_text_only_call(container, events):
    pipeline = await ensure_pipeline(container)
    session = pipeline.session
    assert session.call_sid.startswith("local-")
    assert session.customer["id"] == "rahim"  # DEMO_DEFAULT_CUSTOMER_ID
    assert session.notice is not None  # Rahim's area has a network notice
    assert pipeline.source == "typed"  # text-only: the Copilot opens no audio or speech socket
    assert container.registry.current is session and container.registry.pipeline is pipeline
    assert events.names() == ["call.started"]


async def test_ensure_pipeline_reuses_the_active_call_and_starts_a_new_one_after_it_ends(container, events):
    first = await ensure_pipeline(container)
    assert await ensure_pipeline(container) is first
    await first.finish()
    second = await ensure_pipeline(container)
    assert second is not first and second.session.call_sid != first.session.call_sid
    await settle()


async def test_final_lines_are_stored_and_partials_are_only_broadcast(container, events):
    pipeline = await ensure_pipeline(container)
    await pipeline.ingest("customer", "partial", "amar net")
    await pipeline.ingest("customer", "final", "  amar net kaj korche na  ")
    await pipeline.ingest("agent", "final", "Ami dekhchi.")
    await settle()
    turns = pipeline.session.turns
    assert [(t.id, t.speaker, t.text) for t in turns] == [
        (1, "customer", "amar net kaj korche na"), (2, "human_agent", "Ami dekhchi."),  # M2: "agent" -> role
    ]
    transcripts = events.of("transcript")
    assert transcripts[0] == {"speaker": "customer", "kind": "partial", "text": "amar net"}
    assert transcripts[1]["kind"] == "final" and transcripts[1]["text"] == "amar net kaj korche na"


async def test_empty_text_is_ignored(container, events):
    pipeline = await ensure_pipeline(container)
    await pipeline.ingest("customer", "final", "   ")
    assert pipeline.session.turns == []
    assert events.of("transcript") == []


async def test_the_customer_card_appears_once_on_the_first_customer_line(container, events):
    pipeline = await ensure_pipeline(container)
    await pipeline.ingest("agent", "final", "Assalamu alaikum.")
    assert events.of("card") == []  # an agent line does not reveal the card
    await pipeline.ingest("customer", "final", "Bhai, net kaj korche na.")
    await pipeline.ingest("customer", "final", "2 din dhore.")
    await settle()
    cards = events.of("card")
    assert len(cards) == 1 and cards[0]["customer"]["id"] == "rahim"
    assert pipeline.session.card() == cards[0]


async def test_each_final_line_schedules_analysis_with_the_fast_model(container, fake_bedrock):
    pipeline = await ensure_pipeline(container)
    await pipeline.ingest("customer", "final", "Net kaj korche na.")
    await settle(lambda: len(fake_bedrock.calls_for(FAST_ARN)) >= 1)
    prompt = fake_bedrock.calls_for(FAST_ARN)[0]["user"]
    assert "CUSTOMER: Net kaj korche na." in prompt


async def test_finish_is_idempotent_blocks_new_lines_and_saves_the_record(container, events, runtime_dir, fake_bedrock):
    pipeline = await ensure_pipeline(container)
    await pipeline.ingest("customer", "final", "Net kaj korche na.")
    await pipeline.finish()
    await pipeline.finish()
    await pipeline.ingest("customer", "final", "after the end")
    await settle(lambda: pipeline.session.summary is not None)
    assert events.names().count("call.ended") == 1
    assert [t.text for t in pipeline.session.turns] == ["Net kaj korche na."]
    assert len(fake_bedrock.calls_for(SUMMARY_ARN)) == 1
    saved = runtime_dir / "calls" / f"{pipeline.session.call_sid}.json"
    record = json.loads(saved.read_text(encoding="utf-8"))
    assert record["customer_id"] == "rahim" and record["summary"]["category"] == "Network"


async def test_a_new_pipeline_replaces_the_call_on_screen(container, events):
    first = CallPipeline(container)
    await first.begin(call_sid="call-1", caller="")
    second = CallPipeline(container)
    await second.begin(call_sid="call-2", caller="")
    assert container.registry.current.call_sid == "call-2"
    assert container.registry.get("call-1") is first.session  # older calls stay reachable by id


async def test_phone_map_and_ui_override_pick_the_customer(runtime_dir, fake_bedrock):
    from copilot.container import build_container
    from tests.conftest import make_settings

    c = build_container(make_settings(runtime_dir, demo_phone_map="+8801700000001:nusrat"), bedrock=fake_bedrock)
    assert c.customers.resolve("+880 1700-000001")["id"] == "nusrat"
    assert c.customers.resolve("+8801999999999")["id"] == "rahim"  # unmapped callers get the default
    c.customers.override_id = "karim"
    assert c.customers.resolve("+8801700000001")["id"] == "karim"


def test_typed_line_api_validates_speaker_and_length(client):
    assert client.post("/api/transcript", json={"speaker": "bot", "text": "hi"}).status_code == 422
    assert client.post("/api/transcript", json={"speaker": "agent", "text": ""}).status_code == 422
    assert client.post("/api/transcript", json={"speaker": "agent", "text": "x" * 1001}).status_code == 422
    ok = client.post("/api/transcript", json={"speaker": "agent", "text": "Hello"})
    assert ok.status_code == 200 and ok.json()["turns"] == 1
    assert client.get("/api/transcript").json()["turns"][0]["speaker"] == "human_agent"  # M2: legacy "agent"
