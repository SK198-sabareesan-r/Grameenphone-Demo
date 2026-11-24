"""M2: customer, AI agent and human agent are separate identities with separate effects."""

import json

import pytest

from config.settings import BASE_DIR
from copilot.models.participants import (
    AI_AGENT, CUSTOMER, HUMAN_AGENT, PARTICIPANTS, normalize_speaker,
)
from copilot.services.compliance import ComplianceEngine
from copilot.services.pipeline import ensure_pipeline
from tests.conftest import FAST_ARN, SUMMARY_ARN, settle

REDUCER = BASE_DIR.parent / "frontend" / "src" / "state" / "callReducer.js"
TRANSCRIPT_JSX = BASE_DIR.parent / "frontend" / "src" / "components" / "Transcript.jsx"
LABELS_JS = BASE_DIR.parent / "frontend" / "src" / "state" / "callLabels.js"


# --- the role model ----------------------------------------------------------------------------


def test_three_roles_and_the_legacy_alias():
    assert PARTICIPANTS == ("customer", "ai_agent", "human_agent")
    assert normalize_speaker("agent") == HUMAN_AGENT
    assert normalize_speaker(" Customer ") == CUSTOMER
    assert normalize_speaker("ai_agent") == AI_AGENT


@pytest.mark.parametrize("bad", ["", "bot", "assistant", "supervisor", "AI AGENT"])
def test_unknown_speakers_are_rejected(bad):
    with pytest.raises(ValueError):
        normalize_speaker(bad)


def test_compliance_rule_file_speakers_are_normalised_to_people():
    engine = ComplianceEngine(BASE_DIR / "data" / "mock" / "compliance_rules.json")
    speakers = {rule[part]["speaker"] for rule in engine.rules for part in ("trigger", "satisfy")}
    assert speakers == {CUSTOMER, HUMAN_AGENT}


def test_a_rule_that_names_the_ai_agent_is_refused(tmp_path):
    rules = json.loads((BASE_DIR / "data" / "mock" / "compliance_rules.json").read_text(encoding="utf-8"))
    rules[0]["satisfy"]["speaker"] = "ai_agent"
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(rules), encoding="utf-8")
    with pytest.raises(ValueError):
        ComplianceEngine(path)


# --- pipeline effects per role -----------------------------------------------------------------


async def test_ai_agent_speech_never_changes_compliance(container, events):
    pipeline = await ensure_pipeline(container)
    await pipeline.ingest(AI_AGENT, "final", "Apnar account er details bolchi")  # would trigger the rule
    await pipeline.ingest(AI_AGENT, "final", "Apnar last recharge koto chilo?")  # would satisfy it
    assert set(pipeline.session.compliance.values()) == {"idle"}
    assert events.of("compliance") == []
    await pipeline.ingest(HUMAN_AGENT, "final", "Apnar account er details bolchi")
    assert pipeline.session.compliance["verify_identity"] == "red"
    await pipeline.ingest(AI_AGENT, "final", "Apnar last recharge koto chilo?")
    assert pipeline.session.compliance["verify_identity"] == "red"  # still the human's job
    await pipeline.ingest(HUMAN_AGENT, "final", "Apnar last recharge koto chilo?")
    assert pipeline.session.compliance["verify_identity"] == "green"


async def test_ai_agent_speech_never_marks_a_suggestion_used(container, fake_bedrock):
    fake_bedrock.responses[FAST_ARN] = {"customer_mood": "neutral", "agent_mood": "neutral", "tip": None,
                                        "suggestion_id": "nba-bigger-pack"}
    pipeline = await ensure_pipeline(container)
    await pipeline.ingest(CUSTOMER, "final", "Protimash e data shesh hoye jay.")
    await settle(lambda: "nba-bigger-pack" in pipeline.session.suggestions)
    await pipeline.ingest(AI_AGENT, "final", "Apni boro pack ta nite paren.")
    assert pipeline.session.suggestions["nba-bigger-pack"]["used"] is False
    await pipeline.ingest(HUMAN_AGENT, "final", "Apni boro pack ta nite paren.")
    assert pipeline.session.suggestions["nba-bigger-pack"]["used"] is True


async def test_ai_agent_turns_do_not_trigger_analysis_but_are_in_its_context(container, fake_bedrock):
    pipeline = await ensure_pipeline(container)
    await pipeline.ingest(AI_AGENT, "final", "Hello! How can I help you today?")
    await settle()
    assert fake_bedrock.calls_for(FAST_ARN) == []
    await pipeline.ingest(CUSTOMER, "final", "Net kaj korche na.")
    await settle(lambda: fake_bedrock.calls_for(FAST_ARN))
    prompt = fake_bedrock.calls_for(FAST_ARN)[0]["user"]
    assert "AI AGENT: Hello! How can I help you today?\nCUSTOMER: Net kaj korche na." in prompt
    assert "HUMAN AGENT" in prompt  # the prompt explains the three labels


async def test_the_customer_card_still_appears_on_the_first_customer_line(container, events):
    pipeline = await ensure_pipeline(container)
    await pipeline.ingest(AI_AGENT, "final", "Welcome to Grameenphone.")
    assert events.of("card") == []
    await pipeline.ingest(CUSTOMER, "final", "Net kaj korche na.")
    assert len(events.of("card")) == 1


async def test_partials_carry_the_role(container, events):
    pipeline = await ensure_pipeline(container)
    await pipeline.ingest("agent", "partial", "ami dekh")
    await pipeline.ingest(AI_AGENT, "partial", "one moment")
    assert [e["speaker"] for e in events.of("transcript")] == [HUMAN_AGENT, AI_AGENT]


async def test_unknown_speakers_never_reach_the_transcript(container):
    pipeline = await ensure_pipeline(container)
    with pytest.raises(ValueError):
        await pipeline.ingest("bot", "final", "hi")
    assert pipeline.session.turns == []


async def test_saved_record_and_summary_prompt_keep_the_three_roles_apart(container, fake_bedrock, runtime_dir):
    pipeline = await ensure_pipeline(container)
    await pipeline.ingest(AI_AGENT, "final", "Welcome. Please hold for an agent.")
    await pipeline.ingest(CUSTOMER, "final", "Net kaj korche na.")
    await pipeline.ingest(HUMAN_AGENT, "final", "Ami Tania bolchi.")
    await pipeline.finish()
    await settle(lambda: pipeline.session.summary is not None)
    record = json.loads((runtime_dir / "calls" / f"{pipeline.session.call_sid}.json").read_text(encoding="utf-8"))
    assert [t["speaker"] for t in record["turns"]] == [AI_AGENT, CUSTOMER, HUMAN_AGENT]
    prompt = fake_bedrock.calls_for(SUMMARY_ARN)[0]["user"]
    assert "AI AGENT: Welcome." in prompt and "HUMAN AGENT: Ami Tania bolchi." in prompt
    assert "say what the human agent did" in prompt


# --- API -------------------------------------------------------------------------------------


def test_typed_line_api_accepts_each_role_and_the_legacy_value(client):
    for speaker, expected in [("customer", CUSTOMER), ("ai_agent", AI_AGENT),
                              ("human_agent", HUMAN_AGENT), ("agent", HUMAN_AGENT)]:
        assert client.post("/api/transcript", json={"speaker": speaker, "text": f"line from {speaker}"}).status_code == 200
    turns = client.get("/api/transcript").json()["turns"]
    assert [t["speaker"] for t in turns] == [CUSTOMER, AI_AGENT, HUMAN_AGENT, HUMAN_AGENT]
    assert client.post("/api/transcript", json={"speaker": "AI AGENT", "text": "x"}).status_code == 422


def test_panel_receives_the_ai_agent_role(client):
    with client.websocket_connect("/api/panel/stream") as ws:
        client.post("/api/transcript", json={"speaker": "ai_agent", "text": "Hello!"})
        assert ws.receive_json()["type"] == "call.started"
        message = ws.receive_json()
    assert message["type"] == "transcript" and message["data"]["speaker"] == "ai_agent"


# --- frontend contract (read-only checks of the source) ------------------------------------------


def test_frontend_labels_every_role():
    assert "SPEAKER_LABEL" in TRANSCRIPT_JSX.read_text(encoding="utf-8")  # the labels live in state/callLabels.js
    source = LABELS_JS.read_text(encoding="utf-8")
    for role in PARTICIPANTS:
        assert f"{role}:" in source.replace(" ", ""), f"Transcript.jsx has no label for {role}"
    assert 'agent:"Agent"' in source.replace(" ", "")  # older records still render


def test_frontend_reducer_tracks_a_partial_per_role():
    source = REDUCER.read_text(encoding="utf-8").replace(" ", "")
    assert 'partials:{customer:"",ai_agent:"",human_agent:""}' in source
