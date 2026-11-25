"""Knowledge Base retrieval: it is the only source when it is configured, and it never falls back to local files."""
import asyncio
from pathlib import Path

import pytest

from config.settings import Settings
from copilot.services.kb_retriever import (
    INDEXED_LANGUAGES,
    KnowledgeRetriever,
    KnowledgeUnavailable,
    articles_from_results,
)
from copilot.services.knowledge import KnowledgeBase

DATA = Path(__file__).resolve().parents[1] / "data"


def chunk(doc_id: str, text: str, title: str = "T", kind: str = "public") -> dict:
    return {
        "content": {"text": text},
        "metadata": {"doc_id": doc_id, "title": title, "type": kind, "source_url": f"https://x/{doc_id}", "language": "en"},
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


def search(retriever: KnowledgeRetriever, query: str = "roaming", limit: int = 4):
    return asyncio.run(retriever.search(query, limit))


# ---- grouping ----
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


def test_a_chunk_that_is_not_an_uploaded_knowledge_document_is_left_out():
    call_record = {"content": {"text": "customer: amar net kaj korche na"}, "metadata": {"x-amz-bedrock-kb-source-uri": "s3://b/copilot/calls/1.json"},
                   "location": {"s3Location": {"uri": "s3://b/copilot/calls/1.json"}}}
    assert [a.id for a in articles_from_results([call_record, chunk("a", "x")], limit=4)] == ["a"]
    assert articles_from_results([call_record], limit=4) == []


# ---- the Knowledge Base is the only source ----
def test_without_a_kb_id_the_local_articles_answer_and_the_status_says_so():
    retriever = make(kb_id="")
    assert search(retriever, "roaming activate")
    assert retriever.status() == {"backend": "local_articles", "available": True, "last_error": ""}


def test_only_the_knowledge_base_results_are_returned_never_local_articles():
    retriever = make()
    retriever._retrieve = lambda text: [chunk("campaign-faq", "unrelated")]
    found = search(retriever, "SIM hariye gese, ki korbo")  # the local keyword search would have found sim-replacement
    assert [a.id for a in found] == ["campaign-faq"]


def test_nothing_found_in_the_knowledge_base_is_an_empty_list_not_a_local_answer():
    retriever = make()
    retriever._retrieve = lambda text: []
    assert search(retriever, "roaming activate") == []
    assert retriever.status()["available"] is True


def test_a_kb_failure_raises_pauses_the_kb_and_never_answers_locally():
    retriever = make()
    calls = []

    def boom(text: str):
        calls.append(text)
        raise RuntimeError("expired token")

    retriever._retrieve = boom
    with pytest.raises(KnowledgeUnavailable, match="expired token"):
        search(retriever, "roaming activate")
    with pytest.raises(KnowledgeUnavailable):  # paused: no second call, still no local answer
        search(retriever, "roaming activate")
    assert len(calls) == 1
    status = retriever.status()
    assert status["available"] is False and "expired token" in status["last_error"]


def test_the_knowledge_base_is_tried_again_after_the_pause():
    retriever = make()
    retriever._retrieve = lambda text: (_ for _ in ()).throw(RuntimeError("down"))
    with pytest.raises(KnowledgeUnavailable):
        search(retriever)
    retriever._skip_until = 0.0  # the pause is over
    retriever._retrieve = lambda text: [chunk("roaming-basics", "x")]
    assert [a.id for a in search(retriever)] == ["roaming-basics"]
    assert retriever.status() == {"backend": "bedrock_kb", "available": True, "last_error": ""}


def test_a_search_that_takes_too_long_raises():
    import time
    retriever = make(bedrock_kb_timeout_s=0.05)
    retriever._retrieve = lambda text: time.sleep(0.5) or []
    with pytest.raises(KnowledgeUnavailable):
        search(retriever)


def test_the_search_asks_only_for_uploaded_documents_by_language():
    sent = {}

    class Client:
        def retrieve(self, **kwargs):
            sent.update(kwargs)
            return {"retrievalResults": []}

    retriever = make()
    retriever._client = Client()
    search(retriever)
    config = sent["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert config["filter"] == {"in": {"key": "language", "value": INDEXED_LANGUAGES}} and INDEXED_LANGUAGES == ["en", "bn"]
    assert sent["knowledgeBaseId"] == "KB1" and config["numberOfResults"] == 5


# ---- the optional rewrite ----
def test_kb_results_are_used_and_the_query_is_rewritten():
    bedrock = FakeBedrock({"query": "how to activate roaming"})
    retriever = make(bedrock=bedrock, bedrock_kb_rewrite_query=True)
    seen = []
    retriever._retrieve = lambda text: seen.append(text) or [chunk("roaming-activation", "Dial *121#", "Roaming")]
    found = search(retriever, "roaming kemne chalu korbo")
    assert seen == ["how to activate roaming"]
    assert found[0].id == "roaming-activation"


def test_the_original_sentence_is_used_when_the_rewrite_fails():
    retriever = make(bedrock=FakeBedrock(error=RuntimeError("throttled")), bedrock_kb_rewrite_query=True)
    seen = []
    retriever._retrieve = lambda text: seen.append(text) or [chunk("a", "x")]
    search(retriever, "net kaj korche na")
    assert seen == ["net kaj korche na"]
