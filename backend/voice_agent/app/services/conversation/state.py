"""Per-call conversation state and metrics. In memory, one object per logical call, never shared."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from app.services.llm.base import LlmMessage

MAX_SEEN_FINALS = 128


class Phase(str, Enum):
    IDLE = "idle"
    THINKING = "thinking"  # waiting for the LLM
    SYNTHESIZING = "synthesizing"  # waiting for TTS
    PLAYING = "playing"  # audio being sent to Vobiz / waiting for its playback acknowledgement


@dataclass
class CallConversation:
    call_id: str
    history_max: int = 20
    messages: list[LlmMessage] = field(default_factory=list, repr=False)
    phase: Phase = Phase.IDLE
    greeting: str = "pending"  # pending | started | played | failed | interrupted
    turn_id: int = 0  # bumped on every launch/interrupt: work for an older turn is stale
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    task_kind: str = ""  # "greeting" | "turn"
    pending_reply: bool = False  # a customer utterance arrived while the greeting was still being prepared
    playing_since: float | None = None
    closed: bool = False
    # A human agent has taken the call: the AI must not speak or answer (hand-off, M3). Reversible if the
    # hand-off fails; `pending_notice` is then spoken once the AI stream is back.
    suspended: bool = False
    pending_notice: str | None = None
    interruptions: int = 0
    turns: int = 0
    _seen: deque = field(default_factory=lambda: deque(maxlen=MAX_SEEN_FINALS), repr=False)
    _seen_set: set = field(default_factory=set, repr=False)

    @property
    def task_active(self) -> bool:
        return self.task is not None and not self.task.done()

    def seen_before(self, key: tuple) -> bool:
        """Record a final transcript's identity. True if it was already processed (duplicate delivery)."""
        if key in self._seen_set:
            return True
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(key)
        self._seen_set.add(key)
        return False

    def add(self, role: str, content: str) -> None:
        self.messages.append(LlmMessage(role=role, content=content))  # type: ignore[arg-type]
        if len(self.messages) > self.history_max:
            del self.messages[: len(self.messages) - self.history_max]

    def summary(self) -> dict[str, object]:
        """Metadata only: never message text."""
        return {
            "call_id": self.call_id, "phase": self.phase.value, "greeting": self.greeting,
            "messages": len(self.messages), "turns": self.turns, "interruptions": self.interruptions,
            "task_active": self.task_active, "suspended": self.suspended,
        }


@dataclass
class _Latency:
    count: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0

    def add(self, ms: float) -> None:
        self.count += 1
        self.total_ms += ms
        self.max_ms = max(self.max_ms, ms)

    def as_dict(self) -> dict[str, float | int]:
        avg = round(self.total_ms / self.count, 1) if self.count else 0.0
        return {"count": self.count, "avg_ms": avg, "max_ms": round(self.max_ms, 1)}


@dataclass
class ConversationMetrics:
    calls_started: int = 0
    calls_cleaned: int = 0
    greetings_attempted: int = 0
    greetings_sent: int = 0
    greetings_acknowledged: int = 0
    greetings_failed: int = 0
    utterances_processed: int = 0
    duplicate_finals: int = 0
    ignored_events: int = 0  # not customer inbound speech
    llm_requests: int = 0
    llm_failures: int = 0
    tts_requests: int = 0
    tts_failures: int = 0
    playback_sent: int = 0
    playback_acknowledged: int = 0
    playback_failed: int = 0
    playback_timeouts: int = 0
    playback_cleared: int = 0
    interruptions: int = 0
    barge_in_ignored_grace: int = 0
    tasks_cancelled: int = 0
    conversation_failures: int = 0
    contact_details_blocked: int = 0  # model replies replaced because they read out a number or address
    llm_latency: _Latency = field(default_factory=_Latency)
    tts_latency: _Latency = field(default_factory=_Latency)

    def snapshot(self) -> dict[str, object]:
        out: dict[str, object] = {
            k: v for k, v in self.__dict__.items() if isinstance(v, int) and not isinstance(v, bool)
        }
        out["llm_latency"] = self.llm_latency.as_dict()
        out["tts_latency"] = self.tts_latency.as_dict()
        return out
