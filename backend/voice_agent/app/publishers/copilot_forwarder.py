"""Forwards live call events from the voice agent to the Copilot backend (M3).

Three kinds of event reach the Copilot, over two internal HTTP routes (see backend/copilot/api/internal_routes.py):

* transcript events (POST .../transcripts/events): Sarvam partial/final transcripts from the transcript
  pipeline (this class is a pipeline subscriber), and the AI agent's own replies (`emit_ai_message`);
* call lifecycle events (POST .../calls/events): call_started, human_handoff_requested, hold/resume, ...
  (`emit_call_event`).

Guarantees (each covered by tests/test_copilot_forwarder.py):

* Never blocks the audio or STT path. `on_event` and the `emit_*` methods only append to a bounded
  in-memory buffer; one background worker per call sends it. The pipeline already runs subscribers in
  their own tasks, so a slow Copilot cannot delay the Sarvam receive loop.
* Order: this class assigns `sequence_number` (per call, across every speaker) when an event is accepted,
  and the worker sends in that order. The Copilot also sorts by sequence_number.
* Idempotent retries: the event_id is fixed when the event is accepted (`<call_id>-t<n>`), so a retry of
  the same event is recognised as a duplicate by the Copilot. Final transcripts are also de-duplicated here.
* Bounded: a full buffer drops the oldest PARTIAL first. Finals and lifecycle events are only dropped when
  the buffer is full of them (counted and logged, never silently). Partials get one retry, everything else
  `max_attempts` with exponential back-off.
* Transient failures (network error, timeout, 408, 429, 5xx) are retried; permanent ones (401, 403, 409,
  422, other 4xx) are not. Nothing is retried forever.
* Speaker attribution never guesses: see `attribute_speaker`.
* Logs carry ids, counts and status codes only: never transcript text, the token or phone numbers.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import Settings
from app.publishers.transcripts import TRANSCRIPT_FINAL, TRANSCRIPT_PARTIAL, TranscriptEvent
from app.services.voice.track_router import TRACK_INBOUND, TRACK_OUTBOUND

logger = logging.getLogger(__name__)

TOKEN_HEADER = "X-Internal-Token"
AI_AGENT, HUMAN_AGENT, CUSTOMER, UNKNOWN = "ai_agent", "human_agent", "customer", "unknown"
TRANSIENT_STATUS = frozenset({408, 425, 429})
ONCE_ONLY = frozenset({"call_started", "ai_agent_connected", "call_ended", "human_agent_connected"})

# What each lifecycle event does to the call status / who handles the call (mirrors the Copilot).
STATE_AFTER: dict[str, tuple[str, str | None]] = {
    "call_started": ("in_progress", AI_AGENT),
    "ai_agent_connected": ("in_progress", AI_AGENT),
    "ai_resolution_attempt": ("ai_resolution_attempt", None),  # the customer said the AI's help did not work
    "human_handoff_requested": ("handoff_requested", None),
    "human_agent_dialing": ("dialing_human", None),
    "human_agent_connected": ("with_human", HUMAN_AGENT),
    "human_handoff_failed": ("handoff_failed", AI_AGENT),
    "customer_put_on_hold": ("on_hold", None),
    "customer_resumed": ("with_human", None),
    "human_agent_disconnected": ("human_disconnected", None),
    "call_ended": ("ended", None),
}


def attribute_speaker(event: TranscriptEvent, agent_mode: str, outbound_track_speaker: str) -> tuple[str, str]:
    """Decide who spoke, from the media track only. Returns (speaker, basis). Never fabricates.

    `TranscriptEvent.speaker` is NOT used: the STT sink leaves it at a default ("customer") on purpose.

    * No track (the primary AI-call stream): Vobiz forks only the caller's inbound audio there (the stream
      is requested with audioTrack="inbound"; confirmed with real speech in M4.2). So it is the customer.
    * track_inbound on a monitored stream: the same provider tag, so the customer's (the A-leg's) audio.
      Documented by Vobiz, NOT yet confirmed on a live transferred call.
    * track_outbound on a monitored stream: audio Vobiz sends TO the customer. After a transfer that is
      the human agent's voice, but Vobiz does not document it for bridged calls, so it is "unknown" unless
      VOBIZ_OUTBOUND_TRACK_SPEAKER=human_agent says it has been verified, and only while a human is handling
      the call.
    """
    track = event.track
    if track is None:
        return CUSTOMER, "primary_stream_inbound_track"
    if track == TRACK_INBOUND:
        return CUSTOMER, "monitor_inbound_track"
    if track == TRACK_OUTBOUND:
        if agent_mode == HUMAN_AGENT and outbound_track_speaker == HUMAN_AGENT:
            return HUMAN_AGENT, "monitor_outbound_track_configured"
        return UNKNOWN, "outbound_track_unverified"
    return UNKNOWN, "unrecognized_track"


def _iso(value: str | None = None) -> str:
    if value:
        parsed = value.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(parsed)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def session_id_for(call_id: str) -> str:
    """Stable per call (also after a voice-agent restart): derived from the call id, no state needed."""
    candidate = f"sess-{call_id}"
    if len(candidate) <= 128 and all(ch.isalnum() or ch in "_.-" for ch in candidate):
        return candidate
    return "sess-" + hashlib.sha256(call_id.encode()).hexdigest()[:32]


def safe_call_id(call_id: str) -> str:
    """The Copilot only accepts [A-Za-z0-9_.-] ids; anything else is replaced by a stable hash."""
    if call_id and len(call_id) <= 128 and call_id[0].isalnum() and all(ch.isalnum() or ch in "_.-" for ch in call_id):
        return call_id
    return "call-" + hashlib.sha256(call_id.encode()).hexdigest()[:32]


@dataclass
class _Item:
    kind: str  # "transcript" | "call"
    body: dict[str, Any]
    partial: bool = False


@dataclass
class _Call:
    call_id: str
    session_id: str
    key: str = ""  # the id the rest of the app uses (call_id above is the Copilot-safe form)
    status: str = "in_progress"
    agent_mode: str = AI_AGENT
    seq: int = 0
    call_events: int = 0
    buffer: deque[_Item] = field(default_factory=deque)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    worker: asyncio.Task[None] | None = None
    closed: bool = False
    seen_finals: set[tuple[Any, ...]] = field(default_factory=set)
    once: set[str] = field(default_factory=set)
    sent: int = 0
    last_status: int | None = None  # HTTP status of the most recent answer from the Copilot
    failed: int = 0
    dropped_partials: int = 0
    dropped_other: int = 0
    duplicates: int = 0


class CopilotEventForwarder:
    """Pipeline subscriber (`on_event`, `on_call_closed`) plus the emit_* API used by the rest of the app."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        self._url = settings.copilot_transcript_webhook_url.strip()
        self._calls_url = settings.copilot_calls_webhook_url
        self._token = settings.internal_api_token.get_secret_value()
        self._buffer_size = max(8, settings.copilot_forward_buffer_size)
        self._max_attempts = max(1, settings.copilot_forward_max_attempts)
        self._backoff_s = max(0.0, settings.copilot_forward_backoff_s)
        self._max_backoff_s = max(self._backoff_s, settings.copilot_forward_max_backoff_s)
        self._drain_timeout_s = settings.copilot_forward_drain_timeout_s
        self._idle_s = 600.0
        self._outbound_speaker = settings.vobiz_outbound_track_speaker
        self._owns_client = client is None
        self._http = client or httpx.AsyncClient(timeout=httpx.Timeout(settings.copilot_forward_timeout_s))
        self._sleep = sleep
        self._calls: dict[str, _Call] = {}
        self.enabled = bool(self._url and self._token)
        # Set by the app: call_id -> "ai_resolved" | "human_handled" | "unresolved" | "no_interaction". Added to call_ended.
        self.resolution_outcome = None

    # ---- state ------------------------------------------------------------------------------------

    def _state(self, call_id: str) -> _Call:
        call = self._calls.get(call_id)
        if call is None:
            call = self._calls[call_id] = _Call(
                call_id=safe_call_id(call_id), session_id=session_id_for(safe_call_id(call_id)), key=call_id
            )
        return call

    def describe(self) -> dict[str, object]:
        """What the forwarder will do, for startup logs and diagnostics. Never the token or a query string."""
        from urllib.parse import urlsplit

        parts = urlsplit(self._url)
        why = None
        if not self._url:
            why = "COPILOT_TRANSCRIPT_WEBHOOK_URL is empty"
        elif not self._token:
            why = "INTERNAL_API_TOKEN is empty"
        return {
            "enabled": self.enabled, "disabled_reason": why, "token_set": bool(self._token),
            "transcripts_url": f"{parts.scheme}://{parts.netloc}{parts.path}" if self._url else None,
            "calls_url": self._calls_url if self._url else None, "max_attempts": self._max_attempts,
            "buffer_size": self._buffer_size,
        }

    def agent_mode(self, call_id: str) -> str:
        return self._state(call_id).agent_mode

    def snapshot(self, call_id: str) -> dict[str, object] | None:
        call = self._calls.get(call_id)
        if call is None:
            return None
        return {
            "call_id": call.call_id, "status": call.status, "agent_mode": call.agent_mode, "sequence": call.seq,
            "buffered": len(call.buffer), "sent": call.sent, "failed": call.failed,
            "dropped_partials": call.dropped_partials, "dropped_other": call.dropped_other,
            "duplicates": call.duplicates, "last_status": call.last_status,
        }

    # ---- pipeline subscriber ----------------------------------------------------------------------

    async def on_event(self, event: TranscriptEvent) -> None:
        if not self.enabled or event.type not in (TRANSCRIPT_PARTIAL, TRANSCRIPT_FINAL):
            return
        text = (event.text or "").strip()
        if not text:
            return
        call = self._state(event.call_id)
        if call.closed:
            return
        final = event.type == TRANSCRIPT_FINAL
        if final:
            # stt_request_id: utterance_idx restarts at 0 on each new Sarvam session (after a reconnect).
            key = (event.stream_id, event.track, event.stt_request_id,
                   event.utterance_idx if event.utterance_idx is not None else event.sequence)
            if key in call.seen_finals:
                call.duplicates += 1
                logger.info("copilot_forward_duplicate_final", extra={"call_id": call.call_id})
                return
            call.seen_finals.add(key)
        speaker, basis = attribute_speaker(event, call.agent_mode, self._outbound_speaker)
        self._enqueue_transcript(
            call, speaker=speaker, basis=basis, text=text, final=final, timestamp=_iso(event.received_at),
            language=event.language,
        )

    async def on_call_closed(self, event: Any) -> None:
        """The transcript session closed (hangup, or idle after a lost hangup webhook). Every final transcript
        is already in the buffer, so `call_ended` goes after them; once-only, so a hangup listener sending it
        too is harmless. The state is kept until the call listener forgets it, so late events keep counting up."""
        self.emit_call_event(event.call_id, "call_ended", {"reason": getattr(event, "reason", "closed")})
        await self.finish_call(event.call_id, forget=False)

    # ---- emit API ---------------------------------------------------------------------------------

    def emit_ai_message(self, call_id: str, text: str) -> None:
        """The AI agent's own reply, as a final transcript line. Never raises."""
        try:
            text = (text or "").strip()
            if not self.enabled or not text:
                return
            call = self._state(call_id)
            if call.closed:
                return
            self._enqueue_transcript(
                call, speaker=AI_AGENT, basis="ai_agent_reply", text=text, final=True, timestamp=_iso(), language=None
            )
        except Exception as exc:
            logger.warning("copilot_forward_emit_failed", extra={"error": type(exc).__name__})

    def emit_call_event(self, call_id: str, event_type: str, data: dict[str, Any] | None = None) -> None:
        """A call lifecycle event. Updates the status / agent mode that later transcript events carry.
        Once-only types (call_started, call_ended, ...) are sent once per call. Never raises."""
        try:
            call = self._state(call_id)
            status, mode = STATE_AFTER.get(event_type, (call.status, None))
            if event_type in ONCE_ONLY:
                if event_type in call.once:
                    return
                call.once.add(event_type)
                event_id = f"{call.call_id}-{event_type}"
            else:
                call.call_events += 1
                event_id = f"{call.call_id}-c{call.call_events}"
            if event_type == "call_ended" and self.resolution_outcome is not None and "resolution" not in (data or {}):
                try:
                    data = {**(data or {}), "resolution": self.resolution_outcome(call_id)}
                except Exception as exc:  # the outcome is a nicety: the call_ended event must always go out
                    logger.warning("copilot_forward_outcome_failed", extra={"error": type(exc).__name__})
            call.status = status
            if mode:
                call.agent_mode = mode
            if not self.enabled or (call.closed and event_type != "call_ended"):
                return
            body = {
                "event_id": event_id, "call_id": call.call_id, "session_id": call.session_id,
                "type": event_type, "timestamp": _iso(), "source": "voice_agent", "data": data or {},
            }
            self._push(call, _Item("call", body))
        except Exception as exc:
            logger.warning("copilot_forward_emit_failed", extra={"error": type(exc).__name__})

    async def finish_call(self, call_id: str, *, forget: bool = True) -> None:
        """No more events for this call: wait (bounded) for what is buffered, then stop the worker."""
        call = self._calls.get(call_id)
        if call is None:
            return
        call.closed = True
        call.wake.set()
        worker = call.worker
        if worker is not None and not worker.done():
            try:
                await asyncio.wait_for(asyncio.shield(worker), self._drain_timeout_s)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                worker.cancel()
                lost = len(call.buffer)
                if lost:
                    call.failed += lost
                    logger.error("copilot_forward_undelivered_at_close", extra={"call_id": call.call_id, "events": lost})
                call.buffer.clear()
        if forget:
            self._calls.pop(call_id, None)

    async def aclose(self) -> None:
        for call_id in list(self._calls):
            await self.finish_call(call_id)
        if self._owns_client:
            await self._http.aclose()

    # ---- buffering --------------------------------------------------------------------------------

    def _enqueue_transcript(
        self, call: _Call, *, speaker: str, basis: str, text: str, final: bool, timestamp: str, language: str | None
    ) -> None:
        call.seq += 1
        body = {
            "event_id": f"{call.call_id}-t{call.seq}", "call_id": call.call_id, "session_id": call.session_id,
            "speaker": speaker, "speaker_basis": basis, "transcript_text": text,
            "transcript_type": "final" if final else "partial", "timestamp": timestamp,
            "sequence_number": call.seq, "call_status": call.status, "agent_mode": call.agent_mode,
        }
        if language:
            body["language"] = language[:16]
        self._push(call, _Item("transcript", body, partial=not final))

    def _push(self, call: _Call, item: _Item) -> None:
        buf = call.buffer
        if len(buf) >= self._buffer_size:
            if not self._evict_partial(call):
                if item.partial:
                    call.dropped_partials += 1
                    return
                if len(buf) >= self._buffer_size * 2:  # hard cap: drop the oldest non-partial, loudly
                    buf.popleft()
                    call.dropped_other += 1
                    logger.error("copilot_forward_buffer_overflow", extra={"call_id": call.call_id, "dropped": call.dropped_other})
        buf.append(item)
        call.wake.set()
        if call.worker is None or call.worker.done():
            call.worker = asyncio.create_task(self._run(call), name=f"copilot-forward:{call.call_id}")

    def _evict_partial(self, call: _Call) -> bool:
        for i, queued in enumerate(call.buffer):
            if queued.partial:
                del call.buffer[i]
                call.dropped_partials += 1
                return True
        return False

    # ---- sending ----------------------------------------------------------------------------------

    async def _run(self, call: _Call) -> None:
        try:
            while True:
                if not call.buffer:
                    if call.closed:
                        return
                    call.wake.clear()
                    try:
                        await asyncio.wait_for(call.wake.wait(), self._idle_s)
                    except asyncio.TimeoutError:
                        if not call.buffer:
                            self._calls.pop(call.key, None)
                            return
                    continue
                item = call.buffer.popleft()
                if await self._send(call, item):
                    call.sent += 1
                else:
                    call.failed += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the forwarder must never take anything else down
            logger.error("copilot_forward_worker_crashed", extra={"call_id": call.call_id, "error": type(exc).__name__})

    async def _send(self, call: _Call, item: _Item) -> bool:
        url = self._url if item.kind == "transcript" else self._calls_url
        attempts = 2 if item.partial else self._max_attempts  # partials are superseded: do not hold up finals
        delay = self._backoff_s
        for attempt in range(1, attempts + 1):
            reason: str
            try:
                response = await self._http.post(url, json=item.body, headers={TOKEN_HEADER: self._token})
                code = response.status_code
                first = call.last_status is None
                call.last_status = code
                if code < 300:
                    if first:  # positive proof in the log that this call reaches the Copilot
                        logger.info("copilot_forward_first_delivery_ok", extra={"call_id": call.call_id, "status": code, "kind": item.kind})
                    return True
                if code not in TRANSIENT_STATUS and code < 500:
                    logger.error(
                        "copilot_forward_rejected",
                        extra={"call_id": call.call_id, "event_id": item.body["event_id"], "status": code},
                    )
                    return False
                reason = f"http_{code}"
            except httpx.HTTPError as exc:
                reason = type(exc).__name__
            if attempt == attempts:
                logger.error(
                    "copilot_forward_gave_up",
                    extra={"call_id": call.call_id, "event_id": item.body["event_id"], "attempts": attempts, "reason": reason},
                )
                return False
            logger.warning(
                "copilot_forward_retry",
                extra={"call_id": call.call_id, "attempt": attempt, "reason": reason},
            )
            await self._sleep(delay * (0.75 + random.random() * 0.5))
            delay = min(delay * 2 if delay else self._backoff_s, self._max_backoff_s)
        return False

class CopilotCallListener:
    """CallStateManager listener: tells the Copilot when the AI is connected and when the call ends."""

    def __init__(self, forwarder: CopilotEventForwarder, *, conversation_enabled: bool) -> None:
        self._forwarder = forwarder
        self._conversation_enabled = conversation_enabled

    async def on_stream_start(self, session: Any) -> None:
        if session.kind == "primary" and self._conversation_enabled:
            self._forwarder.emit_call_event(session.call_id, "ai_agent_connected")

    async def on_stream_end(self, session: Any, reason: str) -> None:
        return None

    async def on_dtmf(self, session: Any, event: Any) -> None:
        return None

    async def on_call_ended(self, call_id: str, reason: str) -> None:
        self._forwarder.emit_call_event(call_id, "call_ended", {"reason": reason})
        await self._forwarder.finish_call(call_id)
