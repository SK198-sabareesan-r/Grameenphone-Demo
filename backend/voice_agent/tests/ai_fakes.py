"""Fakes shared by the AI voice conversation tests. No provider is ever contacted."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from app.integrations.vobiz.stream_sender import VobizStreamSender
from app.publishers.transcripts import TRANSCRIPT_FINAL, TRANSCRIPT_PARTIAL, VAD_SPEECH_END, VAD_SPEECH_START, TranscriptEvent
from app.services.audio.audio_buffer import BoundedFrameQueue
from app.services.audio.formats import ENC_L16, AudioFormat
from app.services.llm.base import LlmMessage, LlmResult
from app.services.sarvam.tts import TtsAudio
from app.services.voice.call_manager import StreamSession
from app.services.voice.playback import PlaybackController

L16_16K = AudioFormat(ENC_L16, 16000)
PCM_BYTES = 3200  # 100 ms at 16 kHz -> 5 playAudio chunks of 20 ms


def marker_for(text: str) -> int:
    """A byte value (1..250) derived from the spoken text, so tests can tell whose audio was played."""
    return sum(text.encode()) % 250 + 1


class FakeLlm:
    def __init__(self, reply_fn: Callable[[list[LlmMessage]], str] | None = None) -> None:
        self.reply_fn = reply_fn or (lambda messages: f"Reply to {messages[-1].content}")
        self.calls: list[tuple[str, list[LlmMessage]]] = []
        self.gate: asyncio.Event | None = None
        self.errors: list[BaseException] = []
        self.cancelled = 0

    async def generate(self, *, system: str, messages) -> LlmResult:
        self.calls.append((system, list(messages)))
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self.errors:
            raise self.errors.pop(0)
        return LlmResult(text=self.reply_fn(list(messages)), latency_ms=1.0, input_tokens=3, output_tokens=4)


class FakeTts:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.gates: list[asyncio.Event] = []  # consumed one per call, in order
        self.errors: list[BaseException | None] = []  # consumed one per call; None = succeed
        self.empty_next = False
        self.cancelled = 0

    async def synthesize(self, text: str) -> TtsAudio:
        self.texts.append(text)
        try:
            if self.gates:
                await self.gates.pop(0).wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        pcm = b"" if self.empty_next else bytes([marker_for(text)]) * PCM_BYTES
        self.empty_next = False
        return TtsAudio(pcm=pcm, format=L16_16K, duration_ms=100.0, source_codec="wav", source_sample_rate=16000,
                        resampled=False, request_id="req-test", latency_ms=1.0)


class Wire:
    """Stands in for the Vobiz WebSocket: records every message the server sends."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.fail = False
        self.acked: set[str] = set()

    async def send(self, text: str) -> None:
        if self.fail:
            raise ConnectionError("socket closed")
        self.messages.append(json.loads(text))

    def of(self, event: str) -> list[dict]:
        return [m for m in self.messages if m["event"] == event]

    def pcm_of(self, checkpoint_index: int) -> bytes:
        """Audio of the n-th utterance (the playAudio chunks before that checkpoint)."""
        import base64

        out, seen = [], 0
        for m in self.messages:
            if m["event"] == "playAudio":
                out.append(base64.b64decode(m["media"]["payload"]))
            elif m["event"] == "checkpoint":
                if seen == checkpoint_index:
                    return b"".join(out)
                seen += 1
                out = []
        return b"".join(out)


def make_stream(call_id: str, stream_id: str = "s1", *, checkpoint_timeout_s: float = 5.0,
                kind: str = "primary") -> tuple[StreamSession, Wire]:
    wire = Wire()
    sender = VobizStreamSender(stream_id, wire.send)
    session = StreamSession(
        call_id=call_id, stream_id=stream_id, account_id="1", tracks=("inbound",), audio_format=L16_16K,
        queue=BoundedFrameQueue(10), sender=sender,
        playback=PlaybackController(sender, checkpoint_timeout_s=checkpoint_timeout_s, call_id=call_id),
        kind=kind,
    )
    return session, wire


def transcript(call_id: str, type_: str, seq: int, text: str | None = None, *, stream_id: str = "s1",
               track: str | None = None, speaker: str = "customer", source: str = "sarvam") -> TranscriptEvent:
    return TranscriptEvent(
        call_id=call_id, stream_id=stream_id, sequence=seq, type=type_, received_at="2026-01-01T00:00:00Z",
        source=source, speaker=speaker, is_final=type_ == TRANSCRIPT_FINAL, text=text, track=track,
        utterance_idx=seq,
    )


def final(call_id: str, text: str, seq: int, **kw) -> TranscriptEvent:
    return transcript(call_id, TRANSCRIPT_FINAL, seq, text, **kw)


def partial(call_id: str, text: str, seq: int, **kw) -> TranscriptEvent:
    return transcript(call_id, TRANSCRIPT_PARTIAL, seq, text, **kw)


def speech_start(call_id: str, seq: int, **kw) -> TranscriptEvent:
    return transcript(call_id, VAD_SPEECH_START, seq, **kw)


def speech_end(call_id: str, seq: int, **kw) -> TranscriptEvent:
    return transcript(call_id, VAD_SPEECH_END, seq, **kw)


async def settle(predicate, timeout: float = 3.0) -> None:
    async def _wait() -> None:
        while not predicate():
            await asyncio.sleep(0.002)

    await asyncio.wait_for(_wait(), timeout)
