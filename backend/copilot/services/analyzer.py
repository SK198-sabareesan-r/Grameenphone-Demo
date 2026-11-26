"""Per-turn analysis: customer mood, agent mood, tip and next-best-action (Amazon Bedrock)."""

import asyncio
import logging

from config.settings import Settings
from copilot.services import language
from copilot.services.bedrock import BedrockClient
from copilot.services.hub import PanelHub
from copilot.services.nba import NbaCatalog
from copilot.services.sessions import CallSession
from copilot.utils.helpers import contains_any, load_json, read_text, render_prompt

log = logging.getLogger(__name__)

CUSTOMER_MOODS = {"calm", "neutral", "frustrated", "angry"}
AGENT_MOODS = {"calm", "neutral", "rushed", "empathetic"}
SYSTEM = "You analyse live customer-service calls and reply with JSON only."
ANGRY_TIP = "Acknowledge the repeated complaint, give a ticket number and a clear time."
# The keyword fallback's tip, in the language the customer speaks.
ANGRY_TIPS = {
    language.ENGLISH: ANGRY_TIP,
    language.BANGLA: "বারবার একই অভিযোগের কথা স্বীকার করুন, একটি টিকেট নম্বর এবং পরিষ্কার সময় জানান।",
    language.BANGLISH: "Barbar eki obhijoger kotha shikar korun, ekta ticket number ar porishkar shomoy janan.",
}


class TurnAnalyzer:
    def __init__(self, settings: Settings, bedrock: BedrockClient, nba: NbaCatalog, hub: PanelHub):
        self._model = settings.bedrock_inference_profile_arn_fast
        self._bedrock, self._nba, self._hub = bedrock, nba, hub
        self._template = read_text(settings.data_dir / "prompts" / "analyze_turn.md")
        self._keywords = load_json(settings.data_dir / "mock" / "mood_keywords.json")
        self._running: set[str] = set()
        self._dirty: set[str] = set()

    def schedule(self, session: CallSession) -> None:
        """Run analysis now, or once more after the one in flight finishes."""
        if session.call_sid in self._running:
            self._dirty.add(session.call_sid)
            return
        self._running.add(session.call_sid)
        asyncio.create_task(self._loop(session))

    async def _loop(self, session: CallSession) -> None:
        try:
            while True:
                self._dirty.discard(session.call_sid)
                await self.run_once(session)
                if session.call_sid not in self._dirty:
                    break
        finally:
            self._running.discard(session.call_sid)

    async def run_once(self, session: CallSession) -> None:
        """Analyse the call as it stands now, update the session and notify the panel."""
        try:
            result = await self._bedrock.complete_json(self._model, SYSTEM, self._prompt(session))
        except Exception:
            log.exception("Bedrock analysis failed, using keyword fallback")
            result = self._fallback(session)
        await self._apply(session, result)

    def _prompt(self, session: CallSession) -> str:
        return render_prompt(
            self._template,
            customer=session.customer,
            network=session.network_view(),
            nba=self._nba.llm_view(),
            shown=list(session.suggestions),
            transcript=session.transcript_text(last=12),
            language=language.NAMES[session.language()],
            language_rule=language.rule(session.language()),
        )

    def _fallback(self, session: CallSession) -> dict:
        last_customer = next((t.text for t in reversed(session.turns) if t.speaker == "customer"), "")
        mood = "neutral"
        if contains_any(last_customer, self._keywords["angry"]):
            mood = "angry"
        elif contains_any(last_customer, self._keywords["frustrated"]):
            mood = "frustrated"
        return {
            "customer_mood": mood,
            "agent_mood": "neutral",
            "tip": language.pick(ANGRY_TIPS, session.language()) if mood == "angry" else None,
            "suggestion_id": None,
        }

    async def _apply(self, session: CallSession, result: dict) -> None:
        customer = result.get("customer_mood")
        agent = result.get("agent_mood")
        session.mood = {
            "customer": customer if customer in CUSTOMER_MOODS else session.mood["customer"],
            "agent": agent if agent in AGENT_MOODS else session.mood["agent"],
            "tip": result.get("tip") if isinstance(result.get("tip"), str) else None,
        }
        session.mood_history.append(session.mood["customer"])
        await self._hub.broadcast("mood", session.mood)
        if await self._nba.show(session, result.get("suggestion_id")):
            await self._hub.broadcast("suggestions", list(session.suggestions.values()))
