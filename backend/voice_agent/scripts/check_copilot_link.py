"""Check the voice agent -> Copilot link with the REAL settings from config/.env. No Vobiz or Sarvam call.

    python -m scripts.check_copilot_link [--copilot http://localhost:8000] [--panel-user NAME --panel-password PW]

It prints the effective forwarding settings (never the token), then sends one synthetic call
(call_started, a partial and a final customer line, an AI reply, call_ended) through the same
CopilotEventForwarder the voice agent uses, and reports the HTTP status the Copilot answered with.
With --panel-user it also opens the Copilot panel WebSocket and lists the events it receives.

The synthetic call id starts with DIAG-, so its files under the Copilot's data/conversations/ are easy to find
and delete. Run it against a Copilot you are happy to add one test call to.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

import httpx

from app.core.config import Settings
from app.publishers.copilot_forwarder import CopilotEventForwarder
from app.publishers.transcripts import TRANSCRIPT_FINAL, TRANSCRIPT_PARTIAL, TranscriptEvent


def stt(call: str, kind: str, seq: int, text: str) -> TranscriptEvent:
    return TranscriptEvent(
        call_id=call, stream_id="diag-stream", sequence=seq, type=kind, received_at="2026-11-24T10:30:00Z",
        text=text, utterance_idx=seq, is_final=kind == TRANSCRIPT_FINAL,
    )


async def panel_listener(base: str, user: str, password: str, seen: list[dict], ready: asyncio.Event) -> None:
    import websockets

    async with httpx.AsyncClient(base_url=base, timeout=10) as http:
        login = await http.post("/api/auth/login", json={"username": user, "password": password})
        login.raise_for_status()
        auth = {"Authorization": f"Bearer {login.json()['token']}"}
        ticket = (await http.post("/api/auth/ws-ticket", headers=auth)).json()["ticket"]
    url = base.replace("http", "ws", 1) + f"/api/panel/stream?ticket={ticket}"
    async with websockets.connect(url) as ws:
        ready.set()
        try:
            while True:
                seen.append(json.loads(await asyncio.wait_for(ws.recv(), 6)))
        except (asyncio.TimeoutError, Exception):
            return


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--copilot", default="http://localhost:8000")
    ap.add_argument("--panel-user")
    ap.add_argument("--panel-password")
    ap.add_argument("--save-panel-events", help="write the received panel events to this JSON file")
    args = ap.parse_args()

    settings = Settings.from_env()
    forwarder = CopilotEventForwarder(settings)
    info = forwarder.describe()
    print("forwarding:", json.dumps(info))
    if not forwarder.enabled:
        print(f"NOT SENDING: {info['disabled_reason']}. Fix config/.env and restart the voice agent.")
        return 2

    async with httpx.AsyncClient(timeout=5) as http:
        try:
            health = await http.get(args.copilot.rstrip("/") + "/api/health")
            print("copilot /api/health:", health.status_code)
        except httpx.HTTPError as exc:
            print("copilot unreachable:", type(exc).__name__)
            return 3

    seen: list[dict] = []
    listener = None
    if args.panel_user:
        ready = asyncio.Event()
        listener = asyncio.create_task(panel_listener(args.copilot.rstrip("/"), args.panel_user, args.panel_password or "", seen, ready))
        await asyncio.wait_for(ready.wait(), 10)

    call = f"DIAG-{uuid.uuid4().hex[:8]}"
    forwarder.emit_call_event(call, "call_started", {"from_number": "+8801700000000", "direction": "inbound"})
    forwarder.emit_call_event(call, "ai_agent_connected")
    await forwarder.on_event(stt(call, TRANSCRIPT_PARTIAL, 1, "amar net kaj korche"))
    await forwarder.on_event(stt(call, TRANSCRIPT_FINAL, 2, "amar net kaj korche na"))
    forwarder.emit_ai_message(call, "Ami dekhchi. Ektu opekkha korun.")
    forwarder.emit_call_event(call, "call_ended", {"reason": "diagnostic"})
    snap_before = forwarder.snapshot(call)
    await forwarder.finish_call(call)
    print("synthetic call:", call)
    print("forwarder state before drain:", json.dumps(snap_before))
    if listener is not None:
        await listener
        print("panel events:", [e["type"] for e in seen])
        if args.save_panel_events:
            with open(args.save_panel_events, "w", encoding="utf-8") as fh:
                json.dump(seen, fh)
    await forwarder.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
