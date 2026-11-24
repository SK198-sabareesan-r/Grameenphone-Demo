"""Rule-based compliance reminders. Predictable on purpose, so the demo behaves the same each run.

Rules are about what people say. Speakers in the rule file ("agent", "customer") are normalised to
participant roles at load time, and AI-agent speech never triggers or satisfies a rule.
"""

from pathlib import Path

from copilot.models.participants import COMPLIANCE_SPEAKERS, normalize_speaker
from copilot.utils.helpers import contains_any, load_json


class ComplianceEngine:
    def __init__(self, rules_path: Path) -> None:
        self.rules: list[dict] = load_json(rules_path)
        for rule in self.rules:
            for part in ("trigger", "satisfy"):
                rule[part]["speaker"] = normalize_speaker(rule[part]["speaker"])
                if rule[part]["speaker"] not in COMPLIANCE_SPEAKERS:
                    raise ValueError(f"compliance rule {rule['id']}: {part} speaker must be a person")

    def initial_states(self) -> dict[str, str]:
        return {rule["id"]: "idle" for rule in self.rules}

    def evaluate(self, states: dict[str, str], speaker: str, text: str) -> bool:
        """Update states in place from one spoken turn. Returns True if anything changed."""
        speaker = normalize_speaker(speaker)
        if speaker not in COMPLIANCE_SPEAKERS:
            return False
        changed = False
        for rule in self.rules:
            new_state = self._next_state(rule, states[rule["id"]], speaker, text)
            if new_state != states[rule["id"]]:
                states[rule["id"]] = new_state
                changed = True
        return changed

    @staticmethod
    def _next_state(rule: dict, state: str, speaker: str, text: str) -> str:
        if state == "green":
            return state
        satisfy, trigger = rule["satisfy"], rule["trigger"]
        can_satisfy = state in ("red", "pending") or rule.get("satisfy_before_trigger")
        if can_satisfy and speaker == satisfy["speaker"] and contains_any(text, satisfy["keywords"]):
            return "green"
        if state == "idle" and speaker == trigger["speaker"] and contains_any(text, trigger["keywords"]):
            return rule["trigger_state"]
        return state

    def view(self, states: dict[str, str]) -> list[dict]:
        """Rules that should be visible in the panel, with their message."""
        items = []
        for rule in self.rules:
            state = states.get(rule["id"], "idle")
            if state == "idle":
                continue
            message = rule["message_done"] if state == "green" else rule["message_active"]
            items.append({"id": rule["id"], "label": rule["label"], "state": state, "message": message})
        return items
