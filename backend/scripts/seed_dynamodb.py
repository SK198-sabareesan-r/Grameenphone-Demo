"""Load the mock data into the DynamoDB tables.

Run from the backend folder:

    python -m scripts.seed_dynamodb --dry-run     show what would be written, change nothing
    python -m scripts.seed_dynamodb               write the data

Safe to repeat: items are overwritten and nothing is ever deleted. Only the six tables named
<DYNAMODB_TABLE_PREFIX>customers, network-areas, tickets, calls, packs and gp-centres are touched.
The calls table is left empty on purpose: the app writes to it.
AWS credentials, region and the table prefix come from config/.env.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from config.settings import Settings, get_settings
from copilot.services.customers import normalize_number
from copilot.utils.aws import aws_session

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
        item = {k: v for k, v in customer.items() if k not in ("open_tickets", "msisdn")}
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


def ticket_items(mock_dir: Path, now: datetime) -> list[dict]:
    """Earlier complaints from the customer data (Karim's three). Dates are set relative to today."""
    items = []
    for customer in _load(mock_dir / "customers.json"):
        for ticket in customer.get("open_tickets", []):
            days = int(re.match(r"\d+", ticket["age"]).group())
            items.append(
                {
                    "customer_id": customer["id"],
                    "ticket_id": ticket["id"],
                    "category": TOPIC_CATEGORY.get(ticket["topic"], "Other"),
                    "subject": ticket["topic"],
                    "problem": ticket["topic"],
                    "status": "open",
                    "created_at": (now - timedelta(days=days)).isoformat(timespec="seconds"),
                    "source": "seed",
                }
            )
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


def check_tables(client, prefix: str) -> list[str]:
    """Every table must exist with the expected keys. Returns the problems found."""
    problems = []
    for short, (pk, sk) in TABLES.items():
        name = prefix + short
        try:
            table = client.describe_table(TableName=name)["Table"]
        except client.exceptions.ResourceNotFoundException:
            problems.append(f"{name}: table not found in this account and region")
            continue
        keys = {k["KeyType"]: k["AttributeName"] for k in table["KeySchema"]}
        if keys.get("HASH") != pk or keys.get("RANGE") != sk:
            problems.append(f"{name}: keys are {keys}, expected partition={pk} sort={sk}")
    return problems


def write_all(resource, prefix: str, seed: dict[str, list[dict]]) -> None:
    for short, rows in seed.items():
        pk, sk = TABLES[short]
        table = resource.Table(prefix + short)
        with table.batch_writer(overwrite_by_pkeys=[k for k in (pk, sk) if k]) as batch:
            for row in rows:
                batch.put_item(Item=row)


def read_back(resource, prefix: str, seed: dict[str, list[dict]]) -> list[str]:
    """Read every written item again (strongly consistent) and compare it with what was written."""
    bad = []
    for short, rows in seed.items():
        pk, sk = TABLES[short]
        table = resource.Table(prefix + short)
        for row in rows:
            key = {pk: row[pk], **({sk: row[sk]} if sk else {})}
            if table.get_item(Key=key, ConsistentRead=True).get("Item") != row:
                bad.append(f"{prefix + short}: {key}")
    return bad


def summary(seed: dict[str, list[dict]], prefix: str) -> list[str]:
    lines = [f"  {prefix + short:34} {len(rows):3} items" for short, rows in seed.items()]
    lines.append(f"  {prefix + 'calls':34}   0 items (written by the app)")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="show what would be written, change nothing")
    args = parser.parse_args()

    settings = get_settings()
    prefix = settings.dynamodb_table_prefix
    seed = build_seed(settings, datetime.now(timezone.utc))
    print("Seed data:")
    print("\n".join(summary(seed, prefix)))
    if args.dry_run:
        print("Dry run: nothing was written.")
        return

    session = aws_session(settings)
    try:
        who = session.client("sts").get_caller_identity()
        print(f"Account {who['Account']}, region {session.region_name}")
        problems = check_tables(session.client("dynamodb"), prefix)
        if problems:
            print("Not writing anything. Fix these first:\n  " + "\n  ".join(problems))
            sys.exit(1)
        resource = session.resource("dynamodb")
        write_all(resource, prefix, seed)
        bad = read_back(resource, prefix, seed)
    except (ClientError, BotoCoreError) as error:
        print(f"AWS error: {type(error).__name__}: {str(error)[:300]}")
        print("If the credentials expired, refresh the AWS_* values in config/.env and run again.")
        sys.exit(1)

    total = sum(len(rows) for rows in seed.values())
    if bad:
        print(f"Wrote {total} items, but {len(bad)} did not read back identically:\n  " + "\n  ".join(bad))
        sys.exit(1)
    print(f"Wrote {total} items and read every one back: all match.")


if __name__ == "__main__":
    main()
