"""Automatic lookup for each customer sentence.

One Bedrock call per lookup. The knowledge articles that match the sentence, the customer card and the
network notice are gathered first and given to the model together. The model decides whether the sentence
needs a lookup at all (greetings and small talk do not) and writes a short suggestion for the agent.
If Bedrock fails, the matching articles are shown on their own, so the agent still gets something.
"""

import asyncio
import logging
import re

from config.settings import Settings
from copilot.repo.base import DataUnavailable, Repo
from copilot.services.bedrock import BedrockClient
from copilot.services.hub import PanelHub
from copilot.services.kb_retriever import KnowledgeRetriever, KnowledgeUnavailable
from copilot.services.knowledge import Article
from copilot.services.sessions import CallSession, Turn
from copilot.utils.helpers import load_json, read_text, render_prompt

log = logging.getLogger(__name__)

SYSTEM = "You help a contact-centre agent during a live call and reply with JSON only."
NOT_FOUND = "Not found in the approved knowledge or the customer's data. Check with a supervisor."
CONFIDENCE_LEVELS = {"high", "medium", "low"}
MAX_ANSWER_CHARS = 600
SHORT_SENTENCE_WORDS = 5  # shorter sentences are searched together with the customer's previous one
_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_TRAILING_BRACKET = re.compile(r"\s*[(\[][^()\[\]]*[)\]]\s*$")  # a translation the model added after the question


def _words(text: str) -> list[str]:
    return _PUNCTUATION.sub(" ", text.lower()).split()


class LiveAssist:
    def __init__(
        self, settings: Settings, bedrock: BedrockClient, kb: KnowledgeRetriever, hub: PanelHub, repo: Repo
    ) -> None:
        self._enabled = settings.assist_enabled
        self._model = settings.bedrock_inference_profile_arn_fast
        self._timeout_s = settings.assist_timeout_s
        self._limit = settings.assist_max_articles
        self._bedrock, self._kb, self._hub, self._repo = bedrock, kb, hub, repo
        self._template = read_text(settings.data_dir / "prompts" / "live_assist.md")
        smalltalk = load_json(settings.data_dir / "mock" / "smalltalk.json")
        self._max_words = int(smalltalk["max_words"])
        self._phrases = {" ".join(_words(p)) for p in smalltalk["phrases"]}
        self._known_words = {w for p in self._phrases for w in p.split()}
        self._running: set[str] = set()
        self._pending: dict[str, int] = {}  # call id -> newest customer turn waiting its turn

    # ---- deciding whether to look anything up ----

    def is_small_talk(self, text: str) -> bool:
        words = _words(text)
        if not words:
            return True
        if " ".join(words) in self._phrases:
            return True
        return len(words) <= self._max_words and all(w in self._known_words for w in words)

    def schedule(self, session: CallSession, turn: Turn) -> None:
        """Look up the customer's sentence in the background.

        One lookup runs at a time per call. Sentences that arrive meanwhile wait, and only the newest
        waiting one is kept, because the model also sees the recent transcript.
        """
        if not self._enabled or turn.speaker != "customer" or self.is_small_talk(turn.text):
            return
        sid = session.call_sid
        if sid in self._running:
            self._pending[sid] = turn.id
            return
        self._running.add(sid)
        asyncio.create_task(self._loop(session, turn))

    async def run_now(self, session: CallSession) -> dict | None:
        """Look up the customer's latest sentence on request, even if it looks like small talk."""
        turn = next((t for t in reversed(session.turns) if t.speaker == "customer"), None)
        if turn is None:
            return None
        return await self._handle(session, turn)

    async def _loop(self, session: CallSession, turn: Turn | None) -> None:
        sid = session.call_sid
        try:
            while turn is not None:
                try:
                    await self._handle(session, turn)
                except Exception:
                    log.exception("Live lookup failed for call %s", sid)
                waiting = self._pending.pop(sid, None)
                turn = next((t for t in session.turns if t.id == waiting), None) if waiting else None
        finally:
            self._running.discard(sid)

    # ---- one lookup ----

    async def _handle(self, session: CallSession, turn: Turn) -> dict:
        await self._hub.broadcast("answer.pending", {"turn_id": turn.id, "question": turn.text})
        # The knowledge search and the packs and GP Centres read are independent, so they run together.
        (articles, kb_down), (packs, centres) = await asyncio.gather(
            self._search(self._query(session, turn)), self._reference(session)
        )
        entry = await self._generate(session, turn, articles, packs, centres, kb_down)
        session.set_answer(entry)
        await self._hub.broadcast("answer", entry)
        return entry

    async def _search(self, query: str) -> tuple[list[Article], bool]:
        """The matching articles, and whether the Knowledge Base could not be searched. The lookup carries on without it."""
        try:
            return await self._kb.search(query, self._limit), False
        except KnowledgeUnavailable as error:
            log.warning("The lookup has no knowledge: %s", error)
            return [], True

    async def _reference(self, session: CallSession) -> tuple[list[dict], list[dict]]:
        """The pack catalog, and the GP Centres in the customer's registered area and current location."""
        areas = [(session.customer or {}).get("area"), (session.current_location or {}).get("area")]
        areas = list(dict.fromkeys(a for a in areas if a))
        try:
            packs, *by_area = await asyncio.gather(
                self._repo.list_packs(), *[self._repo.list_centres(a) for a in areas]
            )
        except DataUnavailable as error:
            log.warning("Packs and GP Centres are not available for this lookup: %s", error)
            return [], []
        return packs, [c for centres in by_area for c in centres]

    def _query(self, session: CallSession, turn: Turn) -> str:
        if len(turn.text.split()) >= SHORT_SENTENCE_WORDS:
            return turn.text
        previous = next(
            (t.text for t in reversed(session.turns) if t.speaker == "customer" and t.id < turn.id), ""
        )
        return f"{previous} {turn.text}".strip()

    async def _generate(
        self, session: CallSession, turn: Turn, articles: list[Article], packs: list[dict], centres: list[dict],
        kb_down: bool = False,
    ) -> dict:
        prompt = render_prompt(
            self._template,
            number_confirmed="yes" if session.identity_confirmed else "no",
            identity_verified="yes" if session.compliance.get("verify_identity") == "green" else "no",
            customer=session.customer,
            network=session.network_view(),
            packs=self._packs_block(packs),
            centres=self._centres_block(centres),
            articles=self._articles_block(articles, kb_down),
            transcript=session.transcript_text(last=8),
            question=turn.text,
        )
        try:
            raw = await asyncio.wait_for(
                self._bedrock.complete_json(self._model, SYSTEM, prompt, max_tokens=500),
                self._timeout_s,
            )
            return self._clean(raw, turn, articles, session)
        except Exception:
            log.exception("Bedrock lookup failed, showing the matching articles only")
            return self._search_only(turn, articles)

    @staticmethod
    def _packs_block(packs: list[dict]) -> str:
        if not packs:
            return "(none)"
        lines = []
        for p in sorted(packs, key=lambda p: p["pack_id"]):
            code = p.get("activation_code") or "none published"
            flag = " [sample offer]" if p.get("sample_offer") else ""
            lines.append(
                f"- {p['name']}: Tk {p['price_bdt']}, {p['validity_days']} days, activation code: {code}{flag}"
            )
        return "\n".join(lines)

    @staticmethod
    def _centres_block(centres: list[dict]) -> str:
        if not centres:
            return "(none)"
        return "\n".join(
            f"- {c['name']} ({c['area']}): {c['address']}, open {c.get('hours') or 'hours not listed'}"
            + (" [sample placeholder, not a real address]" if c.get("type") == "sample" else "")
            for c in centres
        )

    @staticmethod
    def _articles_block(articles: list[Article], kb_down: bool = False) -> str:
        if kb_down:
            return "(the knowledge base could not be reached. Do not answer from memory: say the knowledge is not available right now and use only the customer card and the network status.)"
        if not articles:
            return "(no matching article)"
        return "\n\n".join(f"[{a.id}] {a.title} (type: {a.type})\n{a.body}" for a in articles)

    # ---- shaping the result ----

    def _clean(self, raw: dict, turn: Turn, articles: list[Article], session: CallSession | None = None) -> dict:
        if raw.get("needs_lookup") is not True:
            return {"turn_id": turn.id, "question": turn.text, "needs_lookup": False}
        answer = self._shorten(str(raw.get("answer") or "").strip())
        clarify = self._clean_question(raw.get("clarifying_question"), turn, session)
        allowed = {a.id: a for a in articles}
        listed = raw.get("sources") if isinstance(raw.get("sources"), list) else []
        ids = [i for i in dict.fromkeys(listed) if isinstance(i, str) and i in allowed]
        confidence = raw.get("confidence")
        return {
            "turn_id": turn.id,
            "question": turn.text,
            "needs_lookup": True,
            "answer": answer or (None if clarify else NOT_FOUND),
            "sources": [allowed[i].public_view() for i in ids],
            "used_customer_data": raw.get("used_customer_data") is True,
            "confidence": confidence if confidence in CONFIDENCE_LEVELS else "low",
            "clarifying_question": clarify,
            "mode": "ai",
        }

    @staticmethod
    def _shorten(answer: str) -> str:
        """Cut a long answer at the end of a sentence, not in the middle of a word or a code."""
        if len(answer) <= MAX_ANSWER_CHARS:
            return answer
        cut = answer[:MAX_ANSWER_CHARS]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "), cut.rfind("\u0964 "))  # \u0964 is the Bangla full stop
        if end >= MAX_ANSWER_CHARS // 2:
            return cut[: end + 1]
        return cut.rsplit(" ", 1)[0].rstrip(" ,;:") + "\u2026"
    @staticmethod
    def _clean_question(value, turn: Turn, session: CallSession | None) -> str | None:
        """The "Ask" line. Drops a bracketed translation, and drops a question the panel already showed on this call,
        so the agent is not told to ask the same thing again and again."""
        if not isinstance(value, str):
            return None
        question = _TRAILING_BRACKET.sub("", value.strip()).strip()
        if not question:
            return None
        asked = {" ".join(_words(a["clarifying_question"])) for a in (session.answers if session else [])
                 if a.get("clarifying_question") and a.get("turn_id") != turn.id}
        return None if " ".join(_words(question)) in asked else question
    @staticmethod
    def _search_only(turn: Turn, articles: list[Article]) -> dict:
        if not articles:
            return {"turn_id": turn.id, "question": turn.text, "needs_lookup": False}
        return {
            "turn_id": turn.id,
            "question": turn.text,
            "needs_lookup": True,
            "answer": None,
            "sources": [a.public_view() for a in articles],
            "used_customer_data": False,
            "confidence": "low",
            "clarifying_question": None,
            "mode": "search_only",
        }
