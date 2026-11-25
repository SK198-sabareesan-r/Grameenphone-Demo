"""One call: transcript lines in (typed now; the voice agent later), panel updates out."""

import asyncio
import logging
import time
import uuid

from copilot.container import Container
from copilot.models.participants import (
    ANALYSIS_TRIGGERS,
    COMPLIANCE_SPEAKERS,
    CUSTOMER,
    SUGGESTION_SPEAKERS,
    UNKNOWN,
    normalize_speaker,
)
from copilot.services.sessions import CallSession, Turn

log = logging.getLogger(__name__)
SOURCES = ("typed", "voice")  # "voice" = a call forwarded by the voice agent (M3)


def session_view(c: Container, session: CallSession) -> dict:
    """Everything the panel needs to draw the current call."""
    return {
        "call_sid": session.call_sid,
        "ended": session.ended_at is not None,
        "status": session.status,
        "agent_mode": session.agent_mode,
        "turns": [t.__dict__ for t in session.turns],
        "card": session.card(),
        "caller_number": session.caller_number,
        "current_location": session.current_location,
        "location_prompt": session.location_prompt,
        "heard": session.heard,
        "mood": session.mood,
        "compliance": c.compliance.view(session.compliance),
        "suggestions": list(session.suggestions.values()),
        "answers": session.answers,
        "summary": session.summary,
        "ticket": session.ticket,
        "recording_url": session.recording_url,
    }


async def ensure_pipeline(c: Container) -> "CallPipeline":
    """Return the pipeline of the active call, or start a text-only call if there is none."""
    pipeline, session = c.registry.pipeline, c.registry.current
    if pipeline and session and session.ended_at is None:
        return pipeline
    pipeline = CallPipeline(c)
    await pipeline.begin(call_sid=None, caller="", source="typed")
    return pipeline


class CallPipeline:
    def __init__(self, c: Container) -> None:
        self._c = c
        self.session: CallSession | None = None
        self.source: str | None = None

    async def begin(
        self, call_sid: str | None, caller: str, source: str = "typed", agent_mode: str | None = None
    ) -> None:
        if source not in SOURCES:
            raise ValueError(f"unknown call source {source!r}")
        c = self._c
        self.source = source
        # No customer is loaded yet. For a voice call the card is looked up from the caller ID when the call
        # reaches the human agent (set_status). It stays "not confirmed" until the number is confirmed.
        self.session = c.registry.start(
            CallSession(
                call_sid=call_sid or f"local-{uuid.uuid4().hex[:8]}",
                compliance=c.compliance.initial_states(),
                caller_number=caller,
                agent_mode=agent_mode,  # voice calls start with the AI agent: the first panel update already says so
            ),
            pipeline=self,
        )
        await c.hub.broadcast("call.started", session_view(c, self.session))
        if source == "typed" and caller:  # a typed test call counts as handled by the human agent from the start
            await c.identity.preload(self.session)

    async def set_status(self, status: str, agent_mode: str | None = None) -> None:
        """Voice calls: record the call status and who is handling the call, and tell the panel."""
        session = self.session
        if session is None or session.ended_at is not None:
            return
        session.status = status
        if agent_mode:
            session.agent_mode = agent_mode
        if agent_mode == "human_agent":  # the call reached the human agent: look the caller up now, so the card is ready
            await self._c.identity.preload(session)
        await self._c.hub.broadcast(
            "call.status",
            {"call_sid": session.call_sid, "status": status, "agent_mode": session.agent_mode},
        )

    # ---- Transcript handling ----

    async def ingest(self, speaker: str, kind: str, text: str, ts: float | None = None) -> None:
        """Handle one transcript event. kind is 'partial' or 'final'.

        speaker is a participant role (customer | ai_agent | human_agent); "agent" means human_agent.
        "unknown" is accepted only from the voice integration, for speech whose speaker could not be
        verified: it is shown and stored, but never used for compliance, suggestions or analysis.
        `ts` is the time the speech was recognised (defaults to now).
        """
        role = UNKNOWN if speaker == UNKNOWN else normalize_speaker(speaker)
        text = text.strip()
        session = self.session
        if not text or session is None or session.ended_at is not None:
            return
        c = self._c
        if kind == "partial":
            partial = {"speaker": role, "kind": "partial", "text": text}
            if ts:  # voice calls carry the recognition time; typed lines keep the original shape
                partial["ts"] = ts
            await c.hub.broadcast("transcript", partial)
            return
        turn = session.add_turn(role, text, ts)
        await c.hub.broadcast("transcript", {**turn.__dict__, "kind": "final"})
        await self._after_final_turn(session, turn)

    async def _after_final_turn(self, session: CallSession, turn: Turn) -> None:
        """Per-role effects. AI-agent turns are stored and shown, but never change compliance or
        suggestions and do not trigger analysis on their own (they are context for the next one)."""
        c = self._c
        role, text = turn.speaker, turn.text
        if role == CUSTOMER:
            await c.location.on_customer_turn(session, text)  # asks the agent to confirm where the customer is
        await c.capture.on_turn(session, turn)  # picks up the mobile number and the location from the speech
        if role in COMPLIANCE_SPEAKERS and c.compliance.evaluate(session.compliance, role, text):
            await c.hub.broadcast("compliance", c.compliance.view(session.compliance))
        if role in SUGGESTION_SPEAKERS and c.nba.mark_used_by_speech(session, text):
            await c.hub.broadcast("suggestions", list(session.suggestions.values()))
        if role in ANALYSIS_TRIGGERS:
            c.analyzer.schedule(session)
        c.assist.schedule(session, turn)  # customer sentences only; the service decides what to skip

    # ---- End of call ----

    async def finish(self) -> None:
        session = self.session
        if session is None or session.ended_at is not None:
            return
        session.ended_at = time.time()
        session.status = "ended"
        await self._c.hub.broadcast("call.ended", {"call_sid": session.call_sid})
        asyncio.create_task(self._c.summarizer.run(session))
