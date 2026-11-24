"""Ask Copilot: no-match answer, source filtering and error handling (fake Bedrock only)."""

from copilot.services.ask import NO_ANSWER
from tests.conftest import FAST_ARN

NO_MATCH_QUESTION = "zzqx plorp vrumm"
ROAMING_QUESTION = "How do I activate roaming before I travel abroad?"


async def test_no_matching_article_returns_the_escalation_answer_without_calling_bedrock(container, fake_bedrock):
    assert container.knowledge.search(NO_MATCH_QUESTION) == []
    result = await container.ask.answer(NO_MATCH_QUESTION)
    assert result == {"answer": NO_ANSWER, "sources": []}
    assert fake_bedrock.calls == []


async def test_sources_are_limited_to_the_retrieved_articles(container, fake_bedrock):
    retrieved = [a.id for a in container.knowledge.search(ROAMING_QUESTION)]
    assert retrieved and len(retrieved) <= 4
    fake_bedrock.responses[FAST_ARN] = {"answer": "Dial *4848# ...", "sources": [retrieved[-1], "invented-article"]}
    result = await container.ask.answer(ROAMING_QUESTION)
    assert result["answer"] == "Dial *4848# ..."
    assert [s["id"] for s in result["sources"]] == [retrieved[-1]]
    assert set(result["sources"][0]) == {"id", "title", "type", "source_url"}
    prompt = fake_bedrock.calls_for(FAST_ARN)[0]["user"]
    assert ROAMING_QUESTION in prompt and f"[{retrieved[0]}]" in prompt


async def test_only_invented_sources_fall_back_to_the_top_article(container, fake_bedrock):
    top = container.knowledge.search(ROAMING_QUESTION)[0].id
    fake_bedrock.responses[FAST_ARN] = {"answer": "x", "sources": ["invented"]}
    result = await container.ask.answer(ROAMING_QUESTION)
    assert [s["id"] for s in result["sources"]] == [top]


def test_ask_route_no_match(client, fake_bedrock):
    response = client.post("/api/ask", json={"question": NO_MATCH_QUESTION})
    assert response.status_code == 200
    assert response.json() == {"answer": NO_ANSWER, "sources": []}
    assert fake_bedrock.calls == []


def test_ask_route_maps_bedrock_failure_to_502(client, fake_bedrock):
    fake_bedrock.responses[FAST_ARN] = RuntimeError("throttled")
    response = client.post("/api/ask", json={"question": ROAMING_QUESTION})
    assert response.status_code == 502
    assert response.json()["detail"].startswith("Ask Copilot failed")


def test_ask_route_validates_question_length(client):
    assert client.post("/api/ask", json={"question": "a"}).status_code == 422
    assert client.post("/api/ask", json={"question": "a" * 501}).status_code == 422


def test_knowledge_articles_route(client):
    articles = client.get("/api/knowledge/articles").json()["articles"]
    assert len(articles) == 15
    assert all(set(a) == {"id", "title", "type", "source_url"} for a in articles)
