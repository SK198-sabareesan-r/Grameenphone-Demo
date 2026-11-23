"""Adoption dashboard: seeded sample numbers plus live numbers from this machine's calls."""

from collections import Counter

from config.settings import Settings
from copilot.services.storage import CallStorage
from copilot.utils.helpers import load_json


class DashboardService:
    def __init__(self, settings: Settings, storage: CallStorage) -> None:
        self._seed = load_json(settings.data_dir / "mock" / "dashboard_seed.json")
        self._storage = storage

    def build(self) -> dict:
        records = self._storage.all_records()
        shown = sum(len(r["suggestions"]) for r in records)
        used = sum(1 for r in records for s in r["suggestions"] if s["used"])
        moods = Counter(m for r in records for m in r["mood_history"])
        seed = self._seed
        return {
            "label": seed["label"],
            "kpis": {
                "calls_with_copilot": seed["calls_with_copilot"] + len(records),
                "suggestions_shown": seed["suggestions_shown"] + shown,
                "suggestions_accepted": seed["suggestions_accepted"] + used,
                "avg_handle_time_saved_sec": seed["avg_handle_time_saved_sec"],
            },
            "mood_trend": seed["mood_trend"],
            "top_suggestions": seed["top_suggestions"],
            "live": {"calls": len(records), "moods": dict(moods)},
        }
