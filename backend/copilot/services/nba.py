"""Next-best-action catalog and tracking of which suggestions were shown or used.

A suggestion can carry the details it refers to, read from the data store: the pack being offered
(price, validity, activation code) or the GP Centres near the customer.
"""

import logging
from pathlib import Path

from copilot.models.participants import SUGGESTION_SPEAKERS
from copilot.repo.base import DataUnavailable, Repo
from copilot.services import language
from copilot.services.sessions import CallSession
from copilot.utils.helpers import contains_any, load_json

log = logging.getLogger(__name__)
PACK_FIELDS = ("name", "data_gb", "minutes", "price_bdt", "validity_days", "activation_code", "sample_offer")
CENTRE_FIELDS = ("area", "name", "address", "hours", "type")
MAX_CENTRES_PER_AREA = 3
RECENT_TURNS = 4  # how many of the latest lines are checked for a suggestion the agent has already said


class NbaCatalog:
    def __init__(self, rules_path: Path, repo: Repo) -> None:
        self.rules: dict[str, dict] = {r["id"]: r for r in load_json(rules_path)}
        self._repo = repo

    def llm_view(self) -> list[dict]:
        return [{"id": r["id"], "when": r["when"]} for r in self.rules.values()]

    async def show(self, session: CallSession, suggestion_id: str | None) -> bool:
        """Add a suggestion to the session. Returns True when it is new."""
        rule = self.rules.get(suggestion_id or "")
        if not rule or suggestion_id in session.suggestions:
            return False
        lang = session.language()  # shown in the language the customer is speaking (English when no translation)
        suffix = "" if lang == language.ENGLISH else f"_{lang}"
        session.suggestions[rule["id"]] = {
            "id": rule["id"],
            "title": rule.get(f"title{suffix}") or rule["title"],
            "text": rule.get(f"text{suffix}") or rule["text"],
            "source": rule["source"],
            "used": self._said_recently(session, rule),
            **await self._details(session, rule),
        }
        return True

    @staticmethod
    def _said_recently(session: CallSession, rule: dict) -> bool:
        """The analysis can finish after the agent has already said the suggestion, so look back a few lines."""
        recent = session.turns[-RECENT_TURNS:]
        return any(
            t.speaker in SUGGESTION_SPEAKERS and contains_any(t.text, rule["used_keywords"]) for t in recent
        )

    async def _details(self, session: CallSession, rule: dict) -> dict:
        """The pack or the GP Centres this suggestion refers to. If the data store fails, show it without them."""
        details: dict = {}
        try:
            if rule.get("pack_id"):
                pack = await self._repo.get_pack(rule["pack_id"])
                if pack:
                    details["pack"] = {k: pack.get(k) for k in PACK_FIELDS}
            if rule.get("show_centres"):
                details["centres"] = await self._centres(session)
        except DataUnavailable as error:
            log.warning("Suggestion %s is shown without its details: %s", rule["id"], error)
        return details

    async def _centres(self, session: CallSession) -> list[dict]:
        """GP Centres in the customer's registered area, and in the area they are in now."""
        areas = [(session.customer or {}).get("area"), (session.current_location or {}).get("area")]
        centres = []
        for area in dict.fromkeys(a for a in areas if a):
            for centre in (await self._repo.list_centres(area))[:MAX_CENTRES_PER_AREA]:
                centres.append({k: centre.get(k) for k in CENTRE_FIELDS})
        return centres

    def mark_used_by_speech(self, session: CallSession, text: str) -> bool:
        changed = False
        for sid, item in session.suggestions.items():
            if sid not in self.rules:
                continue  # a first step (guide.py) is ticked by what happens, not by words
            keywords = self.rules[sid]["used_keywords"]
            if not item["used"] and contains_any(text, keywords):
                item["used"] = True
                changed = True
        return changed

    def mark_used(self, session: CallSession, suggestion_id: str) -> bool:
        item = session.suggestions.get(suggestion_id)
        if not item or item["used"]:
            return False
        item["used"] = True
        return True
