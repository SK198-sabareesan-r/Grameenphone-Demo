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
    normalize_speaker,
)
from copilot.services.sessions import CallSession

log = logging.getLogger(__name__)
SOURCES = ("typed",)  # the voice-agent source is added with the voice integration (M3)


def session_view(c: Container, session: CallSession) -> dict:
    """Everything the panel needs to draw the current call."""
    return {
        "call_sid": session.call_sid,
        "ended": session.ended_at is not None,
        "turns": [t.__dict__ for t in session.turns],
        "card": session.card(),
        "mood": session.mood,
        "compliance": c.compliance.view(session.compliance),
        "suggestions": list(session.suggestions.values()),
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

    async def begin(self, call_sid: str | None, caller: str, source: str = "typed") -> None:
        if source not in SOURCES:
            raise ValueError(f"unknown call source {source!r}")
        c = self._c
        customer = c.customers.resolve(caller)
        self.source = source
        self.session = c.registry.start(
            CallSession(
                call_sid=call_sid or f"local-{uuid.uuid4().hex[:8]}",
                customer=customer,
                notice=c.customers.notice_for(customer),
                compliance=c.compliance.initial_states(),
            ),
            pipeline=self,
        )
        await c.hub.broadcast("call.started", session_view(c, self.session))

    # ---- Transcript handling ----

    async def ingest(self, speaker: str, kind: str, text: str) -> None:
        """Handle one transcript event. kind is 'partial' or 'final'.

        speaker is a participant role (customer | ai_agent | human_agent); "agent" means human_agent.
        """
        role = normalize_speaker(speaker)
        text = text.strip()
        session = self.session
        if not text or session is None or session.ended_at is not None:
            return
        c = self._c
        if kind == "partial":
            await c.hub.broadcast("transcript", {"speaker": role, "kind": "partial", "text": text})
            return
        turn = session.add_turn(role, text)
        await c.hub.broadcast("transcript", {**turn.__dict__, "kind": "final"})
        await self._after_final_turn(session, role, text)

    async def _after_final_turn(self, session: CallSession, role: str, text: str) -> None:
        """Per-role effects. AI-agent turns are stored and shown, but never change compliance or
        suggestions and do not trigger analysis on their own (they are context for the next one)."""
        c = self._c
        if role == CUSTOMER and not session.card_visible:
            session.card_visible = True
            await c.hub.broadcast("card", session.card())
        if role in COMPLIANCE_SPEAKERS and c.compliance.evaluate(session.compliance, role, text):
            await c.hub.broadcast("compliance", c.compliance.view(session.compliance))
        if role in SUGGESTION_SPEAKERS and c.nba.mark_used_by_speech(session, text):
            await c.hub.broadcast("suggestions", list(session.suggestions.values()))
        if role in ANALYSIS_TRIGGERS:
            c.analyzer.schedule(session)

    # ---- End of call ----

    async def finish(self) -> None:
        session = self.session
        if session is None or session.ended_at is not None:
            return
        session.ended_at = time.time()
        await self._c.hub.broadcast("call.ended", {"call_sid": session.call_sid})
        asyncio.create_task(self._c.summarizer.run(session))
