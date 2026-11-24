"""Next-best-action catalog, and how per-turn analysis shows suggestions (fake Bedrock only)."""

import pytest

from config.settings import BASE_DIR
from copilot.services.analyzer import ANGRY_TIP
from copilot.services.nba import NbaCatalog
from copilot.services.pipeline import ensure_pipeline
from copilot.services.sessions import CallSession
from tests.conftest import FAST_ARN, settle

RULES = BASE_DIR / "data" / "mock" / "nba_rules.json"


@pytest.fixture
def catalog() -> NbaCatalog:
    return NbaCatalog(RULES)


@pytest.fixture
def session() -> CallSession:
    return CallSession(call_sid="t-1", compliance={})


def test_llm_view_exposes_only_id_and_when(catalog):
    view = catalog.llm_view()
    assert {r["id"] for r in view} == {
        "nba-network-apology", "nba-bigger-pack", "nba-vas-refund", "nba-roaming-pack", "nba-sim-block",
    }
    assert all(set(r) == {"id", "when"} for r in view)


def test_show_adds_a_known_suggestion_once(catalog, session):
    assert catalog.show(session, "nba-bigger-pack") is True
    assert catalog.show(session, "nba-bigger-pack") is False
    assert catalog.show(session, "made-up-id") is False
    assert catalog.show(session, None) is False
    assert session.suggestions["nba-bigger-pack"] == {
        "id": "nba-bigger-pack", "title": "Offer the bigger 30-day pack (sample)",
        "text": "The customer runs out of data before the pack ends every month. Offer the bigger 30-day pack.",
        "source": "bundle-packs", "used": False,
    }


def test_speech_marks_a_shown_suggestion_used(catalog, session):
    catalog.show(session, "nba-bigger-pack")
    assert catalog.mark_used_by_speech(session, "Apni boro pack ta nite paren.") is True
    assert session.suggestions["nba-bigger-pack"]["used"] is True
    assert catalog.mark_used_by_speech(session, "boro pack") is False  # already used


def test_speech_does_not_mark_suggestions_that_were_never_shown(catalog, session):
    assert catalog.mark_used_by_speech(session, "boro pack") is False
    assert session.suggestions == {}


def test_manual_mark_used(catalog, session):
    catalog.show(session, "nba-roaming-pack")
    assert catalog.mark_used(session, "nba-roaming-pack") is True
    assert catalog.mark_used(session, "nba-roaming-pack") is False
    assert catalog.mark_used(session, "nba-sim-block") is False


async def test_analysis_result_shows_the_suggestion_and_agent_speech_marks_it_used(container, events, fake_bedrock):
    fake_bedrock.responses[FAST_ARN] = {
        "customer_mood": "frustrated", "agent_mood": "calm", "tip": "Offer the pack.",
        "suggestion_id": "nba-bigger-pack",
    }
    pipeline = await ensure_pipeline(container)
    await pipeline.ingest("customer", "final", "Protimash e data shesh hoye jay.")
    await settle(lambda: "nba-bigger-pack" in pipeline.session.suggestions)
    assert pipeline.session.mood == {"customer": "frustrated", "agent": "calm", "tip": "Offer the pack."}
    await pipeline.ingest("agent", "final", "Apni boro pack ta nite paren.")
    assert pipeline.session.suggestions["nba-bigger-pack"]["used"] is True
    await settle()
    assert events.of("suggestions")[-1][0]["used"] is True


async def test_invalid_moods_from_the_model_keep_the_previous_values(container, fake_bedrock):
    fake_bedrock.responses[FAST_ARN] = {"customer_mood": "furious", "agent_mood": 7, "tip": 5, "suggestion_id": "x"}
    pipeline = await ensure_pipeline(container)
    await pipeline.ingest("customer", "final", "Hello")
    await settle(lambda: pipeline.session.mood_history)
    assert pipeline.session.mood == {"customer": "neutral", "agent": "neutral", "tip": None}
    assert pipeline.session.suggestions == {}


async def test_bedrock_failure_falls_back_to_keyword_mood_without_a_suggestion(container, events, fake_bedrock):
    fake_bedrock.responses[FAST_ARN] = RuntimeError("Bedrock is down")
    pipeline = await ensure_pipeline(container)
    await pipeline.ingest("customer", "final", "Prottek bar eki kotha, kono kaj hoy na!")
    await settle(lambda: pipeline.session.mood_history)
    assert pipeline.session.mood == {"customer": "angry", "agent": "neutral", "tip": ANGRY_TIP}
    assert pipeline.session.suggestions == {}
    assert events.of("mood")[-1]["customer"] == "angry"


async def test_fallback_detects_frustration(container, fake_bedrock):
    fake_bedrock.responses[FAST_ARN] = RuntimeError("Bedrock is down")
    pipeline = await ensure_pipeline(container)
    await pipeline.ingest("customer", "final", "Amar ekta problem hocche.")
    await settle(lambda: pipeline.session.mood_history)
    assert pipeline.session.mood["customer"] == "frustrated" and pipeline.session.mood["tip"] is None


def test_use_suggestion_route(client, fake_bedrock):
    fake_bedrock.responses[FAST_ARN] = {
        "customer_mood": "neutral", "agent_mood": "neutral", "tip": None, "suggestion_id": "nba-roaming-pack",
    }
    assert client.post("/api/suggestions/nba-roaming-pack/use").status_code == 409  # no call yet
    client.post("/api/transcript", json={"speaker": "customer", "text": "Ami Malaysia jabo."})
    client.post("/api/analyze")  # runs the analysis synchronously
    assert client.post("/api/suggestions/nba-roaming-pack/use").json() == {"ok": True}
    assert client.post("/api/suggestions/nba-roaming-pack/use").status_code == 404  # already used
