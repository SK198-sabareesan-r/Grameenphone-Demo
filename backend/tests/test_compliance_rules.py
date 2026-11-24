"""Rule-based compliance reminders, using the real data/mock/compliance_rules.json."""

import pytest

from config.settings import BASE_DIR
from copilot.services.compliance import ComplianceEngine
from copilot.services.pipeline import ensure_pipeline

RULES = BASE_DIR / "data" / "mock" / "compliance_rules.json"


@pytest.fixture
def engine() -> ComplianceEngine:
    return ComplianceEngine(RULES)


def test_rules_start_idle_and_are_hidden(engine):
    states = engine.initial_states()
    assert states == {"verify_identity": "idle", "read_price_validity": "idle"}
    assert engine.view(states) == []


def test_verify_identity_goes_red_then_green(engine):
    states = engine.initial_states()
    assert engine.evaluate(states, "agent", "Apnar account er details bolchi") is True
    assert states["verify_identity"] == "red"
    view = engine.view(states)
    assert view == [{
        "id": "verify_identity", "label": "Verify identity first", "state": "red",
        "message": "Verify identity first (for example, ask for the last recharge amount).",
    }]
    assert engine.evaluate(states, "agent", "Age apnar last recharge koto chilo?") is True
    assert states["verify_identity"] == "green"
    assert engine.view(states)[0]["message"] == "Identity checked."


def test_identity_check_before_the_trigger_counts(engine):
    states = engine.initial_states()
    engine.evaluate(states, "agent", "Apnar last recharge koto chilo?")
    assert states["verify_identity"] == "green"
    assert engine.evaluate(states, "agent", "Apnar account er details bolchi") is False  # green stays green
    assert states["verify_identity"] == "green"


def test_rules_are_scoped_to_the_speaker(engine):
    states = engine.initial_states()
    assert engine.evaluate(states, "customer", "amar account er details ki?") is False
    assert states["verify_identity"] == "idle"


def test_bangla_keywords_trigger(engine):
    states = engine.initial_states()
    engine.evaluate(states, "agent", "আপনার অ্যাকাউন্টের তথ্য বলছি")
    assert states["verify_identity"] == "red"


def test_read_price_and_validity_needs_the_trigger_first(engine):
    states = engine.initial_states()
    engine.evaluate(states, "agent", "Price 299 taka")  # satisfy_before_trigger is false
    assert states["read_price_validity"] == "idle"
    engine.evaluate(states, "customer", "Thik ache, chalu kore den.")
    assert states["read_price_validity"] == "pending"
    engine.evaluate(states, "agent", "Dam 299 taka, validity 30 din.")
    assert states["read_price_validity"] == "green"


async def test_pipeline_broadcasts_compliance_only_when_it_changes(container, events):
    pipeline = await ensure_pipeline(container)
    await pipeline.ingest("agent", "final", "Apnar account er details bolchi")
    await pipeline.ingest("agent", "final", "Ekto opekkha korun")
    await pipeline.ingest("agent", "final", "Apnar last recharge koto chilo?")
    updates = events.of("compliance")
    assert [[item["state"] for item in update] for update in updates] == [["red"], ["green"]]
