"""Persistence for live voice calls (POC only; PostgreSQL replaces it later).

One group of five JSON documents per call:
    transcript.json          every transcript event (partial and final), ordered by sequence_number
    customer_context.json    customer id and details, intent, references to the customer's turns
    ai_agent_context.json    AI-agent messages, actions, hand-off reason and time
    human_agent_context.json human-agent reference, transcript references, notes, hold/resume, hand-off times
    call_events.json         call lifecycle events in chronological order

Where they live is decided by the blob store passed in (see build_conversation_store in container.py):
the call-transcripts S3 bucket in normal use, memory when no bucket is configured, or a local folder
when a Path is given (tests only). Nothing is written to the local disk in normal use.

Rules this module enforces (each is covered by tests/test_conversation_store.py):
* Local writes are atomic: a temporary file in the same folder is written, flushed, fsynced and renamed over
  the target, so a crash never leaves a half-written file. An S3 put replaces the object whole.
* One asyncio.Lock per call serialises read-modify-write, so concurrent requests cannot overwrite each
  other. The lock is per process: run ONE backend process while this store is in use.
* An event with an `event_id` that is already stored is a duplicate and is not stored again. A different
  event that reuses a stored `sequence_number` is a conflict. Events are kept sorted by sequence_number
  (transcript) or timestamp (call events), whatever order they arrive in.
* A missing document starts empty. A document that is not valid JSON is set aside (`*.corrupt-<time>`),
  never deleted, and the document starts empty again.
* call_id is checked against a strict pattern before it is used in a key or a path.
* API keys and tokens are never stored. The customer's phone number is stored as the caller gave it, in the
  call-transcripts bucket only.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import tempfile
import time
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")

TRANSCRIPT = "transcript"
CUSTOMER = "customer_context"
AI_AGENT = "ai_agent_context"
HUMAN_AGENT = "human_agent_context"
CALL_EVENTS = "call_events"
FILE_NAMES = {name: f"{name}.json" for name in (TRANSCRIPT, CUSTOMER, AI_AGENT, HUMAN_AGENT, CALL_EVENTS)}

REPLACE_RETRIES = 5  # os.replace can fail briefly on Windows while a reader has the file open


class StoreError(Exception):
    """Base class for store failures that callers map to HTTP status codes."""


class InvalidIdentifier(StoreError):
    """call_id or session_id is not an acceptable identifier."""


class SessionMismatch(StoreError):
    """The event's session_id differs from the one already recorded for this call."""


class SequenceConflict(StoreError):
    """A different event already uses this sequence_number."""


class StorageUnavailable(StoreError):
    """The file could not be written (disk full, permissions, ...)."""


def check_id(kind: str, value: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.match(value) or ".." in value:
        raise InvalidIdentifier(f"invalid {kind}")
    return value


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_ts(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def atomic_write_json(path: Path, document: Any) -> None:
    """Write `document` to `path` so readers see either the old or the new file, never a partial one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(REPLACE_RETRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == REPLACE_RETRIES - 1:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        tmp.unlink(missing_ok=True)


class LocalBlobs:
    """Documents as files in one folder per call. For tests only: normal use never writes to the local disk."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def call_dir(self, call_id: str) -> Path:
        return self._root / call_id

    def read(self, call_id: str, name: str) -> dict | None:
        path = self.call_dir(call_id) / f"{name}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            return data
        except (OSError, ValueError) as exc:
            aside = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
            try:
                os.replace(path, aside)
            except OSError:
                pass
            log.error("conversation_file_corrupt", extra={"file": path.name, "error": type(exc).__name__})
            return None

    def write(self, call_id: str, name: str, doc: dict) -> None:
        atomic_write_json(self.call_dir(call_id) / f"{name}.json", doc)

    def exists(self, call_id: str) -> bool:
        return self.call_dir(call_id).is_dir()


class _Call:
    """In-memory copy of one call's documents plus its lock. The blob store holds the durable copy."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.docs: dict[str, dict] = {}
        self.event_ids: dict[str, set[str]] = {}  # per document name that holds events
        self.sequences: dict[int, str] = {}  # transcript sequence_number -> event_id
        self.dirty: set[str] = set()  # documents changed in memory but not yet written (partials only)
        self.flush_task: asyncio.Task | None = None


class ConversationStore:
    def __init__(self, blobs: Any, *, partial_flush_s: float = 0.0) -> None:
        """`blobs` is a blob store (S3Blobs, MemoryBlobs), or a folder path, which means local files (tests).

        `partial_flush_s` > 0: a partial transcript event is kept in memory and written with the next final
        event, or after this many seconds, or when the call is released, instead of one S3 upload per partial.
        On a slow network one upload per partial queued every event of the call behind S3 and the live panel
        stopped. Partials not yet written are lost if the process stops. 0 = write every event at once.
        """
        self._blobs = LocalBlobs(Path(blobs)) if isinstance(blobs, (str, Path)) else blobs
        self._calls: dict[str, _Call] = {}
        self._partial_flush_s = max(0.0, partial_flush_s)

    # ---- paths and loading -------------------------------------------------------------------
    def call_dir(self, call_id: str) -> Path:
        """The folder of one call. Only the local test store has one."""
        return self._blobs.call_dir(check_id("call_id", call_id))

    def _empty(self, name: str, call_id: str) -> dict:
        base = {"schema_version": SCHEMA_VERSION, "call_id": call_id, "session_id": None, "customer_id": None}
        if name in (TRANSCRIPT, CALL_EVENTS):
            base["events"] = []
        elif name == CUSTOMER:
            base.update(customer=None, caller_ref=None, initial_intent=None, intent=None,
                        conversation_history_refs=[], collected_context={})
        elif name == AI_AGENT:
            base.update(messages=[], actions=[], llm_context={"history_ref": TRANSCRIPT}, handoff=None, resolution=None)
        elif name == HUMAN_AGENT:
            base.update(agent_ref=None, transcript_refs=[], notes=[], hold_events=[], actions=[],
                        handoff=None, assist_activated_at=None)
        return base

    def _load_file(self, name: str, call_id: str) -> dict:
        data = self._blobs.read(call_id, name)
        return self._empty(name, call_id) if data is None else data

    def _call(self, call_id: str) -> _Call:
        check_id("call_id", call_id)
        call = self._calls.get(call_id)
        if call is None:
            call = self._calls[call_id] = _Call()
        return call

    async def _doc(self, call: _Call, name: str, call_id: str) -> dict:
        if name not in call.docs:
            try:
                doc = await asyncio.to_thread(self._load_file, name, call_id)
            except OSError as exc:
                log.error("conversation_read_failed", extra={"document": name, "error": type(exc).__name__})
                raise StorageUnavailable("could not read conversation document") from exc
            call.docs[name] = doc
            if name in (TRANSCRIPT, CALL_EVENTS):
                call.event_ids[name] = {e.get("event_id") for e in doc.get("events", [])}
            if name == TRANSCRIPT:
                call.sequences = {e["sequence_number"]: e["event_id"] for e in doc.get("events", [])
                                  if "sequence_number" in e}
        return call.docs[name]

    def _schedule_flush(self, call: _Call, call_id: str) -> None:
        if call.flush_task is None or call.flush_task.done():
            call.flush_task = asyncio.create_task(self._flush_later(call, call_id), name=f"flush-{call_id}")

    async def _flush_later(self, call: _Call, call_id: str) -> None:
        await asyncio.sleep(self._partial_flush_s)
        await self._flush(call, call_id)

    async def _flush(self, call: _Call, call_id: str) -> None:
        """Write documents that only have unsaved partials. A failure is logged; the next save retries."""
        async with call.lock:
            for name in list(call.dirty):
                doc = call.docs.get(name)
                if doc is None:
                    call.dirty.discard(name)
                    continue
                try:
                    await self._save(call, call_id, name, doc)
                except StorageUnavailable:
                    pass  # logged in _save; the partials of this document are lost, finals are retried by the sender

    async def _save(self, call: _Call, call_id: str, name: str, doc: dict) -> None:
        call.dirty.discard(name)  # this write carries every change made so far, partials included
        try:
            await asyncio.to_thread(self._blobs.write, call_id, name, doc)
        except OSError as exc:
            # The in-memory copy was already changed. Forget it so the next access reloads the stored
            # document, which still holds the last good state: a retry of the same event is then stored,
            # not "duplicate".
            call.docs.pop(name, None)
            call.event_ids.pop(name, None)
            if name == TRANSCRIPT:
                call.sequences = {}
            log.error("conversation_write_failed", extra={"document": name, "error": type(exc).__name__})
            raise StorageUnavailable("could not write conversation file") from exc

    def _bind(self, doc: dict, session_id: str | None, customer_id: str | None) -> None:
        """Tie a document to the call's session and customer. A different session is refused."""
        if session_id:
            if doc.get("session_id") and doc["session_id"] != session_id:
                raise SessionMismatch("session_id does not match the one recorded for this call")
            doc["session_id"] = session_id
        if customer_id and not doc.get("customer_id"):
            doc["customer_id"] = customer_id

    # ---- public API --------------------------------------------------------------------------

    async def add_transcript_event(self, event: dict) -> str:
        """Store one transcript event. Returns "stored" or "duplicate"."""
        call_id = event["call_id"]
        call = self._call(call_id)
        async with call.lock:
            doc = await self._doc(call, TRANSCRIPT, call_id)
            ids = call.event_ids[TRANSCRIPT]
            if event["event_id"] in ids:
                return "duplicate"
            self._bind(doc, event.get("session_id"), event.get("customer_id"))
            seq = event["sequence_number"]
            if seq in call.sequences:
                raise SequenceConflict("sequence_number is already used by a different event")
            record = {k: v for k, v in event.items() if k not in ("call_id", "session_id", "customer_id")}
            events = doc["events"]
            keys = [e["sequence_number"] for e in events]
            events.insert(bisect_right(keys, seq), record)
            ids.add(event["event_id"])
            call.sequences[seq] = event["event_id"]
            if self._partial_flush_s > 0 and event.get("transcript_type") == "partial":
                call.dirty.add(TRANSCRIPT)  # written with the next final, or by the timer
                self._schedule_flush(call, call_id)
                return "stored"
            await self._save(call, call_id, TRANSCRIPT, doc)
            return "stored"

    async def add_call_event(self, event: dict) -> str:
        """Store one lifecycle event, kept in timestamp order. Returns "stored" or "duplicate"."""
        call_id = event["call_id"]
        call = self._call(call_id)
        async with call.lock:
            doc = await self._doc(call, CALL_EVENTS, call_id)
            ids = call.event_ids[CALL_EVENTS]
            if event["event_id"] in ids:
                return "duplicate"
            self._bind(doc, event.get("session_id"), event.get("customer_id"))
            record = {k: v for k, v in event.items() if k not in ("call_id", "session_id", "customer_id")}
            events = doc["events"]
            events.append(record)
            if len(events) > 1 and _parse_ts(events[-1]["timestamp"]) < _parse_ts(events[-2]["timestamp"]):
                events.sort(key=lambda e: _parse_ts(e["timestamp"]))  # stable: ties keep arrival order
            ids.add(event["event_id"])
            await self._save(call, call_id, CALL_EVENTS, doc)
            return "stored"

    async def update(
        self, call_id: str, name: str, mutate: Callable[[dict], None],
        *, session_id: str | None = None, customer_id: str | None = None,
    ) -> dict:
        """Read-modify-write one context document under the call's lock. Returns a copy."""
        if name not in (CUSTOMER, AI_AGENT, HUMAN_AGENT):
            raise ValueError(f"not a context document: {name}")
        call = self._call(call_id)
        async with call.lock:
            doc = await self._doc(call, name, call_id)
            self._bind(doc, session_id, customer_id)
            mutate(doc)
            await self._save(call, call_id, name, doc)
            return copy.deepcopy(doc)

    async def read(self, call_id: str, name: str) -> dict:
        call = self._call(call_id)
        async with call.lock:
            return copy.deepcopy(await self._doc(call, name, call_id))

    async def customer_id_of(self, call_id: str) -> str | None:
        """The customer this call was matched to (set when the call started), or None."""
        call = self._call(call_id)
        async with call.lock:
            return (await self._doc(call, CUSTOMER, call_id)).get("customer_id")

    async def bind_customer(self, call_id: str, session_id: str | None, customer_id: str | None) -> None:
        """Once the customer is known, record the id in every document of the call, so the five files can
        be joined on call_id, session_id and customer_id. Creates any file that does not exist yet."""
        call = self._call(call_id)
        async with call.lock:
            for name in FILE_NAMES:
                doc = await self._doc(call, name, call_id)
                self._bind(doc, session_id, customer_id)
                await self._save(call, call_id, name, doc)

    async def exists(self, call_id: str) -> bool:
        try:
            check_id("call_id", call_id)
        except InvalidIdentifier:
            return False
        try:
            return await asyncio.to_thread(self._blobs.exists, call_id)
        except OSError:
            return False

    async def release(self, call_id: str) -> None:
        """Drop the in-memory copy of a finished call. The files stay."""
        call = self._calls.get(call_id)
        if call is None:
            return
        if call.dirty:  # a pending timer then finds nothing left to write
            await self._flush(call, call_id)  # write the last partials before the memory copy goes
        if not call.lock.locked():
            self._calls.pop(call_id, None)

    async def handoff_package(self, call_id: str) -> dict:
        """Everything a human agent needs when a call is handed over, from the stored files."""
        transcript = await self.read(call_id, TRANSCRIPT)
        customer = await self.read(call_id, CUSTOMER)
        ai = await self.read(call_id, AI_AGENT)
        human = await self.read(call_id, HUMAN_AGENT)
        events = await self.read(call_id, CALL_EVENTS)
        finals = [e for e in transcript["events"] if e.get("transcript_type") == "final"]
        ai_conversation = [e for e in finals if e.get("agent_mode") == "ai_agent"]
        live = [e for e in finals if e.get("agent_mode") == "human_agent"]
        handoff = ai.get("handoff") or {}
        return {
            "call_id": call_id,
            "session_id": transcript.get("session_id"),
            "customer_id": customer.get("customer_id"),
            "customer": customer.get("customer"),
            "initial_intent": customer.get("initial_intent"),
            "intent": customer.get("intent"),
            "handoff_reason": handoff.get("reason"),
            "handoff_requested_at": handoff.get("requested_at"),
            "ai_conversation": ai_conversation,  # AI agent and customer, before and until the hand-off
            "live_conversation": live,  # customer and human agent after the hand-off
            "summary": {
                "initial_intent": customer.get("initial_intent"),
                "customer_turns": sum(1 for e in finals if e.get("speaker") == "customer"),
                "ai_agent_turns": sum(1 for e in finals if e.get("speaker") == "ai_agent"),
                "human_agent_turns": sum(1 for e in finals if e.get("speaker") == "human_agent"),
                "handoff_reason": handoff.get("reason"),
            },
            "pending_actions": handoff.get("pending_actions", []),
            "notes": human.get("notes", []),
            "hold_events": human.get("hold_events", []),
            "call_events": events["events"],
        }
