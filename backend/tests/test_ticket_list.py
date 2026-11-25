"""The tickets list: filters by mobile number, ticket number, date range and status (open by default)."""
from datetime import date

import pytest

from copilot.services.ticket_filter import ALL, filter_tickets, ticket_date
from copilot.services.pipeline import CallPipeline


def ticket(number: int, status="open", created="2026-11-20T10:00:00+00:00", mobile="9500799295", **extra) -> dict:
    return {"customer_id": "c", "ticket_id": f"TKT-{number}", "status": status, "created_at": created,
            "mobile_number": mobile, **extra}


TICKETS = [
    ticket(1, "open", "2026-11-20T10:00:00+00:00"),
    ticket(2, "closed", "2026-11-21T10:00:00+00:00"),
    ticket(3, "resolved", "2026-11-22T10:00:00+00:00", mobile="01712345678"),
    ticket(4, "in_progress", "2026-11-23T10:00:00+00:00", mobile="", caller_number="+8801912345678"),
    ticket(5, "open", "2026-11-24T10:00:00+00:00", mobile="01712345678"),
]


def ids(found) -> list[str]:
    return [t["ticket_id"] for t in found[0]]


def test_only_open_tickets_by_default_newest_first():
    assert ids(filter_tickets(TICKETS)) == ["TKT-5", "TKT-1"]


@pytest.mark.parametrize("status,expected", [
    ("closed", ["TKT-2"]), ("resolved", ["TKT-3"]), ("in_progress", ["TKT-4"]),
    (ALL, ["TKT-5", "TKT-4", "TKT-3", "TKT-2", "TKT-1"]),
])
def test_status_filter(status, expected):
    assert ids(filter_tickets(TICKETS, status=status)) == expected


def test_a_ticket_without_a_status_counts_as_open():
    assert ids(filter_tickets([{"ticket_id": "TKT-9", "created_at": "2026-11-20T10:00:00+00:00"}])) == ["TKT-9"]


@pytest.mark.parametrize("typed", ["9500799295", "+919500799295", "91 95007 99295", "09500799295", "95007"])
def test_mobile_filter_accepts_any_format_or_part_of_the_number(typed):
    assert ids(filter_tickets(TICKETS, mobile=typed, status=ALL)) == ["TKT-2", "TKT-1"]


def test_mobile_filter_also_matches_the_caller_id_of_an_unidentified_ticket():
    assert ids(filter_tickets(TICKETS, mobile="01912345678", status=ALL)) == ["TKT-4"]
    assert ids(filter_tickets(TICKETS, mobile="+880 1712 345678", status=ALL)) == ["TKT-5", "TKT-3"]


@pytest.mark.parametrize("typed", ["TKT-3", "tkt-3", "3", " 3 "])
def test_ticket_number_filter_is_a_case_blind_part_of_the_number(typed):
    assert ids(filter_tickets(TICKETS, ticket_no=typed, status=ALL)) == ["TKT-3"]


def test_date_range_is_inclusive_and_uses_bangladesh_dates():
    found = filter_tickets(TICKETS, status=ALL, date_from=date(2026, 11, 21), date_to=date(2026, 11, 23))
    assert ids(found) == ["TKT-4", "TKT-3", "TKT-2"]
    assert ids(filter_tickets(TICKETS, status=ALL, date_from=date(2026, 11, 24))) == ["TKT-5"]
    assert ids(filter_tickets(TICKETS, status=ALL, date_to=date(2026, 11, 20))) == ["TKT-1"]
    # 20:00 UTC on 20 Nov is 02:00 on 21 Nov in Bangladesh
    late = [ticket(7, created="2026-11-20T20:00:00+00:00")]
    assert ticket_date(late[0]) == date(2026, 11, 21)
    assert ids(filter_tickets(late, date_from=date(2026, 11, 21), date_to=date(2026, 11, 21))) == ["TKT-7"]


def test_filters_combine():
    found = filter_tickets(TICKETS, mobile="9500799295", status="open", date_from=date(2026, 11, 20), date_to=date(2026, 11, 20))
    assert ids(found) == ["TKT-1"]


def test_limit_keeps_the_newest_and_reports_the_total():
    found, total = filter_tickets(TICKETS, status=ALL, limit=2)
    assert [t["ticket_id"] for t in found] == ["TKT-5", "TKT-4"] and total == 5


def test_a_ticket_with_no_readable_date_is_left_out_of_a_date_range():
    assert ids(filter_tickets([{"ticket_id": "TKT-8", "status": "open"}], date_from=date(2026, 1, 1))) == []


# ---- the route ----
def test_the_list_shows_only_open_tickets_until_another_status_is_asked_for(client):
    default = client.get("/api/tickets").json()
    assert default["total"] == 5 and {t["status"] for t in default["tickets"]} == {"open"}
    closed = client.get("/api/tickets?status=closed").json()["tickets"]
    assert [t["ticket_id"] for t in closed] == ["TKT-10098"]
    assert client.get("/api/tickets?status=all").json()["total"] == 7


def test_the_list_filters_by_mobile_number_ticket_number_and_dates(client):
    body = client.get("/api/tickets?status=all&mobile=%2B919500799295").json()
    assert {t["ticket_id"] for t in body["tickets"]} == {"TKT-10150", "TKT-10187", "TKT-10098", "TKT-10122"}
    assert [t["ticket_id"] for t in client.get("/api/tickets?status=all&ticket=10144").json()["tickets"]] == ["TKT-10144"]
    nothing = client.get("/api/tickets?status=all&from_date=2030-01-01").json()
    assert nothing["tickets"] == [] and nothing["total"] == 0


@pytest.mark.parametrize("query", ["status=nope", "from_date=not-a-date", "from_date=2026-11-25&to_date=2026-11-01", "limit=0"])
def test_bad_filters_are_refused(client, query):
    assert client.get(f"/api/tickets?{query}").status_code == 422


def test_the_statuses_the_filter_offers(client):
    assert client.get("/api/tickets/statuses").json() == {
        "statuses": ["open", "in_progress", "resolved", "closed"], "default": "open"}


async def test_a_new_ticket_carries_the_customers_own_number_and_name(client, container):
    pipeline = CallPipeline(container)
    await pipeline.begin(call_sid=None, caller="+919500799295", source="typed")
    client.app.state.container = container  # the route reads the same call as this pipeline
    made = client.post("/api/tickets", json={"call_sid": pipeline.session.call_sid, "customer_id": "abdul-rahim",
                                              "category": "Network", "subject": "Slow", "problem": "Slow"}).json()
    assert made["mobile_number"] == "9500799295" and made["customer_name"] == "Abdul Rahim"
    assert made["caller_number"] == "+919500799295"
    listed = client.get("/api/tickets?mobile=9500799295").json()["tickets"]
    assert made["ticket_id"] in [t["ticket_id"] for t in listed]
