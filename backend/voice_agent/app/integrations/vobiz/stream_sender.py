"""Application -> Vobiz WebSocket commands: playAudio, checkpoint, clearAudio, stop.

Message shapes are exactly those in the Vobiz Stream docs. Audio is sent as raw mono bytes,
base64-encoded, with the real format declared in every playAudio. Nothing is converted here.

Concurrency:
  * every message is written under `_write_lock`, so concurrent callers never interleave frames;
  * whole utterances are serialized by `_utterance_lock`, so two playAudio calls do not mix chunks;
  * clear_audio() bumps a generation counter first, so an utterance that is mid-send (or waiting
    its turn) stops before sending any further chunk once a clear has been requested.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.services.audio.formats import (
    AudioFormat,
    AudioFormatError,
    encode_payload,
    outbound_format,
    validate_raw_audio,
)

logger = logging.getLogger(__name__)

MIN_CHUNK_MS = 20  # documented recommendation: ~20-60 ms per playAudio
MAX_CHUNK_MS = 60
DEFAULT_MAX_AUDIO_BYTES = 10 * 1024 * 1024


class StreamClosedError(RuntimeError):
    """The stream is closed or the socket failed; nothing more can be sent on it."""


@dataclass(frozen=True)
class PlayResult:
    chunks_sent: int
    bytes_sent: int
    aborted: bool  # True if a clearAudio interrupted the utterance before it was fully sent


def check_chunk_ms(chunk_ms: int) -> int:
    if isinstance(chunk_ms, bool) or not isinstance(chunk_ms, int) or not MIN_CHUNK_MS <= chunk_ms <= MAX_CHUNK_MS:
        raise ValueError(f"chunk_ms must be an integer from {MIN_CHUNK_MS} to {MAX_CHUNK_MS}")
    return chunk_ms


def build_play_audio(stream_id: str, fmt: AudioFormat, chunk: bytes) -> dict[str, Any]:
    return {
        "event": "playAudio",
        "streamId": stream_id,
        "media": {
            "contentType": fmt.encoding,
            "sampleRate": fmt.sample_rate,
            "payload": encode_payload(chunk),
        },
    }


def build_checkpoint(stream_id: str, name: str) -> dict[str, Any]:
    return {"event": "checkpoint", "streamId": stream_id, "name": name}


def build_clear_audio(stream_id: str) -> dict[str, Any]:
    return {"event": "clearAudio", "streamId": stream_id}


def build_stop(stream_id: str) -> dict[str, Any]:
    return {"event": "stop", "streamId": stream_id}


def split_chunks(audio: bytes, fmt: AudioFormat, chunk_ms: int) -> list[bytes]:
    size = fmt.bytes_for_ms(check_chunk_ms(chunk_ms))
    return [audio[i : i + size] for i in range(0, len(audio), size)]


class VobizStreamSender:
    """Sends commands for ONE stream (one streamId). A reconnect gets a new sender."""

    def __init__(
        self,
        stream_id: str,
        send_text: Callable[[str], Awaitable[None]],
        *,
        chunk_ms: int = MIN_CHUNK_MS,
        max_audio_bytes: int = DEFAULT_MAX_AUDIO_BYTES,
    ) -> None:
        self.stream_id = stream_id
        self._send_text = send_text
        self._chunk_ms = check_chunk_ms(chunk_ms)
        self._max_audio_bytes = max_audio_bytes
        self._closed = False
        self._generation = 0
        self.play_audio_messages_sent = 0
        self._write_lock = asyncio.Lock()
        self._utterance_lock = asyncio.Lock()

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Mark the stream closed. Later sends raise StreamClosedError instead of touching the socket."""
        self._closed = True

    async def _send(self, message: dict[str, Any], *, generation: int | None = None) -> bool:
        """Write one message. Returns False (sending nothing) if `generation` is stale."""
        text = json.dumps(message, separators=(",", ":"))
        async with self._write_lock:
            if self._closed:
                raise StreamClosedError("stream is closed")
            if generation is not None and generation != self._generation:
                return False
            try:
                await self._send_text(text)
                if message.get("event") == "playAudio":
                    self.play_audio_messages_sent += 1
            except Exception as exc:  # socket closed / network failure
                self._closed = True
                logger.warning(
                    "vobiz_stream_send_failed",
                    extra={"stream_id": self.stream_id, "event": message.get("event"), "error": type(exc).__name__},
                )
                raise StreamClosedError("send failed; stream closed") from exc
        return True

    async def play_audio(
        self, audio: bytes, fmt: AudioFormat, *, chunk_ms: int | None = None
    ) -> PlayResult:
        """Send one utterance as consecutive playAudio chunks.

        The format must be a documented outbound format, `audio` must be raw (no WAV/MP3 header) and
        match it. A clearAudio issued before or during the send aborts it; no chunk is sent after.
        """
        fmt = outbound_format(fmt.encoding, fmt.sample_rate)
        validate_raw_audio(audio, fmt, max_bytes=self._max_audio_bytes)
        chunks = split_chunks(audio, fmt, chunk_ms if chunk_ms is not None else self._chunk_ms)

        generation = self._generation  # captured before waiting, so a clear while queued cancels us
        sent = sent_bytes = 0
        async with self._utterance_lock:
            for chunk in chunks:
                if not await self._send(build_play_audio(self.stream_id, fmt, chunk), generation=generation):
                    return PlayResult(sent, sent_bytes, aborted=True)
                sent += 1
                sent_bytes += len(chunk)
                await asyncio.sleep(0)  # let a pending clear_audio() run between chunks
        return PlayResult(sent, sent_bytes, aborted=False)

    async def checkpoint(self, name: str) -> None:
        await self._send(build_checkpoint(self.stream_id, name))

    async def clear_audio(self) -> None:
        """Send clearAudio. Any utterance still being sent stops before its next chunk."""
        self._generation += 1
        await self._send(build_clear_audio(self.stream_id))

    async def stop(self) -> None:
        """Send stop (Vobiz then runs the next XML element, or hangs up if there is none)."""
        await self._send(build_stop(self.stream_id))


__all__ = [
    "AudioFormatError",
    "PlayResult",
    "StreamClosedError",
    "VobizStreamSender",
    "build_checkpoint",
    "build_clear_audio",
    "build_play_audio",
    "build_stop",
    "check_chunk_ms",
    "split_chunks",
]
