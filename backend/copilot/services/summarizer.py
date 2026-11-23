"""After-call summary and ticket draft (Amazon Bedrock)."""

import logging

from config.settings import Settings
from copilot.models.schemas import TICKET_CATEGORIES
from copilot.services.bedrock import BedrockClient
from copilot.services.hub import PanelHub
from copilot.services.sessions import CallSession
from copilot.services.storage import CallStorage
from copilot.utils.helpers import read_text, render_prompt

log = logging.getLogger(__name__)
SYSTEM = "You write concise after-call notes and reply with JSON only."
EMPTY_DRAFT = {
    "problem": "",
    "checked": "",
    "outcome": "",
    "follow_up": "",
    "category": "Other",
    "subject": "",
}


class CallSummarizer:
    def __init__(
        self, settings: Settings, bedrock: BedrockClient, hub: PanelHub, storage: CallStorage
    ) -> None:
        self._model = settings.bedrock_model_summary
        self._bedrock, self._hub, self._storage = bedrock, hub, storage
        self._template = read_text(settings.data_dir / "prompts" / "summary.md")

    async def run(self, session: CallSession) -> None:
        draft = dict(EMPTY_DRAFT)
        if session.turns:
            draft.update(await self._generate(session))
        if draft["category"] not in TICKET_CATEGORIES:
            draft["category"] = "Other"
        session.summary = draft
        await self._storage.save(session.to_record())
        await self._hub.broadcast("summary", draft)

    async def _generate(self, session: CallSession) -> dict:
        prompt = render_prompt(
            self._template,
            customer=session.customer,
            transcript=session.transcript_text(),
            categories=", ".join(TICKET_CATEGORIES),
        )
        try:
            result = await self._bedrock.complete_json(self._model, SYSTEM, prompt, max_tokens=700)
        except Exception:
            log.exception("Summary generation failed. The agent can fill the draft by hand.")
            return {}
        return {k: str(v) for k, v in result.items() if k in EMPTY_DRAFT}
