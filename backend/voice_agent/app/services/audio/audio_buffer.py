"""Bounded frame queue for inbound audio (one per stream; the only inbound audio buffer).

Real-time audio must not grow memory without limit when a consumer is slow or absent, so a full
queue discards its OLDEST frame (keeping latency bounded) and counts the loss in `dropped`.
Frames still buffered when the queue closes are counted in `discarded_on_close`. Single consumer
(see app/services/voice/audio_consumer.py). Nothing here is persisted.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass

from app.services.audio.formats import AudioFormat


@dataclass(frozen=True, slots=True)
class AudioFrame:
    data: bytes  # raw bytes exactly as received (not converted, not byte-swapped)
    format: AudioFormat
    track: str
    timestamp_ms: int  # Vobiz stream clock, not ours
    chunk: int
    sequence: int | None
    stream_id: str
    received_at: float = 0.0  # time.monotonic() when the frame arrived; used for consumer latency


class BoundedFrameQueue:
    def __init__(self, max_frames: int) -> None:
        if max_frames < 1:
            raise ValueError("max_frames must be >= 1")
        self._max = max_frames
        self._items: deque[AudioFrame] = deque()
        self._available = asyncio.Event()
        self._closed = False
        self.enqueued = 0
        self.dropped = 0
        self.high_water = 0
        self.discarded_on_close = 0
        self.rejected_after_close = 0  # frames offered after the stream ended (never stored)
        self._sealed = False

    def __len__(self) -> int:
        return len(self._items)

    @property
    def closed(self) -> bool:
        """True once the queue accepts no more frames (sealed or closed)."""
        return self._closed or self._sealed

    @property
    def sealed(self) -> bool:
        return self._sealed and not self._closed

    def put(self, frame: AudioFrame) -> bool:
        """Store a frame. Returns False if the queue is closed/sealed or an old frame had to be dropped."""
        if self._closed or self._sealed:
            self.rejected_after_close += 1
            return False
        dropped_one = False
        if len(self._items) >= self._max:
            self._items.popleft()
            self.dropped += 1
            dropped_one = True
        self._items.append(frame)
        self.enqueued += 1
        self.high_water = max(self.high_water, len(self._items))
        self._available.set()
        return not dropped_one

    async def get(self) -> AudioFrame | None:
        """Next frame, or None once the queue is closed."""
        while True:
            if self._closed:
                return None
            if self._items:
                frame = self._items.popleft()
                if not self._items and not self._sealed:
                    self._available.clear()
                return frame
            if self._sealed:
                return None  # sealed and fully drained
            await self._available.wait()

    def seal(self) -> None:
        """Stop accepting frames but keep buffered ones so the consumer can drain them. Idempotent.

        After sealing, get() returns the remaining frames in order, then None.
        """
        self._sealed = True
        self._available.set()

    @property
    def capacity(self) -> int:
        return self._max

    def close(self) -> int:
        """Discard buffered audio and wake any consumer. Idempotent.

        Returns how many buffered frames were discarded by THIS call (counted in
        `discarded_on_close`, so no frame disappears without a trace).
        """
        discarded = 0 if self._closed else len(self._items)
        self.discarded_on_close += discarded
        self._closed = True
        self._items.clear()
        self._available.set()
        return discarded

    def stats(self) -> dict[str, int]:
        return {
            "enqueued": self.enqueued,
            "dropped": self.dropped,
            "high_water": self.high_water,
            "buffered": len(self._items),
            "discarded_on_close": self.discarded_on_close,
            "rejected_after_close": self.rejected_after_close,
        }
