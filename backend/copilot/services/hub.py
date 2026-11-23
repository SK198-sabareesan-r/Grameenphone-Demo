"""Fan-out of live events to connected agent panels."""

import json
import logging

from fastapi import WebSocket

log = logging.getLogger(__name__)


class PanelHub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def send(self, ws: WebSocket, event: str, data: object) -> None:
        await ws.send_text(json.dumps({"type": event, "data": data}, ensure_ascii=False))

    async def broadcast(self, event: str, data: object) -> None:
        message = json.dumps({"type": event, "data": data}, ensure_ascii=False)
        for ws in list(self._clients):
            try:
                await ws.send_text(message)
            except Exception:  # client went away
                log.debug("Dropping panel client after send failure")
                self._clients.discard(ws)
