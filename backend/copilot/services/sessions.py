"""In-memory state for the call that is on screen."""

import time
from dataclasses import dataclass, field

from copilot.models.participants import TRANSCRIPT_LABELS

MAX_ANSWERS = 20


@dataclass
class Turn:
    id: int
    speaker: str  # a participant role: customer | ai_agent | human_agent
    text: str
    ts: float


@dataclass
class CallSession:
    call_sid: str
    compliance: dict[str, str]
    caller_number: str = ""  # only a hint for the agent; nothing is loaded from it
    customer: dict | None = None  # set only when the agent confirms the mobile number
    registered_status: dict | None = None  # network status at the area registered with that number
    current_location: dict | None = None  # network status where the customer says they are now
    location_prompt: bool = False
    heard: dict | None = None  # the last mobile number heard on the call: {number, found, speaker}
    ignored_number: str | None = None  # a number the agent rejected ("not this customer"); not loaded again
    pending_area: str | None = None  # an area the agent named, waiting for the customer to say yes or no
    identity_confirmed: bool = False  # the customer confirmed the mobile number. False while the card comes only from the caller ID
    pending_confirm: bool = False  # the agent read the number back, waiting for the customer to say yes or no
    identified_at: float | None = None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    turns: list[Turn] = field(default_factory=list)
    mood: dict = field(
        default_factory=lambda: {"customer": "neutral", "agent": "neutral", "tip": None}
    )
    mood_history: list[str] = field(default_factory=list)
    suggestions: dict[str, dict] = field(default_factory=dict)
    answers: list[dict] = field(default_factory=list)
    summary: dict | None = None
    ticket: dict | None = None
    recording_url: str = ""
    # Set by voice calls (M3). status: in_progress | handoff_requested | dialing_human | with_human |
    # on_hold | human_disconnected | ended. agent_mode: ai_agent | human_agent (None for typed calls).
    status: str = "in_progress"
    agent_mode: str | None = None

    def add_turn(self, speaker: str, text: str, ts: float | None = None) -> Turn:
        turn = Turn(id=len(self.turns) + 1, speaker=speaker, text=text, ts=ts or time.time())
        self.turns.append(turn)
        return turn

    def identify(self, customer: dict, registered_status: dict, confirmed: bool = True) -> None:
        self.customer = customer
        self.registered_status = registered_status
        self.identified_at = time.time()
        self.identity_confirmed = confirmed
        self.pending_confirm = False

    def clear_identity(self) -> None:
        self.customer = self.registered_status = self.identified_at = self.heard = None
        self.identity_confirmed = self.pending_confirm = False

    def set_location(self, status: dict) -> None:
        self.current_location = status
        self.location_prompt = False

    def network_view(self) -> dict:
        """Both network statuses, for the model. Either can be None."""
        return {"registered_location": self.registered_status, "current_location": self.current_location}

    def set_answer(self, entry: dict) -> None:
        """Keep the lookup for a customer sentence. Sentences that needed no lookup are not stored."""
        if not entry.get("needs_lookup"):
            return
        self.answers = [a for a in self.answers if a["turn_id"] != entry["turn_id"]] + [entry]
        self.answers = self.answers[-MAX_ANSWERS:]

    def transcript_text(self, last: int | None = None) -> str:
        """Transcript for the model, one line per turn, labelled CUSTOMER / AI AGENT / HUMAN AGENT."""
        turns = self.turns[-last:] if last else self.turns
        return "\n".join(
            f"{TRANSCRIPT_LABELS.get(t.speaker, t.speaker.upper())}: {t.text}" for t in turns
        )

    def card(self) -> dict | None:
        """The customer card. None until the agent has confirmed the mobile number."""
        if self.customer is None:
            return None
        return {"customer": self.customer, "registered_status": self.registered_status, "confirmed": self.identity_confirmed}

    def to_record(self) -> dict:
        return {
            "call_id": self.call_sid,  # the key of the calls table
            "customer_id": (self.customer or {}).get("id"),
            "customer_name": (self.customer or {}).get("name"),
            "mobile_number": (self.customer or {}).get("msisdn"),  # the customer's original number
            "caller_number": self.caller_number or None,  # the number the call came from
            "identified_at": self.identified_at,
            "identity_confirmed": self.identity_confirmed,
            "card": self.card(),  # the full customer card as the agent saw it, for session history
            "mood": self.mood,
            "status": self.status,
            "agent_mode": self.agent_mode,
            "current_location": self.current_location,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "turns": [t.__dict__ for t in self.turns],
            "mood_history": self.mood_history,
            "suggestions": list(self.suggestions.values()),
            "answers": self.answers,
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
