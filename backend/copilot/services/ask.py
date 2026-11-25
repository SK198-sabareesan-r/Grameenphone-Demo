"""Ask Copilot: answers an agent's question from the knowledge pack, with sources.

Answers come only from the Knowledge Base. If it cannot be reached, the reply says so ("unavailable": true).
The chat has a short memory, kept in the browser. Each request carries the recent turns. A follow-up such as
"and the price?" is first rewritten into a standalone question using that chat, and that question is what gets
searched. Nothing about the chat is stored here, so it is gone when the browser page is refreshed.
"""
import asyncio
import logging

from config.settings import Settings
from copilot.services.bedrock import BedrockClient
from copilot.services.kb_retriever import KnowledgeRetriever, KnowledgeUnavailable
from copilot.utils.helpers import read_text, render_prompt

log = logging.getLogger(__name__)
SYSTEM = (
    "You answer contact-centre agent questions from approved articles and reply with JSON only. "
    "Always answer in the language of the agent's question: Bangla, Banglish (Bangla in English letters) or English."
)
CONDENSE_SYSTEM = "You rewrite a chat message into a standalone search question and reply with JSON only."
NO_ANSWER = "I could not find this in the approved knowledge. Please escalate to a supervisor."
UNAVAILABLE = "The knowledge base cannot be reached right now, so I cannot answer. Please try again in a moment, or ask a supervisor."
MAX_HISTORY_MESSAGES = 6  # the last three exchanges
MAX_ASSISTANT_CHARS = 700  # an earlier answer is only context, so a long one is cut short
MAX_USER_CHARS = 500
MAX_STANDALONE_CHARS = 500
ROLE_LABEL = {"user": "Agent", "assistant": "Copilot"}


def recent_turns(history: list[dict] | None) -> list[dict]:
    """The last few valid turns, with long messages cut short. Anything else in the list is ignored."""
    turns = []
    for item in history or []:
        role, text = item.get("role"), str(item.get("content") or "").strip()
        if role not in ROLE_LABEL or not text:
            continue
        limit = MAX_ASSISTANT_CHARS if role == "assistant" else MAX_USER_CHARS
        turns.append({"role": role, "content": text if len(text) <= limit else text[:limit].rstrip() + "..."})
    return turns[-MAX_HISTORY_MESSAGES:]


def conversation_text(turns: list[dict]) -> str:
    if not turns:
        return "(none)"
    return "\n".join(f"{ROLE_LABEL[t['role']]}: {t['content']}" for t in turns)


class AskCopilot:
    def __init__(self, settings: Settings, bedrock: BedrockClient, kb: KnowledgeRetriever) -> None:
        self._model = settings.bedrock_inference_profile_arn_fast
        self._bedrock, self._kb = bedrock, kb
        self._timeout_s = settings.ask_condense_timeout_s
        self._template = read_text(settings.data_dir / "prompts" / "ask_copilot.md")
        self._condense_template = read_text(settings.data_dir / "prompts" / "ask_condense.md")

    async def answer(self, question: str, history: list[dict] | None = None) -> dict:
        turns = recent_turns(history)
        standalone = await self._standalone(question, turns) if turns else question
        extra = {"searched_for": standalone} if standalone != question else {}  # shown when the chat changed the question
        try:
            articles = await self._kb.search(standalone)
        except KnowledgeUnavailable:
            # Never answer from anywhere else: the answer would look approved when it is not.
            return {"answer": UNAVAILABLE, "sources": [], "unavailable": True, **extra}
        if not articles:
            return {"answer": NO_ANSWER, "sources": [], **extra}
        block = "\n\n".join(f"[{a.id}] {a.title} (type: {a.type})\n{a.body}" for a in articles)
        prompt = render_prompt(
            self._template, articles=block, conversation=conversation_text(turns), question=question, standalone=standalone
        )
        result = await self._bedrock.complete_json(self._model, SYSTEM, prompt, max_tokens=900)
        allowed = {a.id for a in articles}
        by_id = {a.id: a for a in articles}
        listed = result.get("sources") or []
        ids = [s for s in listed if s in allowed]
        if listed and not ids:  # it named only documents that were not retrieved: show the best match instead
            ids = [articles[0].id]
        # No sources listed means it used none (for example "not in the knowledge"), so none are shown.
        sources = [by_id[i].public_view() for i in ids]
        return {"answer": str(result.get("answer", NO_ANSWER)), "sources": sources, **extra}

    async def _standalone(self, question: str, turns: list[dict]) -> str:
        """The question rewritten so it can be searched without the chat. The original is used if this fails."""
        prompt = render_prompt(self._condense_template, conversation=conversation_text(turns), question=question)
        try:
            reply = await asyncio.wait_for(
                self._bedrock.complete_json(self._model, CONDENSE_SYSTEM, prompt, max_tokens=120), self._timeout_s
            )
        except Exception as error:
            log.warning("Ask Copilot could not use the chat to rewrite the question: %s", type(error).__name__)
            return question
        rewritten = reply.get("question") if isinstance(reply, dict) else None
        if not isinstance(rewritten, str) or not rewritten.strip():
            return question
        return " ".join(rewritten.split())[:MAX_STANDALONE_CHARS]
