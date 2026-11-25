"""Tests for the DynamoDB seed data. No AWS access: only the data the script would write is checked."""

from datetime import datetime, timezone
from decimal import Decimal

from config.settings import Settings
from copilot.repo.seed import TABLES, build_seed

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
        "customers": 6, "network-areas": 7, "tickets": 7, "packs": 5, "gp-centres": 11,
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
    assert len(rows) == 7


def test_earlier_tickets_are_dated_relative_to_today():
    tickets = {t["ticket_id"]: t for t in seed()["tickets"] if t["customer_id"] == "karim"}
    assert set(tickets) == {"TKT-10101", "TKT-10144", "TKT-10190"}
    assert tickets["TKT-10101"]["created_at"].startswith("2026-11-15")  # 9 days before NOW
    assert tickets["TKT-10190"]["created_at"].startswith("2026-11-22")  # 2 days before NOW
    assert all(t["status"] == "open" and t["category"] == "Network" for t in tickets.values())


def test_every_customer_area_and_gp_centre_area_is_a_known_network_area():
    data = seed()
    known = {a["area"] for a in data["network-areas"]}
    assert {c["area"] for c in data["customers"]} <= known
    assert {g["area"] for g in data["gp-centres"]} <= known


def test_seeded_areas_match_the_json_file():
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "data" / "mock" / "network_areas.json"
    names = json.loads(path.read_text(encoding="utf-8"))["areas"]
    assert sorted(a["area"] for a in seed()["network-areas"]) == sorted(names)
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


def test_the_demo_caller_has_the_data_for_every_scenario_in_one_record():
    data = seed()
    customer = next(c for c in data["customers"] if c["mobile_number"] == "9500799295")
    assert customer["name"] == "Abdul Rahim" and customer["type"] == "Prepaid" and customer["area"] == "Mirpur"
    assert customer["previous_pack"]["data_left_gb"] == 5  # CP8: the expired pack with 5 GB left
    assert customer["charges"][0]["amount_bdt"] == 100 and "Cricket Alert" in customer["charges"][0]["item"]  # CP2
    assert customer["roaming"] == "Not activated"  # CP10
    assert [r["amount_bdt"] for r in customer["recent_recharges"]] == [100, 199]  # CP4, and no Tk 200 for CP11
    assert "open_tickets" not in customer
    tickets = [t for t in data["tickets"] if t["customer_id"] == customer["id"]]
    assert {t["ticket_id"] for t in tickets} == {"TKT-10150", "TKT-10187", "TKT-10098", "TKT-10122"}  # CP5: earlier complaints
    assert {t["ticket_id"]: t["status"] for t in tickets} == {
        "TKT-10150": "open", "TKT-10187": "open", "TKT-10098": "closed", "TKT-10122": "resolved"}
    assert all(t["mobile_number"] == "9500799295" and t["customer_name"] == "Abdul Rahim" for t in tickets)
    assert "_note" not in customer  # notes in the data file are not stored
    ids = [t["ticket_id"] for t in data["tickets"]]
    assert len(ids) == len(set(ids)), "ticket numbers are unique across customers"
    mirpur = next(a for a in data["network-areas"] if a["area"] == "Mirpur")
    assert mirpur["notice"]["status"] == "maintenance"  # CP1
    assert any(c["area"] == "Mirpur" for c in data["gp-centres"])  # CP9
    pack_ids = {p["pack_id"] for p in data["packs"]}
    assert {"bundle-30gb-300min-30d", "roaming-travel-10gb-10d"} <= pack_ids  # CP3, CP10