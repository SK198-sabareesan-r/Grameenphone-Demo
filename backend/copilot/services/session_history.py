"""Session history: the finished calls that were saved when the summary was written and the ticket was raised.
The saved record (CallSession.to_record) is the source: DynamoDB calls table, with the same JSON also kept in the
call-transcripts S3 bucket under calls/<mobile number>/<call id>.json. This module only reshapes records for the panel.
"""
from copilot.container import Container
from copilot.services.dashboard import call_mood

DEFAULT_LIMIT = 50


def _started(record: dict) -> float:
    try:
        return float(record.get("started_at") or 0)
    except (TypeError, ValueError):
        return 0.0


def list_item(record: dict) -> dict:
    summary = record.get("summary") or {}
    ticket = record.get("ticket") or {}
    return {
        "call_id": record["call_id"],
        "customer_name": record.get("customer_name"),
        "mobile_number": record.get("mobile_number") or record.get("caller_number"),
        "started_at": record.get("started_at"),
        "ended_at": record.get("ended_at"),
        "subject": summary.get("subject") or "",
        "category": summary.get("category") or "",
        "ticket_id": ticket.get("ticket_id"),
        "mood": call_mood(record),
        "turn_count": len(record.get("turns") or []),
    }


def list_items(records: list[dict], limit: int = DEFAULT_LIMIT, search: str = "") -> list[dict]:
    """Newest first. Calls with no transcript line are left out. `search` matches the name, number, call id,
    ticket number or subject."""
    needle = search.strip().lower()
    items = []
    for record in sorted(records, key=_started, reverse=True):
        if not record.get("turns") or not record.get("call_id"):
            continue
        item = list_item(record)
        haystack = " ".join(str(item[k] or "") for k in ("call_id", "customer_name", "mobile_number", "ticket_id", "subject"))
        if needle and needle not in haystack.lower():
            continue
        items.append(item)
        if len(items) >= limit:
            break
    return items


def record_view(c: Container, record: dict) -> dict:
    """A saved call in the same shape as session_view, so the panel can draw it with the live-call components."""
    moods = record.get("mood_history") or []
    mood = record.get("mood") or {"customer": moods[-1] if moods else "neutral", "agent": "neutral", "tip": None}
    return {
        "call_sid": record["call_id"],
        "ended": True,
        "status": record.get("status") or "ended",
        "agent_mode": record.get("agent_mode"),
        "turns": record.get("turns") or [],
        "card": record.get("card"),  # calls saved before cards were stored have none
        "caller_number": record.get("caller_number") or "",
        "current_location": record.get("current_location"),
        "location_prompt": False,
        "heard": None,
        "mood": mood,
        "compliance": c.compliance.view(record.get("compliance") or {}),
        "suggestions": record.get("suggestions") or [],
        "answers": record.get("answers") or [],
        "summary": record.get("summary"),
        "ticket": record.get("ticket"),
        "recording_url": record.get("recording_url") or "",
        "started_at": record.get("started_at"),
        "ended_at": record.get("ended_at"),
    }
