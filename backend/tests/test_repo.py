"""Tests for the data layer: DynamoDB requests (against an in-memory stand-in), local files, and the fallback."""

import asyncio
from decimal import Decimal

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from config.settings import Settings
from copilot.repo.base import DataUnavailable
from copilot.repo.dynamo import DynamoRepo
from copilot.repo.files import FileRepo
from copilot.repo.resilient import ResilientRepo
from copilot.repo.seed import TABLES
from copilot.utils.dynamo import deserialize_item, from_dynamo, serialize_item, to_dynamo

PREFIX = "gp-copilot-"


def error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "test"}}, "Operation")


class FakeDynamoClient:
    """Just enough of the boto3 DynamoDB client to check what our code sends and how it reads replies."""

    PAGE = 2  # small pages, so every read has to follow LastEvaluatedKey

    def __init__(self) -> None:
        self.tables: dict[str, dict[tuple, dict]] = {}
        self.fail_with: Exception | None = None
        self.count = 0  # how many calls reached the client
        self.requests: list[tuple] = []

    def _enter(self) -> None:
        self.count += 1
        if self.fail_with:
            raise self.fail_with

    def _key(self, table: str, item: dict) -> tuple:
        pk, sk = TABLES[table[len(PREFIX):]]
        return (item[pk]["S"], item[sk]["S"] if sk else None)

    def _page(self, items: list[dict], start_key: dict | None) -> dict:
        start = int(start_key["i"]["N"]) if start_key else 0
        reply: dict = {"Items": items[start : start + self.PAGE]}
        if start + self.PAGE < len(items):
            reply["LastEvaluatedKey"] = {"i": {"N": str(start + self.PAGE)}}
        return reply

    def get_item(self, TableName, Key, **_):
        self._enter()
        item = self.tables.get(TableName, {}).get(self._key(TableName, Key))
        return {"Item": item} if item else {}

    def put_item(self, TableName, Item, ConditionExpression=None, ExpressionAttributeNames=None):
        self._enter()
        self.requests.append(("put_item", ConditionExpression, ExpressionAttributeNames))
        table = self.tables.setdefault(TableName, {})
        key = self._key(TableName, Item)
        if ConditionExpression and key in table:
            raise error("ConditionalCheckFailedException")
        table[key] = Item

    def scan(self, TableName, ExclusiveStartKey=None):
        self._enter()
        return self._page(list(self.tables.get(TableName, {}).values()), ExclusiveStartKey)

    def query(self, TableName, KeyConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues,
              ExclusiveStartKey=None):
        self._enter()
        self.requests.append(("query", KeyConditionExpression, ExpressionAttributeNames))
        value = ExpressionAttributeValues[":pk"]["S"]
        items = [i for k, i in self.tables.get(TableName, {}).items() if k[0] == value]
        return self._page(items, ExclusiveStartKey)

    def delete_item(self, TableName, Key):
        self._enter()
        self.tables.get(TableName, {}).pop(self._key(TableName, Key), None)


def settings() -> Settings:
    return Settings(_env_file=None)  # never read config/.env in tests


def dynamo(fake: FakeDynamoClient) -> DynamoRepo:
    return DynamoRepo(settings(), client=fake)


def ticket(ticket_id: str, customer_id: str = "karim", status: str = "open", source: str = "app") -> dict:
    return {"customer_id": customer_id, "ticket_id": ticket_id, "status": status, "source": source,
            "subject": "Slow", "created_at": "2026-11-20T10:00:00+00:00"}


# ---- conversions ----

def test_floats_and_decimals_convert_both_ways():
    plain = {"ts": 1732431234.5, "n": 7, "nested": [{"x": 2.25}], "none": None, "ok": True, "bn": "মিরপুর"}
    stored = to_dynamo(plain)
    assert stored["ts"] == Decimal("1732431234.5") and isinstance(stored["nested"][0]["x"], Decimal)
    assert from_dynamo(stored) == plain
    assert from_dynamo(Decimal("120")) == 120 and isinstance(from_dynamo(Decimal("120")), int)
    assert deserialize_item(serialize_item(plain)) == plain  # the wire format round-trips too


# ---- DynamoRepo against the stand-in ----

def test_dynamo_repo_reads_back_exactly_what_it_wrote():
    fake = FakeDynamoClient()
    repo = dynamo(fake)
    record = {"call_id": "CA1", "started_at": 1732431234.5, "turns": [{"id": 1, "ts": 1732431235.25}], "customer_id": None}
    asyncio.run(repo.save_call(record))
    assert asyncio.run(repo.list_calls()) == [record]


def test_every_read_follows_pages_to_the_end():
    fake = FakeDynamoClient()
    repo = dynamo(fake)
    for n in range(5):
        item = {"area": "Uttara", "name": f"Centre {n}", "address": "x"}
        fake.tables.setdefault(PREFIX + "gp-centres", {})[("Uttara", f"Centre {n}")] = serialize_item(item)
    assert len(asyncio.run(repo.list_centres("Uttara"))) == 5  # three pages of two
    assert asyncio.run(repo.list_centres("Gulshan")) == []


def test_queries_use_placeholders_for_key_names():
    fake = FakeDynamoClient()
    asyncio.run(dynamo(fake).list_centres("Uttara"))
    asyncio.run(dynamo(fake).list_open_tickets("karim"))
    names = [(call, expr, attr) for call, expr, attr in fake.requests if call == "query"]
    assert names == [("query", "#pk = :pk", {"#pk": "area"}), ("query", "#pk = :pk", {"#pk": "customer_id"})]


def test_a_ticket_id_cannot_be_created_twice():
    fake = FakeDynamoClient()
    repo = dynamo(fake)
    assert asyncio.run(repo.create_ticket(ticket("TKT-10234"))) is True
    assert asyncio.run(repo.create_ticket(ticket("TKT-10234"))) is False  # refused, nothing overwritten
    assert asyncio.run(repo.create_ticket(ticket("TKT-10234", customer_id="other"))) is True  # different customer
    puts = [r for r in fake.requests if r[0] == "put_item"]
    assert puts[0][1] == "attribute_not_exists(#pk)" and puts[0][2] == {"#pk": "customer_id"}


def test_only_open_tickets_are_returned_for_a_customer():
    fake = FakeDynamoClient()
    repo = dynamo(fake)
    for t in (ticket("TKT-1"), ticket("TKT-2", status="closed"), ticket("TKT-3", customer_id="rahim")):
        asyncio.run(repo.create_ticket(t))
    assert [t["ticket_id"] for t in asyncio.run(repo.list_open_tickets("karim"))] == ["TKT-1"]


def test_clearing_removes_app_tickets_and_all_calls_but_keeps_seeded_tickets():
    fake = FakeDynamoClient()
    repo = dynamo(fake)
    for t in (ticket("TKT-10101", source="seed"), ticket("TKT-10234"), ticket("TKT-10235")):
        asyncio.run(repo.create_ticket(t))
    asyncio.run(repo.save_call({"call_id": "CA1"}))
    asyncio.run(repo.save_call({"call_id": "CA2"}))

    asyncio.run(repo.clear_runtime())

    assert [t["ticket_id"] for t in asyncio.run(repo.list_tickets())] == ["TKT-10101"]
    assert asyncio.run(repo.list_calls()) == []


def test_aws_failures_become_data_unavailable_with_the_reason():
    fake = FakeDynamoClient()
    repo = dynamo(fake)
    fake.fail_with = error("ExpiredTokenException")
    with pytest.raises(DataUnavailable, match="ExpiredTokenException"):
        asyncio.run(repo.get_customer("01200000001"))
    fake.fail_with = EndpointConnectionError(endpoint_url="https://dynamodb.test")
    with pytest.raises(DataUnavailable, match="EndpointConnectionError"):
        asyncio.run(repo.list_packs())


# ---- FileRepo ----

def test_file_repo_keeps_seeded_data_and_remembers_what_the_app_wrote(tmp_path):
    repo = FileRepo(settings(), runtime_dir=tmp_path)
    assert asyncio.run(repo.create_ticket(ticket("TKT-10234"))) is True
    assert asyncio.run(repo.create_ticket(ticket("TKT-10234"))) is False
    assert asyncio.run(repo.create_ticket(ticket("TKT-10101"))) is False  # karim already has it, seeded
    assert asyncio.run(repo.create_ticket(ticket("TKT-10101", customer_id="rahim"))) is True  # another customer
    asyncio.run(repo.save_call({"call_id": "CA 1/x", "started_at": 1.5}))

    again = FileRepo(settings(), runtime_dir=tmp_path)  # a restart
    assert "TKT-10234" in [t["ticket_id"] for t in asyncio.run(again.list_tickets())]
    assert asyncio.run(again.list_calls()) == [{"call_id": "CA 1/x", "started_at": 1.5}]

    asyncio.run(again.clear_runtime())
    assert asyncio.run(again.list_calls()) == []
    assert [t["ticket_id"] for t in asyncio.run(again.list_tickets())] == ["TKT-10101", "TKT-10144", "TKT-10190"]


def test_file_repo_returns_copies_so_callers_cannot_change_the_stored_data(tmp_path):
    repo = FileRepo(settings(), runtime_dir=tmp_path)
    customer = asyncio.run(repo.get_customer("01200000001"))
    customer["name"] = "Changed"
    assert asyncio.run(repo.get_customer("01200000001"))["name"] == "Rahim Uddin"


# ---- fallback ----

class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def resilient(tmp_path):
    fake, clock = FakeDynamoClient(), Clock()
    repo = ResilientRepo(dynamo(fake), FileRepo(settings(), runtime_dir=tmp_path), 30.0, clock=clock)
    return repo, fake, clock


def test_it_uses_dynamodb_while_it_works(tmp_path):
    repo, fake, _ = resilient(tmp_path)
    assert asyncio.run(repo.get_customer("01200000001")) is None  # the (empty) stand-in answered, not the files
    assert repo.status() == {"backend": "dynamodb", "fallback_active": False, "reason": None}


def test_it_falls_back_to_the_files_and_does_not_retry_until_the_wait_is_over(tmp_path):
    repo, fake, clock = resilient(tmp_path)
    fake.fail_with = error("ExpiredTokenException")

    assert asyncio.run(repo.get_customer("01200000001"))["name"] == "Rahim Uddin"  # answered from the files
    assert repo.status() == {"backend": "files", "fallback_active": True, "reason": "ExpiredTokenException"}
    calls_after_first_failure = fake.count

    asyncio.run(repo.get_customer("01200000002"))
    asyncio.run(repo.list_packs())
    assert fake.count == calls_after_first_failure  # DynamoDB is left alone during the wait

    fake.fail_with = None
    clock.now += 31  # the wait is over and DynamoDB is back
    assert asyncio.run(repo.get_customer("01200000001")) is None  # DynamoDB answered again
    assert repo.status()["fallback_active"] is False


def test_writes_made_while_dynamodb_is_down_are_kept_locally_and_a_reset_clears_them(tmp_path):
    repo, fake, _ = resilient(tmp_path)
    fake.fail_with = error("ExpiredTokenException")

    assert asyncio.run(repo.create_ticket(ticket("TKT-10234"))) is True
    asyncio.run(repo.save_call({"call_id": "CA1"}))
    assert "TKT-10234" in [t["ticket_id"] for t in asyncio.run(repo.list_tickets())]
    assert asyncio.run(repo.list_calls()) == [{"call_id": "CA1"}]

    asyncio.run(repo.clear_runtime())
    assert asyncio.run(repo.list_calls()) == []
    assert "TKT-10234" not in [t["ticket_id"] for t in asyncio.run(repo.list_tickets())]
