"""Mobile numbers, and the customer as the panel and the model see it."""

import re
from datetime import datetime, timezone

MIN_DIGITS, MAX_DIGITS = 10, 13
_STORAGE_ONLY = ("mobile_number", "source", "team_phone")


def normalize_number(value: str) -> str:
    """One format for every mobile number, digits only.

    Bangladesh numbers are 11 digits, 01XXXXXXXXX. Accepts +880..., 880... and 1XXXXXXXXX.
    Indian numbers (the demo caller) are 10 digits, [6-9]XXXXXXXXX. Accepts +91..., 91... and a leading 0.
    """
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("880") and len(digits) == 13:
        return "0" + digits[3:]
    if len(digits) == 10 and digits.startswith("1"):
        return "0" + digits
    if digits.startswith("91") and len(digits) == 12 and digits[2] in "6789":
        return digits[2:]
    if len(digits) == 11 and digits[0] == "0" and digits[1] in "6789":
        return digits[1:]
    return digits

def is_plausible_number(value: str) -> bool:
    return MIN_DIGITS <= len(re.sub(r"\D", "", value or "")) <= MAX_DIGITS


def age_text(created_at: str | None, now: datetime) -> str:
    """'today', '1 day' or 'N days' for an ISO timestamp. Empty when it cannot be read."""
    try:
        created = datetime.fromisoformat(created_at or "")
    except ValueError:
        return ""
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    days = (now - created).days
    return "today" if days <= 0 else ("1 day" if days == 1 else f"{days} days")


def customer_view(item: dict, open_tickets: list[dict], now: datetime) -> dict:
    """The customer record without storage-only fields, plus msisdn and the open tickets (oldest first)."""
    view = {k: v for k, v in item.items() if k not in _STORAGE_ONLY}
    view["msisdn"] = item["mobile_number"]
    view["open_tickets"] = [
        {
            "id": t["ticket_id"],
            "topic": t.get("subject") or t.get("problem") or "",
            "age": age_text(t.get("created_at"), now),
        }
        for t in sorted(open_tickets, key=lambda t: t.get("created_at", ""))
    ]
    return view
