"""Filtering the tickets list: by mobile number, ticket number, a date range and the status.

Pure functions, so they are tested without AWS. The list is small (a scan of the tickets table); a larger
system would query an index instead.
"""
from datetime import date, datetime, timedelta, timezone

from copilot.services.customers import is_plausible_number, normalize_number

BANGLADESH_TIME = timezone(timedelta(hours=6))  # dates in the filter are Bangladesh dates
STATUSES = ("open", "in_progress", "resolved", "closed")
ALL = "all"
DEFAULT_STATUS = "open"
DEFAULT_LIMIT = 200


def ticket_date(ticket: dict) -> date | None:
    """The Bangladesh calendar date the ticket was created on, or None when it cannot be read."""
    try:
        created = datetime.fromisoformat(ticket.get("created_at") or "")
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created.astimezone(BANGLADESH_TIME).date()


def _matches_number(ticket: dict, wanted: str) -> bool:
    """`wanted` is a full number (any format) or some digits of one. A ticket has the customer's number, or the
    caller ID when no customer was identified."""
    digits = "".join(ch for ch in wanted if ch.isdigit())
    if not digits:
        return True
    needle = normalize_number(wanted) if is_plausible_number(wanted) else digits
    for field in ("mobile_number", "caller_number"):
        if needle in normalize_number(ticket.get(field) or ""):
            return True
    return False


def filter_tickets(
    tickets: list[dict],
    *,
    mobile: str = "",
    ticket_no: str = "",
    date_from: date | None = None,
    date_to: date | None = None,
    status: str = DEFAULT_STATUS,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[dict], int]:
    """The tickets that match every filter given, newest first, and how many matched before `limit`."""
    wanted_no = ticket_no.strip().lower()
    matched = []
    for ticket in tickets:
        if status != ALL and ticket.get("status", "open") != status:
            continue
        if wanted_no and wanted_no not in str(ticket.get("ticket_id", "")).lower():
            continue
        if not _matches_number(ticket, mobile):
            continue
        day = ticket_date(ticket)
        if (date_from or date_to) and day is None:
            continue
        if date_from and day < date_from:
            continue
        if date_to and day > date_to:
            continue
        matched.append(ticket)
    matched.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    return matched[:limit], len(matched)
