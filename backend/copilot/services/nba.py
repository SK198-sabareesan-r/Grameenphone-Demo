"""Next-best-action catalog and tracking of which suggestions were shown or used."""

from pathlib import Path

from copilot.services.sessions import CallSession
from copilot.utils.helpers import contains_any, load_json


class NbaCatalog:
    def __init__(self, rules_path: Path) -> None:
        self.rules: dict[str, dict] = {r["id"]: r for r in load_json(rules_path)}

    def llm_view(self) -> list[dict]:
        return [{"id": r["id"], "when": r["when"]} for r in self.rules.values()]

    def show(self, session: CallSession, suggestion_id: str | None) -> bool:
        """Add a suggestion to the session. Returns True when it is new."""
        rule = self.rules.get(suggestion_id or "")
        if not rule or suggestion_id in session.suggestions:
            return False
        session.suggestions[rule["id"]] = {
            "id": rule["id"],
            "title": rule["title"],
            "text": rule["text"],
            "source": rule["source"],
            "used": False,
        }
        return True

    def mark_used_by_speech(self, session: CallSession, text: str) -> bool:
        changed = False
        for sid, item in session.suggestions.items():
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
