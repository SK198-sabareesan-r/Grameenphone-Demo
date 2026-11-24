"""DEVELOPMENT-ONLY capture of a short inbound audio sample from a controlled Vobiz test call.

Purpose: verify the byte order of Vobiz inbound L16 (M4.2). Guard rails:
* Installed only when APP_ENV=development, VOBIZ_DEV_CAPTURE_ENABLED=true and a test destination
  is configured; otherwise no capture code runs at all.
* Captures only calls registered as test calls: created through POST /api/v1/vobiz/calls (internal
  token) to the configured test destination, or answered with that destination as `To`. Every
  other stream gets no sink ("unavailable"), so real customer calls are never captured.
* At most VOBIZ_DEV_CAPTURE_MAX_SECONDS (hard cap 30 s) per stream, held in memory (bounded) and
  written once at stream end to a directory OUTSIDE the project, owner-only permissions where the
  OS supports them. Files older than the retention period are purged when a new capture starts.
* Bytes are written exactly as received from Vobiz (no conversion). A JSON sidecar records the
  announced format. Phone numbers are never written; the call id is stored hashed.
* There is no HTTP endpoint for captures. Logs contain counts and file names only, never audio.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.config import Settings
from app.integrations.vobiz.phone import normalize_e164
from app.services.audio.audio_buffer import AudioFrame

if TYPE_CHECKING:
    from app.services.voice.call_manager import StreamSession

logger = logging.getLogger(__name__)

HARD_MAX_SECONDS = 30
RAW_SUFFIX = ".l16"
META_SUFFIX = ".json"
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class DevCaptureConfigError(ValueError):
    pass


def default_capture_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "grameenphone-ai-voice-agent" / "vobiz-captures"


def resolve_capture_dir(configured: str, *, project_root: Path = PROJECT_ROOT) -> Path:
    path = Path(configured).expanduser() if configured else default_capture_dir()
    path = path.resolve()
    root = project_root.resolve()
    if path == root or root in path.parents:
        raise DevCaptureConfigError("VOBIZ_DEV_CAPTURE_DIR must be outside the project directory")
    return path


def call_hash(call_id: str) -> str:
    return hashlib.sha256(call_id.encode()).hexdigest()[:12]


def purge_old_captures(directory: Path, max_age_s: float, *, now: float | None = None) -> int:
    """Delete capture files (and their sidecars / derived WAVs) older than max_age_s."""
    if not directory.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - max_age_s
    removed = 0
    for entry in directory.iterdir():
        if entry.is_file() and entry.suffix in (RAW_SUFFIX, META_SUFFIX, ".wav") and entry.stat().st_mtime < cutoff:
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
    return removed


class DevCaptureRegistry:
    """Call ids that may be captured. Bounded and short-lived; in memory only."""

    def __init__(self, destination: str, *, max_entries: int = 50, ttl_s: float = 3600.0) -> None:
        self._destination = normalize_e164(destination)
        self._ids: OrderedDict[str, float] = OrderedDict()
        self._max = max_entries
        self._ttl = ttl_s

    def matches_destination(self, number: str | None) -> bool:
        try:
            return bool(number) and normalize_e164(number) == self._destination
        except ValueError:
            return False

    def register(self, call_id: str) -> None:
        self._ids[call_id] = time.monotonic()
        self._ids.move_to_end(call_id)
        while len(self._ids) > self._max:
            self._ids.popitem(last=False)

    def register_if_test_call(self, call_id: str | None, destination: str | None) -> bool:
        if call_id and self.matches_destination(destination):
            self.register(call_id)
            logger.info("vobiz_dev_capture_call_registered", extra={"call_hash": call_hash(call_id)})
            return True
        return False

    def is_registered(self, call_id: str) -> bool:
        at = self._ids.get(call_id)
        if at is None:
            return False
        if time.monotonic() - at > self._ttl:
            self._ids.pop(call_id, None)
            return False
        return True


class DevCaptureSink:
    """AudioFrameSink that keeps the first N seconds of a stream's raw inbound bytes."""

    def __init__(self, session: "StreamSession", directory: Path, max_seconds: int) -> None:
        self._session = session
        self._dir = directory
        fmt = session.audio_format
        self.max_bytes = fmt.bytes_for_ms(max_seconds * 1000)
        self._buffer = bytearray()
        self.frames_captured = 0
        self.frames_after_limit = 0
        self.first_chunk: int | None = None
        self.last_chunk: int | None = None
        self.path: Path | None = None
        self._limit_logged = False

    async def handle_frame(self, frame: AudioFrame) -> None:
        room = self.max_bytes - len(self._buffer)
        if room <= 0:
            self.frames_after_limit += 1  # counted, deliberately not stored (capture cap reached)
            if not self._limit_logged:
                self._limit_logged = True
                logger.info("vobiz_dev_capture_limit_reached",
                            extra={"call_hash": call_hash(self._session.call_id), "bytes": len(self._buffer)})
            return
        self._buffer += frame.data[:room] if len(frame.data) > room else frame.data  # raw, unmodified
        self.frames_captured += 1
        self.first_chunk = frame.chunk if self.first_chunk is None else self.first_chunk
        self.last_chunk = frame.chunk

    async def close(self, reason: str) -> None:
        data, self._buffer = bytes(self._buffer), bytearray()
        if not data:
            logger.info("vobiz_dev_capture_empty", extra={"call_hash": call_hash(self._session.call_id), "reason": reason})
            return
        self.path = await asyncio.to_thread(self._write, data, reason)
        logger.info(
            "vobiz_dev_capture_saved",
            extra={"call_hash": call_hash(self._session.call_id), "file": self.path.name, "bytes": len(data),
                   "duration_s": round(self._session.audio_format.duration_ms(len(data)) / 1000, 2),
                   "frames_after_limit": self.frames_after_limit, "reason": reason},
        )

    def _write(self, data: bytes, reason: str) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._dir, 0o700)
        except OSError:
            pass
        fmt = self._session.audio_format
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        stem = f"{stamp}_{call_hash(self._session.call_id)}_{self._session.stream_id[:8]}"
        raw_path, meta_path = self._dir / f"{stem}{RAW_SUFFIX}", self._dir / f"{stem}{META_SUFFIX}"
        meta = {
            "source": "vobiz_inbound_stream",
            "call_hash": call_hash(self._session.call_id),
            "stream_id": self._session.stream_id,
            "encoding": fmt.encoding,
            "sample_rate": fmt.sample_rate,
            "channels": 1,
            "byte_order": "unverified",  # raw bytes exactly as received; to be determined by analysis
            "bytes": len(data),
            "duration_s": round(fmt.duration_ms(len(data)) / 1000, 3),
            "frames_captured": self.frames_captured,
            "frames_after_limit": self.frames_after_limit,
            "first_chunk": self.first_chunk,
            "last_chunk": self.last_chunk,
            "end_reason": reason,
            "captured_at": stamp,
        }
        for path, payload in ((raw_path, data), (meta_path, json.dumps(meta, indent=2).encode())):
            tmp = path.with_name(path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(payload)
                os.replace(tmp, path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        return raw_path


class DevCaptureSinkFactory:
    """Per-stream factory: a capture sink for registered test calls, None (no sink) for all others."""

    def __init__(self, settings: Settings, registry: DevCaptureRegistry, directory: Path) -> None:
        self._registry = registry
        self._dir = directory
        self._max_seconds = settings.vobiz_dev_capture_max_seconds
        self._retention_s = settings.vobiz_dev_capture_retention_hours * 3600

    @property
    def directory(self) -> Path:
        return self._dir

    def __call__(self, session: "StreamSession") -> DevCaptureSink | None:
        if not self._registry.is_registered(session.call_id):
            logger.info("vobiz_dev_capture_skipped", extra={"call_hash": call_hash(session.call_id),
                                                             "reason": "not_a_registered_test_call"})
            return None
        purge_old_captures(self._dir, self._retention_s)
        logger.info("vobiz_dev_capture_started", extra={"call_hash": call_hash(session.call_id),
                                                         "max_seconds": self._max_seconds,
                                                         "audio_format": session.audio_format.label})
        return DevCaptureSink(session, self._dir, self._max_seconds)


def build_dev_capture(settings: Settings) -> tuple[DevCaptureRegistry, DevCaptureSinkFactory] | None:
    """Return (registry, factory) when development capture is fully configured, else None."""
    if not settings.vobiz_dev_capture_enabled:
        return None
    if settings.app_env != "development":
        logger.warning("vobiz_dev_capture_refused", extra={"reason": "APP_ENV is not development"})
        return None
    if not 1 <= settings.vobiz_dev_capture_max_seconds <= HARD_MAX_SECONDS:
        raise DevCaptureConfigError(f"VOBIZ_DEV_CAPTURE_MAX_SECONDS must be 1-{HARD_MAX_SECONDS}")
    try:
        registry = DevCaptureRegistry(settings.vobiz_dev_capture_destination)
    except ValueError as exc:
        raise DevCaptureConfigError("VOBIZ_DEV_CAPTURE_DESTINATION must be your test phone in E.164") from exc
    directory = resolve_capture_dir(settings.vobiz_dev_capture_dir)
    logger.warning(
        "vobiz_dev_capture_enabled",
        extra={"max_seconds": settings.vobiz_dev_capture_max_seconds, "directory": str(directory),
               "retention_hours": settings.vobiz_dev_capture_retention_hours},
    )
    return registry, DevCaptureSinkFactory(settings, registry, directory)
