"""Knowledge search for the live lookup and Ask Copilot.

Uses the Bedrock Knowledge Base when BEDROCK_KB_ID is set. If it is not set, or a call fails or times out,
the local keyword search over the article files answers instead, and the Knowledge Base is skipped for
DATA_RETRY_AFTER_S seconds so a bad connection does not slow every lookup.

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
    """Group retrieved chunks by document, keeping the order of the best chunk of each."""
    groups: dict[str, list[dict]] = {}
    for chunk in results:
        meta = chunk.get("metadata", {})
        doc_id = str(meta.get("doc_id") or chunk.get("location", {}).get("s3Location", {}).get("uri", ""))
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
        using_kb = bool(self._kb_id)
        return {
            "backend": "bedrock_kb" if using_kb else "local_articles",
            "fallback_active": using_kb and time.monotonic() < self._skip_until,
            "last_error": self._last_error,
        }

    def get(self, article_id: str) -> Article | None:
        return self._local.get(article_id)

    async def search(self, query: str, limit: int = 4) -> list[Article]:
        if not self._kb_id or time.monotonic() < self._skip_until:
            return self._local.search(query, limit)
        try:
            text = await self._rewrite(query) if self._settings.bedrock_kb_rewrite_query else query
            results = await asyncio.wait_for(
                asyncio.to_thread(self._retrieve, text), self._settings.bedrock_kb_timeout_s
            )
            self._last_error = ""
            return articles_from_results(results, limit)
        except Exception as error:
            self._last_error = f"{type(error).__name__}: {str(error)[:200]}"
            self._skip_until = time.monotonic() + self._settings.data_retry_after_s
            log.warning("Knowledge Base search failed, using the local articles: %s", self._last_error)
            return self._local.search(query, limit)

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

    def _retrieve(self, text: str) -> list[dict]:
        if self._client is None:
            self._client = aws_session(self._settings).client(
                "bedrock-agent-runtime", config=Config(read_timeout=10, retries={"max_attempts": 1})
            )
        reply = self._client.retrieve(
            knowledgeBaseId=self._kb_id,
            retrievalQuery={"text": text},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": self._settings.bedrock_kb_top_k}
            },
        )
        return reply["retrievalResults"]
