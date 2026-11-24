"""Call records: saved through the data store (DynamoDB), and mirrored to Amazon S3 when a bucket is set."""

import asyncio
import json
import logging

from config.settings import Settings
from copilot.repo.base import DataUnavailable, Repo
from copilot.utils.aws import aws_session

log = logging.getLogger(__name__)


class CallStorage:
    def __init__(self, settings: Settings, repo: Repo) -> None:
        self._settings = settings
        self._repo = repo
        self._bucket = settings.s3_bucket
        self._prefix = settings.s3_prefix.strip("/")

    async def save(self, record: dict) -> None:
        """Save one call. A failure is logged and never stops the summary from reaching the agent."""
        try:
            await self._repo.save_call(record)
        except DataUnavailable as error:
            log.error("Could not save call %s: %s", record.get("call_id"), error)
        if self._bucket:
            try:
                await asyncio.to_thread(self._upload, record)
            except Exception:
                log.exception("S3 upload failed for call %s", record.get("call_id"))

    def _upload(self, record: dict) -> None:
        body = json.dumps(record, ensure_ascii=False, indent=2, default=str)
        aws_session(self._settings).client("s3").put_object(
            Bucket=self._bucket,
            Key=f"{self._prefix}/{record['call_id']}.json",
            Body=body.encode("utf-8"),
            ContentType="application/json",
        )

    async def all_records(self) -> list[dict]:
        try:
            return await self._repo.list_calls()
        except DataUnavailable as error:
            log.error("Could not read saved calls: %s", error)
            return []
