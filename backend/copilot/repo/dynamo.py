"""The DynamoDB tables. Calls run in worker threads (boto3 is blocking) so they never stall a live call.

Uses the low-level client, which is thread-safe, with the table names <prefix>customers, network-areas,
tickets, calls, packs and gp-centres. Credentials and region come from config/.env through aws_session.
Any AWS or network failure is raised as DataUnavailable.
"""

import asyncio
from typing import Any

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from config.settings import Settings
from copilot.repo.base import DataUnavailable
from copilot.repo.seed import TABLES
from copilot.utils.aws import aws_session
from copilot.utils.dynamo import deserialize_item, serialize_item

CONDITION_FAILED = "ConditionalCheckFailedException"


def _reason(error: Exception) -> str:
    if isinstance(error, ClientError):
        return str(error.response.get("Error", {}).get("Code", "ClientError"))
    return type(error).__name__


class DynamoRepo:
    name = "dynamodb"

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._settings = settings
        self._prefix = settings.dynamodb_table_prefix
        self._timeout = settings.dynamodb_timeout_s
        self._client = client  # a boto3 "dynamodb" client. Created on first use when not given.

    def _get_client(self):
        if self._client is None:
            config = Config(
                connect_timeout=min(self._timeout, 3.0),
                read_timeout=self._timeout,
                retries={"max_attempts": 1, "mode": "standard"},
            )
            self._client = aws_session(self._settings).client("dynamodb", config=config)
        return self._client

    async def _run(self, func, *args):
        """Run one blocking call in a worker thread. AWS and network failures become DataUnavailable."""
        try:
            return await asyncio.wait_for(asyncio.to_thread(func, *args), self._timeout * 2 + 2)
        except (ClientError, BotoCoreError) as error:
            raise DataUnavailable(_reason(error)) from error
        except asyncio.TimeoutError as error:
            raise DataUnavailable("timeout") from error

    # ---- blocking helpers, run in threads ----

    def _get(self, short: str, key: dict) -> dict | None:
        reply = self._get_client().get_item(TableName=self._prefix + short, Key=serialize_item(key))
        item = reply.get("Item")
        return deserialize_item(item) if item else None

    def _pages(self, call, **args) -> list[dict]:
        items: list[dict] = []
        while True:
            reply = call(**args)
            items += [deserialize_item(i) for i in reply.get("Items", [])]
            if not reply.get("LastEvaluatedKey"):
                return items
            args["ExclusiveStartKey"] = reply["LastEvaluatedKey"]

    def _query(self, short: str, partition_value: str) -> list[dict]:
        pk, _ = TABLES[short]
        return self._pages(
            self._get_client().query,
            TableName=self._prefix + short,
            KeyConditionExpression="#pk = :pk",
            ExpressionAttributeNames={"#pk": pk},  # placeholders: some attribute names are reserved words
            ExpressionAttributeValues={":pk": {"S": partition_value}},
        )

    def _scan(self, short: str) -> list[dict]:
        return self._pages(self._get_client().scan, TableName=self._prefix + short)

    def _put(self, short: str, item: dict, only_if_new: bool = False) -> bool:
        args: dict[str, Any] = {"TableName": self._prefix + short, "Item": serialize_item(item)}
        if only_if_new:
            args["ConditionExpression"] = "attribute_not_exists(#pk)"
            args["ExpressionAttributeNames"] = {"#pk": TABLES[short][0]}
        try:
            self._get_client().put_item(**args)
            return True
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == CONDITION_FAILED:
                return False
            raise

    def _delete(self, short: str, key: dict) -> None:
        self._get_client().delete_item(TableName=self._prefix + short, Key=serialize_item(key))

    def _clear(self) -> None:
        for ticket in self._scan("tickets"):
            if ticket.get("source") == "app":
                self._delete("tickets", {"customer_id": ticket["customer_id"], "ticket_id": ticket["ticket_id"]})
        for call in self._scan("calls"):
            self._delete("calls", {"call_id": call["call_id"]})

    # ---- the Repo interface ----

    async def get_customer(self, mobile_number: str) -> dict | None:
        return await self._run(self._get, "customers", {"mobile_number": mobile_number})

    async def list_areas(self) -> list[dict]:
        return await self._run(self._scan, "network-areas")

    async def get_area(self, area: str) -> dict | None:
        return await self._run(self._get, "network-areas", {"area": area})

    async def list_packs(self) -> list[dict]:
        return await self._run(self._scan, "packs")

    async def get_pack(self, pack_id: str) -> dict | None:
        return await self._run(self._get, "packs", {"pack_id": pack_id})

    async def list_centres(self, area: str) -> list[dict]:
        return await self._run(self._query, "gp-centres", area)

    async def list_open_tickets(self, customer_id: str) -> list[dict]:
        tickets = await self._run(self._query, "tickets", customer_id)
        return [t for t in tickets if t.get("status") == "open"]

    async def list_tickets(self) -> list[dict]:
        return await self._run(self._scan, "tickets")

    async def create_ticket(self, ticket: dict) -> bool:
        return await self._run(self._put, "tickets", ticket, True)

    async def save_call(self, record: dict) -> None:
        await self._run(self._put, "calls", record)

    async def list_calls(self) -> list[dict]:
        return await self._run(self._scan, "calls")

    async def clear_runtime(self) -> None:
        await self._run(self._clear)

    def status(self) -> dict:
        return {"backend": self.name, "fallback_active": False, "reason": None}
