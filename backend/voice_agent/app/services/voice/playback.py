"""Playback control for one Vobiz stream: checkpoints, clearAudio, and playback state.

Checkpoints:
  * names are unique ("cp-<n>-<random>"), registered BEFORE the checkpoint command is sent so a fast
    playedStream cannot be missed;
  * playedStream carries only a name (no streamId), so matching is by name inside this stream's own
    tracker. Each stream owns its tracker, so one call can never resolve another call's checkpoint;
  * the ack is conditional (never sent after clearAudio or a hangup), so every checkpoint has a
    timer and ends as exactly one of: played, timeout, cleared, closed.

clearAudio follows the Vobiz guidance: pending checkpoints are voided at once, and new playback
waits for the clearedAudio acknowledgment (bounded by a timeout) before it is queued.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from app.integrations.vobiz.stream_sender import StreamClosedError, VobizStreamSender
from app.services.audio.formats import AudioFormat

logger = logging.getLogger(__name__)

PLAYED = "played"
TIMEOUT = "timeout"
CLEARED = "cleared"
CLOSED = "closed"
NOT_WAITED = "not_waited"
UNKNOWN = "unknown"


class PlaybackState(str, Enum):
    IDLE = "idle"
    PLAYING = "playing"
    CLEARING = "clearing"


class CheckpointLimitError(RuntimeError):
    """Too many unacknowledged checkpoints on one stream."""


@dataclass
class _Pending:
    future: asyncio.Future[str]
    timer: asyncio.TimerHandle
    created_at: float  # loop.time() when the checkpoint was registered


# Called once per checkpoint when it ends: (name, outcome, elapsed_s, timeout_s or None).
FinishCallback = Callable[[str, str, float, float | None], None]


class CheckpointTracker:
    def __init__(
        self,
        timeout_s: float = 30.0,
        max_pending: int = 64,
        on_finish: FinishCallback | None = None,
    ) -> None:
        self._timeout_s = timeout_s
        self._max_pending = max_pending
        self._on_finish = on_finish
        self._counter = 0
        self._pending: dict[str, _Pending] = {}
        self._closed = False
        self.outcomes: dict[str, int] = {PLAYED: 0, TIMEOUT: 0, CLEARED: 0, CLOSED: 0}

    @property
    def pending(self) -> frozenset[str]:
        return frozenset(self._pending)

    @property
    def max_pending(self) -> int:
        return self._max_pending

    def future(self, name: str) -> asyncio.Future[str] | None:
        entry = self._pending.get(name)
        return entry.future if entry else None

    def ensure_capacity(self) -> None:
        """Raise before any audio is sent if a new checkpoint could not be registered."""
        if self._closed:
            raise StreamClosedError("checkpoint tracker is closed")
        if len(self._pending) >= self._max_pending:
            raise CheckpointLimitError(
                f"{self._max_pending} checkpoints are already awaiting acknowledgment"
            )

    def create(self) -> str:
        self.ensure_capacity()
        self._counter += 1
        name = f"cp-{self._counter}-{secrets.token_hex(4)}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        timer = loop.call_later(self._timeout_s, self._finish, name, TIMEOUT, self._timeout_s)
        self._pending[name] = _Pending(future, timer, loop.time())
        return name

    def _finish(self, name: str, outcome: str, timeout_s: float | None = None) -> bool:
        entry = self._pending.pop(name, None)
        if entry is None:
            return False
        entry.timer.cancel()
        if not entry.future.done():
            entry.future.set_result(outcome)
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1
        if self._on_finish is not None:
            elapsed = asyncio.get_running_loop().time() - entry.created_at
            try:
                self._on_finish(name, outcome, elapsed, timeout_s)
            except Exception:  # logging must never break playback
                pass
        return True

    def resolve(self, name: str) -> bool:
        """playedStream arrived. False if the name is unknown (late, already timed out, or foreign)."""
        return self._finish(name, PLAYED)

    def abandon(self, name: str, outcome: str = TIMEOUT, timeout_s: float | None = None) -> None:
        self._finish(name, outcome, timeout_s)

    def void_all(self, outcome: str = CLEARED) -> int:
        names = list(self._pending)
        for name in names:
            self._finish(name, outcome)
        return len(names)

    def close(self) -> None:
        self._closed = True
        self.void_all(CLOSED)


@dataclass(frozen=True)
class PlaybackResult:
    checkpoint: str | None
    chunks_sent: int
    aborted: bool
    outcome: str  # played | timeout | cleared | closed | not_waited


class PlaybackController:
    def __init__(
        self,
        sender: VobizStreamSender,
        *,
        checkpoint_timeout_s: float = 30.0,
        clear_timeout_s: float = 2.0,
        call_id: str | None = None,
        max_pending_checkpoints: int = 64,
    ) -> None:
        self._sender = sender
        self._call_id = call_id
        self._tracker = CheckpointTracker(
            checkpoint_timeout_s, max_pending_checkpoints, on_finish=self._log_checkpoint_finished
        )
        self._clear_timeout_s = clear_timeout_s
        self._clearing = False
        self._clear_confirmed = asyncio.Event()
        self._clear_confirmed.set()
        self._sending = 0
        self._closed = False
        # Counters reported in vobiz_stream_ended.
        self.utterances_sent = 0
        self.utterances_aborted = 0

    def _log_fields(self) -> dict[str, object]:
        return {"call_id": self._call_id, "stream_id": self._sender.stream_id}

    def _log_checkpoint_finished(self, name: str, outcome: str, elapsed_s: float, timeout_s: float | None) -> None:
        if outcome == PLAYED:
            logger.info(
                "vobiz_checkpoint_played",
                extra={**self._log_fields(), "checkpoint": name, "ack_latency_ms": round(elapsed_s * 1000)},
            )
        elif outcome == TIMEOUT:
            logger.warning(
                "vobiz_checkpoint_timeout",
                extra={
                    **self._log_fields(),
                    "checkpoint": name,
                    "timeout_s": timeout_s,
                    "elapsed_ms": round(elapsed_s * 1000),
                },
            )

    @property
    def checkpoint_outcomes(self) -> dict[str, int]:
        return dict(self._tracker.outcomes)

    @property
    def play_audio_messages(self) -> int:
        """playAudio messages actually written to the socket on this stream."""
        return self._sender.play_audio_messages_sent

    @property
    def max_pending_checkpoints(self) -> int:
        return self._tracker.max_pending

    # --- state --------------------------------------------------------------------------------

    @property
    def state(self) -> PlaybackState:
        if self._closed:
            return PlaybackState.IDLE
        if self._clearing:
            return PlaybackState.CLEARING
        if self._sending or self._tracker.pending:
            return PlaybackState.PLAYING
        return PlaybackState.IDLE

    @property
    def pending_checkpoints(self) -> frozenset[str]:
        return self._tracker.pending

    # --- commands -----------------------------------------------------------------------------

    async def play(
        self,
        audio: bytes,
        fmt: AudioFormat,
        *,
        wait: bool = False,
        timeout_s: float | None = None,
        chunk_ms: int | None = None,
    ) -> PlaybackResult:
        """Send an utterance followed by a checkpoint; optionally wait for the playedStream ack."""
        if self._closed:
            raise StreamClosedError("stream is closed")
        if self._clearing:
            try:
                await asyncio.wait_for(self._clear_confirmed.wait(), self._clear_timeout_s)
            except asyncio.TimeoutError:
                logger.warning("vobiz_clear_unconfirmed", extra={"stream_id": self._sender.stream_id})
                self._clearing = False  # do not block playback forever on a lost acknowledgment

        # Refuse BEFORE sending audio, so a full checkpoint table never leaves untracked audio queued.
        self._tracker.ensure_capacity()

        self._sending += 1
        try:
            result = await self._sender.play_audio(audio, fmt, chunk_ms=chunk_ms)
        finally:
            self._sending -= 1
        if result.aborted:
            self.utterances_aborted += 1
            self._log_playback_sent(result.chunks_sent, result.bytes_sent, fmt, None, aborted=True)
            return PlaybackResult(None, result.chunks_sent, True, CLEARED)

        name = self._tracker.create()
        try:
            await self._sender.checkpoint(name)  # sent right after the last chunk, as documented
        except Exception:
            self._tracker.abandon(name, CLOSED)
            raise
        self.utterances_sent += 1
        self._log_playback_sent(result.chunks_sent, result.bytes_sent, fmt, name, aborted=False)
        if not wait:
            return PlaybackResult(name, result.chunks_sent, False, NOT_WAITED)
        outcome = await self.wait_for(name, timeout_s)
        return PlaybackResult(name, result.chunks_sent, False, outcome)

    async def wait_for(self, name: str, timeout_s: float | None = None) -> str:
        """Wait for a still-pending checkpoint to end. The tracker's own timer guarantees it does.

        Returns "unknown" if the checkpoint already ended (or never existed).
        """
        future = self._tracker.future(name)
        if future is None:
            return UNKNOWN
        try:
            if timeout_s is None:
                return await asyncio.shield(future)
            return await asyncio.wait_for(asyncio.shield(future), timeout_s)
        except asyncio.TimeoutError:
            self._tracker.abandon(name, TIMEOUT, timeout_s)
            return TIMEOUT

    def _log_playback_sent(
        self, chunks: int, n_bytes: int, fmt: AudioFormat, checkpoint: str | None, *, aborted: bool
    ) -> None:
        logger.info(
            "vobiz_playback_sent",
            extra={
                **self._log_fields(),
                "chunks_sent": chunks,
                "bytes_sent": n_bytes,
                "audio_format": fmt.label,
                "duration_ms": round(fmt.duration_ms(n_bytes)),
                "checkpoint": checkpoint,
                "aborted": aborted,
            },
        )

    async def clear(self, *, wait_confirmed: bool = True) -> bool:
        """Send clearAudio and void pending checkpoints. Returns True once clearedAudio is seen."""
        if self._closed:
            raise StreamClosedError("stream is closed")
        self._clearing = True
        self._clear_confirmed.clear()
        try:
            await self._sender.clear_audio()
        except Exception:
            self._clearing = False
            self._clear_confirmed.set()
            raise
        self._tracker.void_all(CLEARED)  # Vobiz will not acknowledge these
        if not wait_confirmed:
            return False
        try:
            await asyncio.wait_for(self._clear_confirmed.wait(), self._clear_timeout_s)
            return True
        except asyncio.TimeoutError:
            logger.warning("vobiz_clear_unconfirmed", extra={"stream_id": self._sender.stream_id})
            return False

    # --- events from Vobiz --------------------------------------------------------------------

    def handle_played_stream(self, name: str) -> bool:
        resolved = self._tracker.resolve(name)
        if not resolved:
            logger.debug("vobiz_played_stream_unmatched", extra={"stream_id": self._sender.stream_id})
        return resolved

    def handle_cleared_audio(self) -> None:
        logger.info(
            "vobiz_cleared_audio_received",
            extra={**self._log_fields(), "awaited": self._clearing},  # False = unsolicited ack
        )
        self._clearing = False
        self._clear_confirmed.set()

    # --- lifecycle ----------------------------------------------------------------------------

    def close(self) -> None:
        """Stream ended: stop sending, resolve every waiter, release everything. Idempotent."""
        self._closed = True
        self._clearing = False
        self._sender.close()
        self._tracker.close()
        self._clear_confirmed.set()
