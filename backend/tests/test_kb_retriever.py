"""Knowledge Base retrieval: grouping of chunks, and the fall back to the local articles."""
import asyncio
from pathlib import Path

from config.settings import Settings
from copilot.services.kb_retriever import KnowledgeRetriever, articles_from_results
from copilot.services.knowledge import KnowledgeBase

DATA = Path(__file__).resolve().parents[1] / "data"


def chunk(doc_id: str, text: str, title: str = "T", kind: str = "public") -> dict:
    return {
        "content": {"text": text},
        "metadata": {"doc_id": doc_id, "title": title, "type": kind, "source_url": f"https://x/{doc_id}"},
        "location": {"s3Location": {"uri": f"s3://b/{doc_id}"}},
    }


class FakeBedrock:
    def __init__(self, reply=None, error: Exception | None = None) -> None:
        self.reply, self.error, self.calls = reply or {}, error, 0

    async def complete_json(self, *args, **kwargs) -> dict:
        self.calls += 1
        if self.error:
            raise self.error
        return self.reply


def make(kb_id: str = "KB1", bedrock: FakeBedrock | None = None, **extra) -> KnowledgeRetriever:
    settings = Settings(_env_file=None, bedrock_kb_id=kb_id, bedrock_inference_profile_arn_fast="arn:x", **extra)
    return KnowledgeRetriever(settings, bedrock or FakeBedrock({"query": "roaming activation"}),
                              KnowledgeBase(DATA / "knowledge" / "articles"))


def test_chunks_of_one_document_are_merged_in_best_first_order():
    results = [chunk("a", "first"), chunk("b", "other", kind="mock"), chunk("a", "second")]
    articles = articles_from_results(results, limit=4)
    assert [a.id for a in articles] == ["a", "b"]
    assert articles[0].body == "first\n\nsecond"
    assert articles[1].type == "mock"


def test_front_matter_is_removed_from_article_chunks():
    articles = articles_from_results([chunk("a", "---\nid: a\ntitle: T\n---\nBody text")], limit=4)
    assert articles[0].body == "Body text"


def test_limit_applies_to_documents():
    results = [chunk(str(i), "x") for i in range(6)]
    assert len(articles_from_results(results, limit=3)) == 3


def test_without_a_kb_id_the_local_articles_answer():
    retriever = make(kb_id="")
    found = asyncio.run(retriever.search("roaming activate"))
    assert found and retriever.status()["backend"] == "local_articles"


def test_a_kb_failure_falls_back_to_local_and_pauses_the_kb():
    retriever = make()
    calls = []

    def boom(text: str):
        calls.append(text)
        raise RuntimeError("expired token")

    retriever._retrieve = boom
    first = asyncio.run(retriever.search("roaming activate"))
    asyncio.run(retriever.search("roaming activate"))
    assert first, "local search should still answer"
    assert len(calls) == 1, "the KB is skipped while it is paused"
    status = retriever.status()
    assert status["fallback_active"] and "expired token" in status["last_error"]


def test_kb_results_are_used_and_the_query_is_rewritten():
    bedrock = FakeBedrock({"query": "how to activate roaming"})
    retriever = make(bedrock=bedrock)
    seen = []
    retriever._retrieve = lambda text: seen.append(text) or [chunk("roaming-activation", "Dial *121#", "Roaming")]
    found = asyncio.run(retriever.search("roaming kemne chalu korbo"))
    assert seen == ["how to activate roaming"]
    assert found[0].id == "roaming-activation"
    assert not retriever.status()["fallback_active"]


def test_the_original_sentence_is_used_when_the_rewrite_fails():
    retriever = make(bedrock=FakeBedrock(error=RuntimeError("throttled")))
    seen = []
    retriever._retrieve = lambda text: seen.append(text) or [chunk("a", "x")]
    asyncio.run(retriever.search("net kaj korche na"))
    assert seen == ["net kaj korche na"]
