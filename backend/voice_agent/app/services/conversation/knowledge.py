"""Approved-knowledge lookup for AI replies: asks the Copilot (Bedrock Knowledge Base, local fallback).

Never raises and never blocks a reply for long: on any failure or timeout it returns "" and the model
answers without it, as before.
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)

KNOWLEDGE_HEADER = (
    "Approved Grameenphone knowledge for this question (from the company knowledge base). "
    "Use it to answer general questions. If it does not cover the question, say you are not sure."
)


class KnowledgeLookup:
    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._url = settings.copilot_knowledge_url
        self._token = settings.internal_api_token.get_secret_value()
        self._timeout = settings.ai_knowledge_timeout_s
        self._owns_client = client is None
        self._http = client or httpx.AsyncClient(timeout=httpx.Timeout(self._timeout))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def context_for(self, call_id: str, question: str) -> str:
        """Text block to append to the system prompt, or "" when nothing was found."""
        question = question.strip()
        if not question:
            return ""
        try:
            reply = await self._http.post(
                self._url,
                json={"query": question[:500], "limit": 3},
                headers={"X-Internal-Token": self._token},
                timeout=self._timeout,
            )
            reply.raise_for_status()
            articles = reply.json().get("articles", [])
        except Exception as exc:
            logger.warning("ai_knowledge_lookup_failed", extra={"call_id": call_id, "error": type(exc).__name__})
            return ""
        logger.info(
            "ai_knowledge_lookup",
            extra={"call_id": call_id, "articles": [a.get("id") for a in articles]},
        )
        if not articles:
            return ""
        blocks = [f"[{a.get('title', a.get('id', ''))}]\n{a.get('body', '')}" for a in articles]
        return KNOWLEDGE_HEADER + "\n\n" + "\n\n".join(blocks)
