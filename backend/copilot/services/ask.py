"""Ask Copilot: answers an agent's question from the knowledge pack, with sources."""

from config.settings import Settings
from copilot.services.bedrock import BedrockClient
from copilot.services.kb_retriever import KnowledgeRetriever
from copilot.utils.helpers import read_text, render_prompt

SYSTEM = "You answer contact-centre agent questions from approved articles and reply with JSON only."
NO_ANSWER = "I could not find this in the approved knowledge. Please escalate to a supervisor."


class AskCopilot:
    def __init__(self, settings: Settings, bedrock: BedrockClient, kb: KnowledgeRetriever) -> None:
        self._model = settings.bedrock_inference_profile_arn_fast
        self._bedrock, self._kb = bedrock, kb
        self._template = read_text(settings.data_dir / "prompts" / "ask_copilot.md")

    async def answer(self, question: str) -> dict:
        articles = await self._kb.search(question)
        if not articles:
            return {"answer": NO_ANSWER, "sources": []}
        block = "\n\n".join(
            f"[{a.id}] {a.title} (type: {a.type})\n{a.body}" for a in articles
        )
        prompt = render_prompt(self._template, articles=block, question=question)
        result = await self._bedrock.complete_json(self._model, SYSTEM, prompt, max_tokens=700)
        allowed = {a.id for a in articles}
        by_id = {a.id: a for a in articles}
        ids = [s for s in result.get("sources", []) if s in allowed] or [articles[0].id]
        sources = [by_id[i].public_view() for i in ids]
        return {"answer": str(result.get("answer", NO_ANSWER)), "sources": sources}
