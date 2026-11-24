"""After-call summary: Bedrock result handling and the empty-draft fallback (fake Bedrock only)."""

import json

from copilot.container import build_container
from copilot.services.bedrock import BedrockClient
from copilot.services.pipeline import ensure_pipeline
from copilot.services.summarizer import EMPTY_DRAFT
from tests.conftest import SUMMARY_ARN, make_settings


async def _call_with_one_line(container):
    pipeline = await ensure_pipeline(container)
    await pipeline.ingest("customer", "final", "Net kaj korche na.")
    return pipeline.session


def _saved(runtime_dir, session) -> dict:
    return json.loads((runtime_dir / "calls" / f"{session.call_sid}.json").read_text(encoding="utf-8"))


async def test_summary_uses_the_summary_model_and_is_saved_and_broadcast(container, events, fake_bedrock, runtime_dir):
    session = await _call_with_one_line(container)
    await container.summarizer.run(session)
    call = fake_bedrock.calls_for(SUMMARY_ARN)[0]
    assert call["max_tokens"] == 700 and "CUSTOMER: Net kaj korche na." in call["user"]
    assert session.summary["category"] == "Network" and set(session.summary) == set(EMPTY_DRAFT)
    assert events.of("summary")[-1] == session.summary
    assert _saved(runtime_dir, session)["summary"] == session.summary


async def test_bedrock_failure_gives_an_empty_draft_the_agent_can_fill(container, events, fake_bedrock, runtime_dir):
    fake_bedrock.responses[SUMMARY_ARN] = RuntimeError("throttled")
    session = await _call_with_one_line(container)
    await container.summarizer.run(session)
    assert session.summary == EMPTY_DRAFT
    assert events.of("summary")[-1] == EMPTY_DRAFT
    assert _saved(runtime_dir, session)["summary"] == EMPTY_DRAFT


async def test_a_call_without_turns_does_not_call_bedrock(container, fake_bedrock):
    pipeline = await ensure_pipeline(container)
    await container.summarizer.run(pipeline.session)
    assert fake_bedrock.calls_for(SUMMARY_ARN) == []
    assert pipeline.session.summary == EMPTY_DRAFT


async def test_unknown_category_extra_keys_and_non_string_values_are_cleaned(container, fake_bedrock):
    fake_bedrock.responses[SUMMARY_ARN] = {
        "category": "Spaceships", "subject": 42, "problem": ["a"], "injected": "drop me",
    }
    session = await _call_with_one_line(container)
    await container.summarizer.run(session)
    assert session.summary["category"] == "Other"
    assert session.summary["subject"] == "42" and session.summary["problem"] == "['a']"
    assert "injected" not in session.summary


async def test_missing_summary_arn_falls_back_without_touching_aws(runtime_dir):
    """Real BedrockClient with no ARN: it must raise before any AWS client is created (the conftest
    guard fails the test if a client is created), and the summarizer must fall back."""
    settings = make_settings(runtime_dir, bedrock_inference_profile_arn_summary="")
    container = build_container(settings, bedrock=BedrockClient(settings))
    session = await _call_with_one_line(container)
    await container.summarizer.run(session)
    assert session.summary == EMPTY_DRAFT


def test_summary_routes(client, fake_bedrock):
    assert client.get("/api/summary").status_code == 409  # no call
    assert client.post("/api/summary").status_code == 409
    client.post("/api/transcript", json={"speaker": "customer", "text": "Net kaj korche na."})
    assert client.get("/api/summary").status_code == 404  # not written yet
    fake_bedrock.responses[SUMMARY_ARN] = RuntimeError("down")
    body = client.post("/api/summary").json()
    assert body == {"summary": EMPTY_DRAFT}
    assert client.get("/api/summary").json() == {"summary": EMPTY_DRAFT}
