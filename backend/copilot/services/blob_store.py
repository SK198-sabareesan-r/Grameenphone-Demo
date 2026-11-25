"""Where the JSON documents of a call are kept: the call-transcripts S3 bucket, or memory.

A blob store keeps small JSON documents under (call_id, name). The conversation store never touches files
or S3 itself, so nothing reaches the local disk unless a folder is configured on purpose (tests only).
"""
import copy
import json
import logging
import time

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from config.settings import Settings
from copilot.utils.aws import aws_session

log = logging.getLogger(__name__)
MISSING_KEY_CODES = {"NoSuchKey", "404", "NotFound"}


class BlobError(OSError):
    """A document could not be read or written. An OSError, so callers handle it like a disk error."""


class MemoryBlobs:
    """Keeps the documents in this process only. Used when no bucket is configured. Lost on restart."""

    def __init__(self) -> None:
        self._docs: dict[tuple[str, str], dict] = {}

    def read(self, call_id: str, name: str) -> dict | None:
        doc = self._docs.get((call_id, name))
        return copy.deepcopy(doc) if doc is not None else None

    def write(self, call_id: str, name: str, doc: dict) -> None:
        self._docs[(call_id, name)] = copy.deepcopy(doc)

    def exists(self, call_id: str) -> bool:
        return any(key[0] == call_id for key in self._docs)


class S3Blobs:
    """One object per document: <prefix>/<call_id>/<name>.json in the call-transcripts bucket."""

    def __init__(self, settings: Settings, bucket: str, prefix: str) -> None:
        self._settings, self._bucket, self._prefix = settings, bucket, prefix.strip("/")
        self._client = None  # created on first use, so the app starts without AWS config

    def _s3(self):
        if self._client is None:
            self._client = aws_session(self._settings).client(
                "s3", config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2})
            )
        return self._client

    def _key(self, call_id: str, name: str) -> str:
        return f"{self._prefix}/{call_id}/{name}.json"

    def read(self, call_id: str, name: str) -> dict | None:
        key = self._key(call_id, name)
        try:
            body = self._s3().get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in MISSING_KEY_CODES:
                return None
            raise BlobError(f"could not read {key}") from error
        except BotoCoreError as error:
            raise BlobError(f"could not read {key}") from error
        try:
            data = json.loads(body.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            return data
        except ValueError:
            self._set_aside(key)
            return None

    def _set_aside(self, key: str) -> None:
        """A document that is not valid JSON is copied aside, never deleted, and the document starts empty."""
        aside = f"{key}.corrupt-{int(time.time())}"
        try:
            self._s3().copy_object(Bucket=self._bucket, Key=aside, CopySource={"Bucket": self._bucket, "Key": key})
        except (ClientError, BotoCoreError):
            log.exception("conversation_document_corrupt_and_not_copied key=%s", key)
            return
        log.error("conversation_document_corrupt key=%s copied_to=%s", key, aside)

    def write(self, call_id: str, name: str, doc: dict) -> None:
        key = self._key(call_id, name)
        body = json.dumps(doc, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        try:
            self._s3().put_object(
                Bucket=self._bucket, Key=key, Body=body, ContentType="application/json", ServerSideEncryption="AES256"
            )
        except (ClientError, BotoCoreError) as error:
            raise BlobError(f"could not write {key}") from error

    def exists(self, call_id: str) -> bool:
        try:
            reply = self._s3().list_objects_v2(Bucket=self._bucket, Prefix=f"{self._prefix}/{call_id}/", MaxKeys=1)
        except (ClientError, BotoCoreError) as error:
            raise BlobError("could not list the call documents") from error
        return reply.get("KeyCount", 0) > 0
