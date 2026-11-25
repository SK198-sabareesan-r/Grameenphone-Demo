"""Call records: saved through the data store (DynamoDB), and as one JSON file per call in the
call-transcripts S3 bucket, under the customer's original mobile number."""
import asyncio
import json
import logging
import re

from config.settings import Settings
from copilot.repo.base import DataUnavailable, Repo
from copilot.services.customers import normalize_number
from copilot.utils.aws import aws_session

log = logging.getLogger(__name__)
UNIDENTIFIED = "unidentified"  # the folder for a call where no mobile number is known
_SAFE_KEY_PART = re.compile(r"[^A-Za-z0-9_.-]")


def transcript_key(prefix: str, record: dict) -> str:
    """<prefix>/<mobile number>/<call id>.json. The number is the customer's if one was identified, else the
    caller ID, else "unidentified"."""
    number = normalize_number(record.get("mobile_number") or record.get("caller_number") or "") or UNIDENTIFIED
    call_id = _SAFE_KEY_PART.sub("_", str(record["call_id"]))
    return f"{prefix.strip('/')}/{_SAFE_KEY_PART.sub('_', number)}/{call_id}.json"


class CallStorage:
    def __init__(self, settings: Settings, repo: Repo) -> None:
        self._settings = settings
        self._repo = repo
        self._bucket = settings.s3_transcript_bucket
        self._prefix = settings.s3_transcript_prefix

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
        else:
            log.warning("S3_TRANSCRIPT_BUCKET is not set: call %s is not saved as a transcript file.", record.get("call_id"))

    def _upload(self, record: dict) -> None:
        body = json.dumps(record, ensure_ascii=False, indent=2, default=str)
        aws_session(self._settings).client("s3").put_object(
            Bucket=self._bucket,
            Key=transcript_key(self._prefix, record),
            Body=body.encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )

    def source_status(self) -> dict:
        """Where the saved calls are read from, for the dashboard: the calls table, or the in-memory stand-in."""
        return self._repo.status()

    async def all_records(self) -> list[dict]:
        try:
            return await self._repo.list_calls()
        except DataUnavailable as error:
            log.error("Could not read saved calls: %s", error)
            return []
