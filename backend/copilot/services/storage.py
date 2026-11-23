"""Call records: local JSON files, optionally mirrored to Amazon S3."""

import asyncio
import json
import logging

from config.settings import Settings
from copilot.utils.aws import aws_session

log = logging.getLogger(__name__)


class CallStorage:
    def __init__(self, settings: Settings) -> None:
        self._dir = settings.runtime_dir / "calls"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._bucket = settings.s3_bucket
        self._prefix = settings.s3_prefix.strip("/")
        self._settings = settings

    async def save(self, record: dict) -> None:
        body = json.dumps(record, ensure_ascii=False, indent=2)
        (self._dir / f"{record['call_sid']}.json").write_text(body, encoding="utf-8")
        if self._bucket:
            try:
                await asyncio.to_thread(self._upload, record["call_sid"], body)
            except Exception:
                log.exception("S3 upload failed for call %s", record["call_sid"])

    def _upload(self, call_sid: str, body: str) -> None:
        client = aws_session(self._settings).client("s3")
        client.put_object(
            Bucket=self._bucket,
            Key=f"{self._prefix}/{call_sid}.json",
            Body=body.encode("utf-8"),
            ContentType="application/json",
        )

    def all_records(self) -> list[dict]:
        records = []
        for path in self._dir.glob("*.json"):
            records.append(json.loads(path.read_text(encoding="utf-8")))
        return records

    def clear(self) -> None:
        for path in self._dir.glob("*.json"):
            path.unlink()
