"""Adoption dashboard. Every number is computed from the saved calls (the calls table) when it is asked for.

Nothing is seeded or estimated. A figure that cannot be measured is not shown. A call counts as using the Copilot
when it has at least one transcript line.
"""
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from copilot.services.storage import CallStorage
from copilot.services.ticket_filter import BANGLADESH_TIME

TREND_DAYS = 7
TOP_SUGGESTIONS = 5
CALM_MOODS = {"calm", "neutral"}  # shown together as "calm"


def _day(started_at) -> date | None:
    """The Bangladesh calendar date a call started on, or None when it has no readable start time."""
    try:
        return datetime.fromtimestamp(float(started_at), tz=timezone.utc).astimezone(BANGLADESH_TIME).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def call_mood(record: dict) -> str | None:
    """The worst customer mood seen on a call: angry, then frustrated, else calm. None when no mood was read."""
    moods = set(record.get("mood_history") or [])
    if "angry" in moods:
        return "angry"
    if "frustrated" in moods:
        return "frustrated"
    return "calm" if moods & CALM_MOODS else None


def mood_trend(records: list[dict], today: date) -> list[dict]:
    """One row per day for the last TREND_DAYS days, oldest first. Percentages are of the calls that day with a mood."""
    days = [today - timedelta(days=n) for n in range(TREND_DAYS - 1, -1, -1)]
    by_day: dict[date, list[dict]] = {d: [] for d in days}
    for record in records:
        day = _day(record.get("started_at"))
        if day in by_day:
            by_day[day].append(record)
    rows = []
    for day in days:
        moods = Counter(m for m in map(call_mood, by_day[day]) if m)
        rated = sum(moods.values())
        row = {"date": day.isoformat(), "day": day.strftime("%a %d %b"), "calls": len(by_day[day]), "rated": rated}
        for mood in ("calm", "frustrated", "angry"):
            row[mood] = round(100 * moods[mood] / rated) if rated else 0
            row[f"{mood}_calls"] = moods[mood]
        rows.append(row)
    return rows


def top_suggestions(records: list[dict]) -> list[dict]:
    """The suggestions shown most often, with how many were used. Counted by title, over all saved calls."""
    shown: Counter = Counter()
    used: Counter = Counter()
    for record in records:
        for suggestion in record.get("suggestions") or []:
            title = suggestion.get("title") or suggestion.get("id")
            if not title:
                continue
            shown[title] += 1
            used[title] += 1 if suggestion.get("used") else 0
    ranked = sorted(shown, key=lambda t: (-shown[t], t))[:TOP_SUGGESTIONS]
    return [{"title": t, "shown": shown[t], "used": used[t]} for t in ranked]


class DashboardService:
    def __init__(self, storage: CallStorage) -> None:
        self._storage = storage

    async def build(self, now: datetime | None = None) -> dict:
        all_records = await self._storage.all_records()
        records = [r for r in all_records if r.get("turns")]  # a call with no transcript line did not use the Copilot
        today = (now or datetime.now(timezone.utc)).astimezone(BANGLADESH_TIME).date()
        suggestions = [s for r in records for s in r.get("suggestions") or []]
        shown = len(suggestions)
        accepted = sum(1 for s in suggestions if s.get("used"))
        return {
            "source": self._storage.source_status(),
            "kpis": {
                "calls_with_copilot": len(records),
                "suggestions_shown": shown,
                "suggestions_accepted": accepted,
                "acceptance_rate": round(100 * accepted / shown) if shown else None,
                "tickets_raised": sum(1 for r in records if r.get("ticket")),
            },
            "mood_trend": mood_trend(records, today),
            "top_suggestions": top_suggestions(records),
        }
