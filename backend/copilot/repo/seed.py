"""The mock data as DynamoDB items. Used by the seed script and by the local-file repo."""

import json
import re
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from config.settings import Settings
from copilot.services.customers import normalize_number

# table short name -> (partition key, sort key)
TABLES: dict[str, tuple[str, str | None]] = {
    "customers": ("mobile_number", None),
    "network-areas": ("area", None),
    "tickets": ("customer_id", "ticket_id"),
    "calls": ("call_id", None),
    "packs": ("pack_id", None),
    "gp-centres": ("area", "name"),
}
TOPIC_CATEGORY = {"Slow internet": "Network"}


def _load(path: Path):
    """Read JSON. Numbers with decimals become Decimal, because DynamoDB does not accept float."""
    return json.loads(path.read_text(encoding="utf-8-sig"), parse_float=Decimal)


def customer_items(mock_dir: Path, phone_map: dict[str, str]) -> list[dict]:
    """One item per customer, keyed by mobile number. Open tickets live in the tickets table instead."""
    items, by_id = [], {}
    for customer in _load(mock_dir / "customers.json"):
        item = {k: v for k, v in customer.items() if k not in ("open_tickets", "earlier_tickets", "msisdn") and not k.startswith("_")}
        item["mobile_number"] = normalize_number(customer["msisdn"])
        item["source"] = "mock"
        items.append(item)
        by_id[customer["id"]] = item
    # A verified team phone counts as that customer's mobile number, so it gets its own copy of the item.
    for phone, customer_id in phone_map.items():
        if customer_id in by_id:
            items.append({**by_id[customer_id], "mobile_number": normalize_number(phone), "team_phone": True})
    return items


def area_items(mock_dir: Path) -> list[dict]:
    areas = _load(mock_dir / "network_areas.json")["areas"]
    return [
        {"area": name, "aliases": info.get("aliases", []), "notice": info.get("notice"), "source": "mock"}
        for name, info in areas.items()
    ]


def _ticket_item(customer: dict, ticket: dict, status: str, now: datetime) -> dict:
    days = int(re.match(r"\d+", ticket["age"]).group())
    return {
        "customer_id": customer["id"],
        "ticket_id": ticket["id"],
        "customer_name": customer["name"],
        "mobile_number": normalize_number(customer["msisdn"]),  # the customer's own number, for the tickets list
        "category": ticket.get("category") or TOPIC_CATEGORY.get(ticket["topic"], "Other"),
        "subject": ticket["topic"],
        "problem": ticket["topic"],
        "status": status,
        "created_at": (now - timedelta(days=days)).isoformat(timespec="seconds"),
        "source": "seed",
    }


def ticket_items(mock_dir: Path, now: datetime) -> list[dict]:
    """Earlier complaints from the customer data. Dates are set relative to today.
    open_tickets are open. earlier_tickets carry their own status (resolved or closed)."""
    items = []
    for customer in _load(mock_dir / "customers.json"):
        items += [_ticket_item(customer, t, "open", now) for t in customer.get("open_tickets", [])]
        items += [_ticket_item(customer, t, t["status"], now) for t in customer.get("earlier_tickets", [])]
    return items

def _assert_unique_keys(seed: dict[str, list[dict]]) -> None:
    for short, rows in seed.items():
        pk, sk = TABLES[short]
        keys = [(row[pk], row[sk]) if sk else row[pk] for row in rows]
        if len(keys) != len(set(keys)):
            raise ValueError(f"Duplicate keys in {short}")


def build_seed(settings: Settings, now: datetime) -> dict[str, list[dict]]:
    """Everything to write, by table. No AWS access here."""
    mock = settings.data_dir / "mock"
    seed = {
        "customers": customer_items(mock, settings.phone_map),
        "network-areas": area_items(mock),
        "tickets": ticket_items(mock, now),
        "packs": _load(mock / "packs.json"),
        "gp-centres": _load(mock / "gp_centres.json"),
    }
    _assert_unique_keys(seed)
    return seed
