"""The adoption dashboard: every number is computed from the saved calls, none is seeded or estimated."""
from datetime import datetime, timedelta, timezone

import pytest

from copilot.services.dashboard import BANGLADESH_TIME, DashboardService, call_mood, mood_trend, top_suggestions

NOW = datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc)  # 15:00 on 26 Nov in Bangladesh
TODAY = NOW.astimezone(BANGLADESH_TIME).date()


def at(days_ago: int, hour: int = 10) -> float:
    """A start time on a Bangladesh date `days_ago` days before today."""
    day = datetime(TODAY.year, TODAY.month, TODAY.day, hour, tzinfo=BANGLADESH_TIME) - timedelta(days=days_ago)
    return day.timestamp()


def sug(title: str, used: bool = False) -> dict:
    return {"id": title.lower(), "title": title, "used": used}


def call(call_id: str, days_ago: int = 0, moods=("neutral",), suggestions=(), ticket=None, turns=3) -> dict:
    return {"call_id": call_id, "started_at": at(days_ago), "turns": [{"id": i} for i in range(turns)],
            "mood_history": list(moods), "suggestions": list(suggestions), "ticket": ticket}


class Storage:
    def __init__(self, records) -> None:
        self.records = records

    async def all_records(self):
        return self.records

    def source_status(self):
        return {"backend": "dynamodb", "fallback_active": False, "reason": None}


async def build(records):
    return await DashboardService(Storage(records)).build(now=NOW)


async def test_no_calls_means_zeros_not_sample_numbers():
    data = await build([])
    assert data["kpis"] == {"calls_with_copilot": 0, "suggestions_shown": 0, "suggestions_accepted": 0,
                            "acceptance_rate": None, "tickets_raised": 0}
    assert data["top_suggestions"] == []
    assert len(data["mood_trend"]) == 7 and all(row["calls"] == 0 and row["rated"] == 0 for row in data["mood_trend"])
    assert "label" not in data and "live" not in data and "avg_handle_time_saved_sec" not in data["kpis"]


async def test_the_kpis_are_counted_from_the_saved_calls():
    records = [
        call("a", suggestions=[sug("Offer pack", True), sug("Apologise", False)], ticket={"ticket_id": "TKT-1"}),
        call("b", suggestions=[sug("Apologise", True)]),
        call("c", suggestions=[], ticket={"ticket_id": "TKT-2"}),
    ]
    kpis = (await build(records))["kpis"]
    assert kpis == {"calls_with_copilot": 3, "suggestions_shown": 3, "suggestions_accepted": 2,
                    "acceptance_rate": 67, "tickets_raised": 2}


async def test_a_call_with_no_transcript_line_did_not_use_the_copilot():
    data = await build([call("a"), call("empty", turns=0), {"call_id": "no-turns-key", "started_at": at(0)}])
    assert data["kpis"]["calls_with_copilot"] == 1


async def test_the_source_of_the_numbers_is_reported():
    assert (await build([]))["source"] == {"backend": "dynamodb", "fallback_active": False, "reason": None}


def test_a_calls_mood_is_the_worst_one_seen():
    assert call_mood({"mood_history": ["neutral", "frustrated", "angry", "calm"]}) == "angry"
    assert call_mood({"mood_history": ["neutral", "frustrated"]}) == "frustrated"
    assert call_mood({"mood_history": ["neutral", "calm"]}) == "calm"
    assert call_mood({"mood_history": []}) is None and call_mood({}) is None


def test_the_trend_has_seven_days_oldest_first_with_percentages_of_rated_calls():
    records = [
        call("1", 0, ["angry"]), call("2", 0, ["neutral"]), call("3", 0, ["frustrated"]), call("4", 0, ["calm"]),
        call("5", 0, []),  # no mood read: counted as a call, left out of the percentages
        call("6", 2, ["angry"]),
        call("7", 9, ["angry"]),  # older than the window
    ]
    rows = mood_trend(records, TODAY)
    assert [r["date"] for r in rows][-1] == TODAY.isoformat() and len(rows) == 7
    today = rows[-1]
    assert (today["calls"], today["rated"]) == (5, 4)
    assert (today["calm"], today["frustrated"], today["angry"]) == (50, 25, 25)
    assert (today["calm_calls"], today["frustrated_calls"], today["angry_calls"]) == (2, 1, 1)
    two_days_ago = rows[-3]
    assert (two_days_ago["calls"], two_days_ago["angry"]) == (1, 100)
    assert sum(r["calls"] for r in rows) == 6  # the call from 9 days ago is not in the window
    assert rows[-2]["calls"] == 0 and rows[-2]["calm"] == rows[-2]["angry"] == 0


def test_a_call_belongs_to_its_bangladesh_date():
    # 20:00 UTC on 25 Nov is 02:00 on 26 Nov in Bangladesh
    late = {"call_id": "x", "started_at": datetime(2026, 11, 25, 20, 0, tzinfo=timezone.utc).timestamp(), "mood_history": ["calm"]}
    rows = mood_trend([late], TODAY)
    assert rows[-1]["calls"] == 1 and rows[-2]["calls"] == 0


@pytest.mark.parametrize("started", [None, "soon", float("inf"), -1e30])
def test_a_call_without_a_readable_start_time_is_left_out_of_the_trend(started):
    assert sum(r["calls"] for r in mood_trend([{"started_at": started, "mood_history": ["calm"]}], TODAY)) == 0


def test_top_suggestions_count_how_often_each_was_shown_and_used():
    records = [
        {"suggestions": [sug("Apologise", True), sug("Offer pack", False)]},
        {"suggestions": [sug("Apologise", False)]},
        {"suggestions": [sug("Apologise", True), sug("Block SIM", True)]},
    ]
    assert top_suggestions(records) == [
        {"title": "Apologise", "shown": 3, "used": 2},
        {"title": "Block SIM", "shown": 1, "used": 1},
        {"title": "Offer pack", "shown": 1, "used": 0},
    ]


def test_top_suggestions_are_limited_to_five():
    records = [{"suggestions": [sug(f"S{i}") for i in range(8)]}]
    assert len(top_suggestions(records)) == 5


# ---- through the real app ----
def test_the_route_reflects_a_call_that_was_made(client):
    assert client.get("/api/dashboard").json()["kpis"]["calls_with_copilot"] == 0
    client.post("/api/transcript", json={"speaker": "customer", "text": "Net kaj korche na."})
    client.post("/api/calls/current/end")
    for _ in range(40):
        body = client.get("/api/dashboard").json()
        if body["kpis"]["calls_with_copilot"] == 1 and body["mood_trend"][-1]["calls"] == 1:
            break
    assert body["kpis"]["calls_with_copilot"] == 1
    assert body["mood_trend"][-1]["calls"] == 1
    assert body["source"]["backend"] in ("files", "dynamodb")
