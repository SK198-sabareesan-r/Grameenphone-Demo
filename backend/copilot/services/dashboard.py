"""Adoption dashboard. Every number is computed from the saved calls (the calls table) when it is asked for.

Nothing is seeded or estimated. A figure that cannot be measured is not shown. A call counts as using the Copilot
when it has at least one transcript line.
"""
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from copilot.services.storage import CallStorage
from copilot.services.ticket_filter import BANGLADESH_TIME

TREND_DAYS = 7
MAX_TREND_DAYS = 31  # a longer date range shows the mood of its last 31 days (the numbers above it cover all of it)
TOP_SUGGESTIONS = 5
PROCESS_STEP = "process"  # guide.py: "confirm the number / the location" steps, shown on nearly every call
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


def mood_trend(records: list[dict], today: date, days_count: int = TREND_DAYS) -> list[dict]:
    """One row per day for the `days_count` days ending `today`, oldest first. Percentages are of the calls that day
    with a mood."""
    days = [today - timedelta(days=n) for n in range(days_count - 1, -1, -1)]
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


def top_suggestions(records: list[dict], titles: dict[str, str] | None = None) -> list[dict]:
    """The suggestions shown most often, with how many were used, over all saved calls. A catalog suggestion is
    counted under its English title (`titles`, by id), whatever language it was shown in; others by their title."""
    titles = titles or {}
    shown: Counter = Counter()
    used: Counter = Counter()
    for record in records:
        for suggestion in record.get("suggestions") or []:
            if suggestion.get("source") == PROCESS_STEP:
                continue  # the fixed first steps of a call are not Copilot suggestions
            title = titles.get(suggestion.get("id") or "") or suggestion.get("title") or suggestion.get("id")
            if not title:
                continue
            shown[title] += 1
            used[title] += 1 if suggestion.get("used") else 0
    ranked = sorted(shown, key=lambda t: (-shown[t], t))[:TOP_SUGGESTIONS]
    return [{"title": t, "shown": shown[t], "used": used[t]} for t in ranked]


class DashboardService:
    def __init__(self, storage: CallStorage, titles: dict[str, str] | None = None) -> None:
        self._storage = storage
        self._titles = titles or {}  # suggestion id -> English catalog title

    async def build(
        self, now: datetime | None = None, *, from_date: date | None = None, to_date: date | None = None
    ) -> dict:
        """All saved calls, or only those that started on a Bangladesh date in [from_date, to_date] (both inclusive,
        either may be left out). The mood trend covers the chosen days, at most MAX_TREND_DAYS, or the last 7 days."""
        all_records = await self._storage.all_records()
        records = [r for r in all_records if r.get("turns")]  # a call with no transcript line did not use the Copilot
        today = (now or datetime.now(timezone.utc)).astimezone(BANGLADESH_TIME).date()
        if from_date or to_date:
            records = [r for r in records if _in_range(_day(r.get("started_at")), from_date, to_date)]
        # The trend ends on the last chosen day, but never after today (future days have no calls)...
        trend_end = today if to_date is None else min(to_date, today)
        if from_date and from_date > trend_end:  # ...unless the whole range is in the future
            trend_end = to_date or from_date
        trend_start = min(from_date or (trend_end - timedelta(days=TREND_DAYS - 1)), trend_end)
        trend_days = min(MAX_TREND_DAYS, (trend_end - trend_start).days + 1)
        suggestions = [s for r in records for s in r.get("suggestions") or [] if s.get("source") != PROCESS_STEP]
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
            "mood_trend": mood_trend(records, trend_end, trend_days),
            "top_suggestions": top_suggestions(records, self._titles),
            "range": {
                "from_date": from_date.isoformat() if from_date else None,
                "to_date": to_date.isoformat() if to_date else None,
                "trend_days": trend_days,
            },
        }


def _in_range(day: date | None, from_date: date | None, to_date: date | None) -> bool:
    if day is None:
        return False  # no readable start time: cannot be placed in a date range
    return (from_date is None or day >= from_date) and (to_date is None or day <= to_date)
