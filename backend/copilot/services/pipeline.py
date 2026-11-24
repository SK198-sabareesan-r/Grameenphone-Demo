"""One call: audio or typed lines in, transcripts, then panel updates."""

import asyncio
import base64
import logging
import time
import uuid

from copilot.container import Container
from copilot.services.sarvam_stt import SarvamStream
from copilot.services.sessions import CallSession, Turn

log = logging.getLogger(__name__)
TRACK_SPEAKER = {"inbound": "customer", "outbound": "agent"}
CHUNK_BYTES = 800  # 100 ms of 8 kHz mulaw


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
    await pipeline.begin(call_sid=None, caller="", live_audio=False)
    return pipeline


class CallPipeline:
    def __init__(self, c: Container) -> None:
        self._c = c
        self.session: CallSession | None = None
        self._streams: dict[str, SarvamStream] = {}
        self._buffers = {speaker: bytearray() for speaker in TRACK_SPEAKER.values()}

    # ---- Twilio media stream ----

    async def handle(self, message: dict) -> None:
        event = message.get("event")
        if event == "start":
            start = message["start"]
            caller = (start.get("customParameters") or {}).get("caller", "")
            await self.begin(start.get("callSid"), caller, live_audio=True)
        elif event == "media":
            await self._on_media(message["media"])
        elif event == "stop":
            await self.finish()

    async def begin(self, call_sid: str | None, caller: str, live_audio: bool) -> None:
        c = self._c
        customer = c.customers.resolve(caller)
        self.session = c.registry.start(
            CallSession(
                call_sid=call_sid or f"local-{uuid.uuid4().hex[:8]}",
                customer=customer,
                notice=c.customers.notice_for(customer),
                compliance=c.compliance.initial_states(),
            ),
            pipeline=self,
        )
        if live_audio:
            for speaker in self._buffers:
                stream = SarvamStream(c.settings, speaker, self.ingest, self._on_status)
                stream.start()
                self._streams[speaker] = stream
        await c.hub.broadcast("call.started", session_view(c, self.session))

    async def _on_media(self, media: dict) -> None:
        speaker = TRACK_SPEAKER.get(media.get("track", "inbound"))
        if not speaker:
            return
        buffer = self._buffers[speaker]
        buffer.extend(base64.b64decode(media["payload"]))
        while len(buffer) >= CHUNK_BYTES:
            chunk = bytes(buffer[:CHUNK_BYTES])
            del buffer[:CHUNK_BYTES]
            stream = self._streams.get(speaker)
            if stream:
                await stream.send_audio(chunk)

    async def _on_status(self, speaker: str, status: str) -> None:
        await self._c.hub.broadcast("stt.status", {"speaker": speaker, "status": status})

    # ---- Transcript handling (used by speech-to-text and by typed lines) ----

    async def ingest(self, speaker: str, kind: str, text: str) -> None:
        """Handle one transcript event. kind is 'partial' or 'final'."""
        text = text.strip()
        session = self.session
        if not text or session is None or session.ended_at is not None:
            return
        c = self._c
        if kind == "partial":
            await c.hub.broadcast("transcript", {"speaker": speaker, "kind": "partial", "text": text})
            return
        turn = session.add_turn(speaker, text)
        await c.hub.broadcast("transcript", {**turn.__dict__, "kind": "final"})
        await self._after_final_turn(session, turn)

    async def _after_final_turn(self, session: CallSession, turn: Turn) -> None:
        c = self._c
        speaker, text = turn.speaker, turn.text
        if speaker == "customer" and not session.card_visible:
            session.card_visible = True
            await c.hub.broadcast("card", session.card())
        if c.compliance.evaluate(session.compliance, speaker, text):
            await c.hub.broadcast("compliance", c.compliance.view(session.compliance))
        if speaker == "agent" and c.nba.mark_used_by_speech(session, text):
            await c.hub.broadcast("suggestions", list(session.suggestions.values()))
        c.analyzer.schedule(session)
        c.assist.schedule(session, turn)  # customer sentences only; the service decides what to skip

    # ---- End of call ----

    async def finish(self) -> None:
        session = self.session
        if session is None or session.ended_at is not None:
            return
        session.ended_at = time.time()
        for stream in self._streams.values():
            await stream.close()
        await self._c.hub.broadcast("call.ended", {"call_sid": session.call_sid})
        asyncio.create_task(self._c.summarizer.run(session))
