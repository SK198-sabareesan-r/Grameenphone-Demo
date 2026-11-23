"""Sarvam realtime speech-to-text over WebSocket. One stream per speaker."""

import asyncio
import base64
import json
import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlencode

from websockets.asyncio.client import connect

from config.settings import Settings

log = logging.getLogger(__name__)
TranscriptHandler = Callable[[str, str, str], Awaitable[None]]  # speaker, kind, text
StatusHandler = Callable[[str, str], Awaitable[None]]  # speaker, status
KEEPALIVE_SECONDS = 15


def build_stt_url(settings: Settings) -> str:
    params = {
        "language_code": settings.sarvam_stt_language,
        "stream_type": settings.sarvam_stt_stream_type,
        "mode": settings.sarvam_stt_mode,
        "encoding": "mulaw",
        "sample_rate": 8000,
        "silence_duration_ms": settings.sarvam_stt_silence_ms,
    }
    if settings.sarvam_stt_model:
        params["model"] = settings.sarvam_stt_model
    if settings.stt_keyterms:
        params["keyterms"] = json.dumps(settings.stt_keyterms, ensure_ascii=False)
    return f"{settings.sarvam_stt_url}?{urlencode(params)}"


class SarvamStream:
    def __init__(
        self,
        settings: Settings,
        speaker: str,
        on_transcript: TranscriptHandler,
        on_status: StatusHandler,
    ) -> None:
        self._settings = settings
        self._speaker = speaker
        self._on_transcript = on_transcript
        self._on_status = on_status
        self._ws = None
        self._task: asyncio.Task | None = None
        self._closing = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def send_audio(self, chunk: bytes) -> None:
        if self._ws is None:
            return  # connection is down, drop audio instead of queueing stale speech
        payload = {"event": "audio_input", "audio": base64.b64encode(chunk).decode("ascii")}
        try:
            await self._ws.send(json.dumps(payload))
        except Exception:
            log.debug("STT send failed for %s", self._speaker)

    async def close(self) -> None:
        self._closing = True
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"event": "end"}))
                await self._ws.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        retries = 0
        while not self._closing and retries <= self._settings.sarvam_stt_max_retries:
            try:
                await self._connect_and_listen()
                retries = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("STT stream for %s failed", self._speaker)
            self._ws = None
            if self._closing:
                break
            retries += 1
            await self._on_status(self._speaker, "reconnecting")
            await asyncio.sleep(min(2**retries, 8))
        if not self._closing:
            await self._on_status(self._speaker, "failed")

    async def _connect_and_listen(self) -> None:
        key = self._settings.sarvam_api_key.get_secret_value()
        headers = {"api-subscription-key": key}
        async with connect(build_stt_url(self._settings), additional_headers=headers) as ws:
            self._ws = ws
            await self._on_status(self._speaker, "connected")
            keepalive = asyncio.create_task(self._keepalive(ws))
            try:
                async for raw in ws:
                    await self._handle_message(raw)
            finally:
                keepalive.cancel()

    async def _keepalive(self, ws) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_SECONDS)
            await ws.send(json.dumps({"event": "ping"}))

    async def _handle_message(self, raw: str | bytes) -> None:
        data = json.loads(raw)
        event = data.get("event") or data.get("type") or ""
        if event in ("transcript.partial", "transcript.final"):
            text = data.get("text") or data.get("transcript") or ""
            kind = "final" if event.endswith("final") else "partial"
            await self._on_transcript(self._speaker, kind, text)
        elif event == "error":
            log.error("Sarvam STT error for %s: %s", self._speaker, data)
