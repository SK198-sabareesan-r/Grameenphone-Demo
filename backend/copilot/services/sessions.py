"""In-memory state for the call that is on screen."""

import time
from dataclasses import dataclass, field


@dataclass
class Turn:
    id: int
    speaker: str
    text: str
    ts: float


@dataclass
class CallSession:
    call_sid: str
    customer: dict | None
    notice: dict | None
    compliance: dict[str, str]
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    turns: list[Turn] = field(default_factory=list)
    card_visible: bool = False
    mood: dict = field(
        default_factory=lambda: {"customer": "neutral", "agent": "neutral", "tip": None}
    )
    mood_history: list[str] = field(default_factory=list)
    suggestions: dict[str, dict] = field(default_factory=dict)
    summary: dict | None = None
    ticket: dict | None = None
    recording_url: str = ""

    def add_turn(self, speaker: str, text: str) -> Turn:
        turn = Turn(id=len(self.turns) + 1, speaker=speaker, text=text, ts=time.time())
        self.turns.append(turn)
        return turn

    def transcript_text(self, last: int | None = None) -> str:
        turns = self.turns[-last:] if last else self.turns
        return "\n".join(f"{t.speaker.upper()}: {t.text}" for t in turns)

    def card(self) -> dict | None:
        if not self.card_visible:
            return None
        return {"customer": self.customer, "notice": self.notice}

    def to_record(self) -> dict:
        return {
            "call_sid": self.call_sid,
            "customer_id": (self.customer or {}).get("id"),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "turns": [t.__dict__ for t in self.turns],
            "mood_history": self.mood_history,
            "suggestions": list(self.suggestions.values()),
            "compliance": self.compliance,
            "summary": self.summary,
            "ticket": self.ticket,
            "recording_url": self.recording_url,
        }


class SessionRegistry:
    def __init__(self) -> None:
        self.current: CallSession | None = None
        self.pipeline = None  # the CallPipeline driving the current call
        self._by_sid: dict[str, CallSession] = {}

    def start(self, session: CallSession, pipeline=None) -> CallSession:
        self.current = session
        self.pipeline = pipeline
        self._by_sid[session.call_sid] = session
        return session

    def get(self, call_sid: str) -> CallSession | None:
        return self._by_sid.get(call_sid)

    def reset(self) -> None:
        self.current = None
        self.pipeline = None
        self._by_sid.clear()
