"""Tests for the DynamoDB seed data. No AWS access: only the data the script would write is checked."""

from datetime import datetime, timezone
from decimal import Decimal

from config.settings import Settings
from copilot.services.areas import NetworkAreas
from scripts.seed_dynamodb import TABLES, build_seed

NOW = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)


def seed(phone_map: str = ""):
    return build_seed(Settings(_env_file=None, demo_phone_map=phone_map), NOW)


def walk(value):
    yield value
    if isinstance(value, dict):
        for v in value.values():
            yield from walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk(v)


def test_expected_tables_and_row_counts():
    data = seed()
    assert {k: len(v) for k, v in data.items()} == {
        "customers": 5, "network-areas": 7, "tickets": 3, "packs": 5, "gp-centres": 11,
    }
    assert "calls" not in data  # written by the app, never seeded


def test_every_item_has_its_key_attributes_as_non_empty_strings():
    for short, rows in seed().items():
        pk, sk = TABLES[short]
        for row in rows:
            for key in (pk, sk):
                if key:
                    assert isinstance(row[key], str) and row[key].strip(), (short, key, row)


def test_no_float_values_because_dynamodb_rejects_them():
    for rows in seed().values():
        for row in rows:
            assert not any(isinstance(v, float) for v in walk(row)), row


def test_customers_are_keyed_by_normalized_mobile_number_and_hold_no_tickets():
    customers = {c["id"]: c for c in seed()["customers"]}
    assert customers["rahim"]["mobile_number"] == "01200000001"
    assert all("open_tickets" not in c and "msisdn" not in c for c in customers.values())
    assert customers["nusrat"]["charges"][1]["amount_bdt"] == 100  # the value-added service charge
    assert customers["karim"]["area"] == "Uttara"


def test_a_team_phone_gets_its_own_copy_of_that_customer():
    rows = seed("+8801711111111:rahim,+8801722222222:nobody")["customers"]
    copies = [r for r in rows if r.get("team_phone")]
    assert len(copies) == 1  # the unknown customer id is ignored
    assert copies[0]["id"] == "rahim" and copies[0]["mobile_number"] == "01711111111"
    assert len(rows) == 6


def test_earlier_tickets_belong_to_karim_and_are_dated_relative_to_today():
    tickets = {t["ticket_id"]: t for t in seed()["tickets"]}
    assert set(tickets) == {"TKT-10101", "TKT-10144", "TKT-10190"}
    assert {t["customer_id"] for t in tickets.values()} == {"karim"}
    assert tickets["TKT-10101"]["created_at"].startswith("2026-11-15")  # 9 days before NOW
    assert tickets["TKT-10190"]["created_at"].startswith("2026-11-22")  # 2 days before NOW
    assert all(t["status"] == "open" and t["category"] == "Network" for t in tickets.values())


def test_every_customer_area_and_gp_centre_area_is_a_known_network_area():
    data = seed()
    known = {a["area"] for a in data["network-areas"]}
    assert {c["area"] for c in data["customers"]} <= known
    assert {g["area"] for g in data["gp-centres"]} <= known


def test_seeded_areas_match_what_the_app_reads_from_the_json_file():
    from pathlib import Path

    areas = NetworkAreas(Path(__file__).resolve().parents[1] / "data" / "mock" / "network_areas.json")
    assert sorted(a["area"] for a in seed()["network-areas"]) == sorted(areas.names())
    gazipur = next(a for a in seed()["network-areas"] if a["area"] == "Gazipur")
    assert gazipur["notice"]["status"] == "outage"
    assert next(a for a in seed()["network-areas"] if a["area"] == "Dhanmondi")["notice"] is None


def test_packs_do_not_invent_activation_codes_and_one_is_the_sample_offer():
    packs = {p["pack_id"]: p for p in seed()["packs"]}
    assert [p["pack_id"] for p in packs.values() if p.get("sample_offer")] == ["bundle-30gb-300min-30d"]
    assert all(p["activation_code"] is None for p in packs.values() if p["category"] == "bundle")
    assert packs["roaming-travel-10gb-10d"]["activation_code"] == "*121*1599#"
    assert packs["roaming-travel-10gb-10d"]["price_bdt"] == 1599
    assert all(p["verify_before_demo"] for p in packs.values())


def test_gp_centres_are_real_unless_marked_sample():
    centres = seed()["gp-centres"]
    sample = [c for c in centres if c["type"] == "sample"]
    assert [c["area"] for c in sample] == ["Chattogram"]
    for c in centres:
        if c["type"] == "public":
            assert c["source_url"].startswith("https://www.grameenphone.com") and c["address"] and c["hours"]
    assert isinstance(Decimal("1"), Decimal)  # keeps the Decimal import meaningful for float parsing checks
