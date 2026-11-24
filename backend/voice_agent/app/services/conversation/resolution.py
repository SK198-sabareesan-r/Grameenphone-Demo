"""AI-first resolution tracking: when has the AI "had its chance", and should an unresolved call go to a human?

Two kinds of customer signal are recognised, both from the CUSTOMER's own words (never from text the AI generated):

* unresolved_feedback: after the AI has already replied, the customer says it did not help ("still not working",
  "I tried that", "same problem"). Each one is one failed resolution attempt.
* capability_gap: the customer asks for something the assistant cannot do by design (look at their account, balance
  or bill, raise a ticket or complaint, refund, block a SIM). The system prompt says it has no account access.

A report of a problem is NOT a signal: "my network is slow" is something the AI should try to help with first.

Escalation is decided in two steps, so one failed tip does not send every caller to a human:

1. Counting: `AI_MAX_RESOLUTION_ATTEMPTS` unresolved reports, or one capability gap, make the call ELIGIBLE.
2. A structured assessment by the LLM (strict JSON schema, validated here) says whether the assistant still has a useful
   step to offer. Only `escalate=true` starts a transfer. If the model's answer is missing, late or invalid, the
   call is escalated, because the customer has already told us twice that it is not working. After one "another step
   is available" answer, the next eligible signal escalates without asking again, so the AI cannot loop forever.

Nothing here reads the AI's reply text, and logs carry counts and enum values, never sentences.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from app.services.conversation.handoff_intent import normalise

logger = logging.getLogger(__name__)

UNRESOLVED, CAPABILITY_GAP = "unresolved_feedback", "capability_gap"
MAX_TRACKED_CALLS = 10_000

_UNRESOLVED_EN = (
    "still not", "still slow", "still no", "still the same", "still having", "still facing", "still same", "not working", "didn t work", "doesn t work", "did not work", "does not work", "no change", "same problem",
    "same issue", "not fixed", "tried that", "already tried", "i tried", "not helping", "didn t help", "doesn t help",
    "did not help", "nothing happened", "no improvement", "not solved", "not resolved", "didn t fix", "did not fix",
)
_UNRESOLVED_BN = ("এখনো", "এখনও", "ঠিক হয়নি", "কাজ করেনি", "সমাধান হয়নি", "হয়নি")
_UNRESOLVED_BL = ("ekhono", "thik hoy ni", "thik hoyni", "kaj kore ni", "hoy ni", "hoyni", "tarpor o", "same problem")
_RESOLVED = ("thank you", "thanks", "it worked", "working now", "works now", "fixed", "solved", "that helps", "that helped", "got it", "that s all", "all good")
_ACCOUNT_NOUNS = r"(?:balance|bill|usage|account|package|plan|sim|credit|recharge history|data balance|minutes|validity)"
_ACCOUNT_REQUEST = re.compile(
    rf"\b(?:check|see|tell|show|know|what is|what s|how much|how many|when does|change|activate|deactivate|block|cancel|unblock)\b"
    rf"[^.?!]{{0,40}}\b(?:my|the)\s+{_ACCOUNT_NOUNS}\b"
    r"|\b(?:refund|raise a ticket|raise ticket|create a ticket|open a ticket|file a complaint|lodge a complaint|register a complaint|"
    r"make a complaint|complaint number|ticket)\b",
    re.IGNORECASE,
)

ASSESSMENT_SYSTEM_PROMPT = """\
You review a phone conversation between a customer and a virtual assistant of Grameenphone Customer Care.
The assistant can only give general information and general troubleshooting steps. It has NO access to the
customer's account, balance, bill, plan or usage, and it cannot perform actions such as refunds, raising tickets,
blocking a SIM or changing a plan.
Decide whether the call should now be handed to a human agent.
Answer with ONE JSON object and nothing else, exactly these keys:
{"escalate": true or false, "reason": one of "needs_account_access", "needs_action", "troubleshooting_exhausted",
 "other_step_available", "customer_frustrated", "next_step_available": true or false}
Rules: set escalate to true when the customer needs account access or an action, or when the assistant has no further
useful general step. Set escalate to false (reason "other_step_available") only if there is still a specific, different
general step the assistant has not offered yet. Never output anything outside the JSON object.
"""


class EscalationDecision(BaseModel):
    """The only shape the model may answer with. Anything else is rejected, never guessed at."""

    model_config = ConfigDict(extra="forbid")

    escalate: bool
    reason: Literal[
        "needs_account_access", "needs_action", "troubleshooting_exhausted", "other_step_available", "customer_frustrated"
    ]
    next_step_available: bool

    @model_validator(mode="after")
    def _consistent(self) -> "EscalationDecision":
        if self.escalate and self.next_step_available:
            raise ValueError("escalate and next_step_available contradict each other")
        if not self.escalate and not self.next_step_available:
            raise ValueError("not escalating needs a next step")
        return self


def parse_decision(text: str) -> EscalationDecision | None:
    """Strictly validate the model's answer. The first {...} block must match the schema, else None."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return EscalationDecision.model_validate(json.loads(text[start:end + 1]))
    except (ValueError, ValidationError):
        return None


def classify_customer_sentence(text: str, ai_replies: int) -> str | None:
    """UNRESOLVED, CAPABILITY_GAP or None. `ai_replies` = how many answers the AI already gave this call."""
    norm = normalise(text)
    if not norm:
        return None
    if _ACCOUNT_REQUEST.search(text.lower().replace("'", " ")):
        return CAPABILITY_GAP
    if ai_replies >= 1:  # "still not working" before the AI said anything is just the first report of the problem
        padded = f" {norm} "
        if any(f" {p} " in padded for p in _UNRESOLVED_EN + _UNRESOLVED_BL) or any(p in norm for p in _UNRESOLVED_BN):
            if not any(f" {r} " in padded for r in _RESOLVED):
                return UNRESOLVED
    return None


def looks_resolved(text: str) -> bool:
    padded = f" {normalise(text)} "
    return any(f" {r} " in padded for r in _RESOLVED)


@dataclass
class _CallState:
    attempts: int = 0
    gaps: int = 0
    assessments: int = 0
    last_signal: str | None = None  # last customer signal: UNRESOLVED, CAPABILITY_GAP or "resolved"
    escalated: bool = False
    customer_turns: int = 0


@dataclass
class Observation:
    """What one customer sentence means for the resolution flow."""

    signal: str | None = None
    attempt: int = 0  # unresolved reports so far
    eligible: bool = False  # the call may now be escalated (subject to assessment)


@dataclass
class ResolutionPolicy:
    max_attempts: int = 2
    auto_handoff: bool = True  # AI_AUTO_HANDOFF_ON_UNRESOLVED and a handoff mode that allows it
    assess_timeout_s: float = 8.0
    _calls: dict[str, _CallState] = field(default_factory=dict, repr=False)

    def _state(self, call_id: str) -> _CallState:
        state = self._calls.get(call_id)
        if state is None:
            if len(self._calls) >= MAX_TRACKED_CALLS:
                self._calls.pop(next(iter(self._calls)))
            state = self._calls[call_id] = _CallState()
        return state

    def observe(self, call_id: str, text: str, ai_replies: int) -> Observation:
        """Record one final customer sentence. Pure bookkeeping: no I/O, never raises."""
        state = self._state(call_id)
        state.customer_turns += 1
        if looks_resolved(text) and ai_replies >= 1:
            state.last_signal = "resolved"
            return Observation()
        signal = classify_customer_sentence(text, ai_replies)
        if signal is None:
            return Observation()
        state.last_signal = signal
        if signal == UNRESOLVED:
            state.attempts += 1
        else:
            state.gaps += 1
        eligible = self.auto_handoff and not state.escalated and (
            state.gaps >= 1 or state.attempts >= self.max_attempts
        )
        return Observation(signal=signal, attempt=state.attempts, eligible=eligible)

    async def assess(self, llm: Any, call_id: str, messages: list[Any]) -> EscalationDecision:
        """Ask the model, once per eligibility, whether the assistant still has a useful step. Strict schema.
        The second eligible moment escalates without asking again. Any failure escalates (see module doc)."""
        state = self._state(call_id)
        state.assessments += 1
        fallback = EscalationDecision(escalate=True, reason="troubleshooting_exhausted", next_step_available=False)
        if state.assessments > 1:  # the model already said "one more step" once: no endless loop
            return fallback
        convo = "\n".join(f"{'CUSTOMER' if m.role == 'user' else 'ASSISTANT'}: {m.content}" for m in messages[-8:])
        try:
            from app.services.llm.base import LlmMessage

            result = await asyncio.wait_for(
                llm.generate(system=ASSESSMENT_SYSTEM_PROMPT, messages=[LlmMessage(role="user", content=convo)]),
                self.assess_timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("ai_assessment_failed", extra={"call_id": call_id, "error": type(exc).__name__})
            return fallback
        decision = parse_decision(getattr(result, "text", "") or "")
        if decision is None:
            logger.warning("ai_assessment_invalid", extra={"call_id": call_id})
            return fallback
        logger.info("ai_assessment", extra={"call_id": call_id, "escalate": decision.escalate, "reason": decision.reason})
        return decision

    def mark_escalated(self, call_id: str) -> None:
        self._state(call_id).escalated = True

    def reopen(self, call_id: str) -> None:
        """The transfer did not happen; the AI has the call again and may escalate later."""
        state = self._state(call_id)
        state.escalated = False
        state.attempts = 0
        state.gaps = 0

    def outcome(self, call_id: str, handoff_status: str | None) -> str:
        """How the call ended, for the Copilot: human_handled | unresolved | ai_resolved | no_interaction."""
        state = self._calls.get(call_id)
        if handoff_status in ("connected", "completed"):
            return "human_handled"
        if state is None or state.customer_turns == 0:
            return "no_interaction"
        if handoff_status == "failed" or state.last_signal in (UNRESOLVED, CAPABILITY_GAP):
            return "unresolved"
        return "ai_resolved"

    def forget(self, call_id: str) -> None:
        self._calls.pop(call_id, None)
