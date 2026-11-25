"""Knowledge search for the live lookup and Ask Copilot.

When BEDROCK_KB_ID is set, the Bedrock Knowledge Base is the only source of knowledge. Nothing from the local
article files is mixed in, and nothing answers in its place: if the Knowledge Base cannot be reached the search
raises KnowledgeUnavailable, and the caller says so instead of answering from somewhere else. After a failure
the Knowledge Base is skipped for DATA_RETRY_AFTER_S seconds, so a bad connection does not slow every question.

Only documents that were uploaded with a language tag (en or bn, see scripts/upload_kb.py) are searched. The
data source covers a whole bucket, and anything else in it, such as saved call records, must never be returned
as knowledge.

When BEDROCK_KB_ID is empty (development without AWS) the local article files are used, and status() says so.
Chunks of the same document are merged into one Article, so the callers see the same shape either way.
"""
import asyncio
import logging
import re
import time

from botocore.config import Config

from config.settings import Settings
from copilot.services.bedrock import BedrockClient
from copilot.services.knowledge import Article, KnowledgeBase
from copilot.utils.aws import aws_session
from copilot.utils.helpers import read_text, render_prompt

log = logging.getLogger(__name__)
_FRONT_MATTER = re.compile(r"\A\s*---\n.*?\n---\s*", re.DOTALL)
REWRITE_SYSTEM = "You rewrite a telecom customer's sentence into a search query and reply with JSON only."
# The language tags that scripts/upload_kb.py puts in each document's metadata sidecar.
INDEXED_LANGUAGES = ["en", "bn"]
SEARCH_FILTER = {"in": {"key": "language", "value": INDEXED_LANGUAGES}}


class KnowledgeUnavailable(Exception):
    """The Knowledge Base could not be searched (no network, expired credentials, a timeout)."""


def _strip_front_matter(text: str) -> str:
    return _FRONT_MATTER.sub("", text, count=1).strip()


def _article_from_chunks(doc_id: str, chunks: list[dict]) -> Article:
    meta = chunks[0].get("metadata", {})
    body = "\n\n".join(_strip_front_matter(c["content"]["text"]) for c in chunks)
    return Article(
        id=doc_id,
        title=str(meta.get("title") or doc_id),
        type=str(meta.get("type") or "public"),
        source_url=str(meta.get("source_url") or ""),
        fetched=str(meta.get("fetched") or ""),
        category=str(meta.get("category") or ""),
        keywords=[],
        body=body,
    )


def articles_from_results(results: list[dict], limit: int) -> list[Article]:
    """Group retrieved chunks by document, keeping the order of the best chunk of each.
    A chunk without a doc_id did not come from an uploaded knowledge document, so it is left out."""
    groups: dict[str, list[dict]] = {}
    for chunk in results:
        doc_id = str(chunk.get("metadata", {}).get("doc_id") or "")
        if doc_id:
            groups.setdefault(doc_id, []).append(chunk)
    return [_article_from_chunks(doc_id, chunks) for doc_id, chunks in list(groups.items())[:limit]]


class KnowledgeRetriever:
    def __init__(self, settings: Settings, bedrock: BedrockClient, local: KnowledgeBase) -> None:
        self._settings, self._bedrock, self._local = settings, bedrock, local
        self._kb_id = settings.bedrock_kb_id
        self._client = None
        self._skip_until = 0.0
        self._last_error = ""
        self._rewrite_template = read_text(settings.data_dir / "prompts" / "kb_query.md")

    def status(self) -> dict:
        """Where knowledge comes from, and whether the Knowledge Base can be used right now."""
        using_kb = bool(self._kb_id)
        return {
            "backend": "bedrock_kb" if using_kb else "local_articles",
            "available": not (using_kb and time.monotonic() < self._skip_until),
            "last_error": self._last_error,
        }

    def get(self, article_id: str) -> Article | None:
        return self._local.get(article_id)

    async def search(self, query: str, limit: int = 4) -> list[Article]:
        """The best matching documents, best first. Raises KnowledgeUnavailable when the Knowledge Base is set
        but cannot be searched."""
        if not self._kb_id:
            return self._local.search(query, limit)
        if time.monotonic() < self._skip_until:
            raise KnowledgeUnavailable(self._last_error or "The Knowledge Base is paused after an error")
        try:
            text = await self._rewrite(query) if self._settings.bedrock_kb_rewrite_query else query
            results = await asyncio.wait_for(
                asyncio.to_thread(self._retrieve, text), self._settings.bedrock_kb_timeout_s
            )
        except Exception as error:
            self._last_error = f"{type(error).__name__}: {str(error)[:200]}"
            self._skip_until = time.monotonic() + self._settings.data_retry_after_s
            log.warning("Knowledge Base search failed: %s", self._last_error)
            raise KnowledgeUnavailable(self._last_error) from error
        self._last_error = ""
        return articles_from_results(results, limit)

    async def _rewrite(self, query: str) -> str:
        """Ask the model for an English search query. The original sentence is used if this fails."""
        prompt = render_prompt(self._rewrite_template, sentence=query)
        try:
            raw = await asyncio.wait_for(
                self._bedrock.complete_json(
                    self._settings.bedrock_inference_profile_arn_fast, REWRITE_SYSTEM, prompt, max_tokens=80
                ),
                self._settings.bedrock_kb_rewrite_timeout_s,
            )
            rewritten = str(raw.get("query") or "").strip()
            return rewritten or query
        except Exception as error:
            log.info("Query rewrite skipped: %s", type(error).__name__)
            return query

    async def warm_up(self) -> None:
        """Open the connection and load credentials before the first call, which is otherwise 3 to 5 s slower."""
        if self._kb_id:
            await asyncio.to_thread(self._retrieve, "internet")

    def _get_client(self):
        if self._client is None:
            self._client = aws_session(self._settings).client(
                "bedrock-agent-runtime", config=Config(read_timeout=10, retries={"max_attempts": 1})
            )
        return self._client

    def _retrieve(self, text: str) -> list[dict]:
        reply = self._get_client().retrieve(
            knowledgeBaseId=self._kb_id,
            retrievalQuery={"text": text},
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": self._settings.bedrock_kb_top_k,
                    "filter": SEARCH_FILTER,
                }
            },
        )
        return reply["retrievalResults"]
