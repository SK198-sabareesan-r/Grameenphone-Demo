"""Simulated Vobiz client for the /ws/vobiz/stream endpoint (manual testing, no real call).

It plays Vobiz's role: gets a stream URL from our own answer webhook, connects, sends `start`
and synthetic 20 ms `media` frames, prints what the server sends back (playAudio / checkpoint /
clearAudio), and answers checkpoints with `playedStream` and clearAudio with `clearedAudio`,
the way the Vobiz docs describe.

This only proves our server against the DOCUMENTED protocol. It is not a real-provider test.

Usage (app running locally with VOBIZ_VALIDATE_SIGNATURE=false and PUBLIC_BASE_URL set):
    python scripts/simulate_vobiz_stream.py --seconds 20
Then, from another terminal, trigger playback/clear with the internal routes (see README notes
printed at start-up).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import uuid
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import httpx
import websockets

FORMATS = {
    "l16-8k": ("audio/x-l16", 8000, 2),
    "l16-16k": ("audio/x-l16", 16000, 2),
    "mulaw-8k": ("audio/x-mulaw", 8000, 1),
}


async def get_stream_url(http_base: str, call_id: str) -> str:
    form = {"CallUUID": call_id, "From": "+10000000000", "To": "+10000000001",
            "Direction": "outbound", "Event": "StartApp", "CallStatus": "in-progress"}
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(f"{http_base}/api/vobiz/answer", data=form)
    response.raise_for_status()
    stream = ET.fromstring(response.text).find("Stream")
    if stream is None or not stream.text:
        sys.exit("answer webhook returned no <Stream>")
    # The XML points at PUBLIC_BASE_URL (wss://...). Redirect to the local server, keep path + query.
    public = urlsplit(stream.text.strip())
    local = urlsplit(http_base)
    scheme = "wss" if local.scheme == "https" else "ws"
    return urlunsplit((scheme, local.netloc, public.path, public.query, ""))


async def run(args: argparse.Namespace) -> None:
    encoding, rate, width = FORMATS[args.format]
    call_id = args.call_id or str(uuid.uuid4())
    stream_id = str(uuid.uuid4())
    ws_url = await get_stream_url(args.base, call_id)
    print(f"call_id={call_id}\nstream_id={stream_id}\nconnecting (token hidden)...")
    print(
        "\nIn another terminal, try:\n"
        f'  curl -X POST {args.base}/api/vobiz/streams/{call_id}/test-tone -H "X-Internal-Token: $INTERNAL" '
        '-H "Content-Type: application/json" -d "{\\"duration_ms\\": 1000}"\n'
        f'  curl -X POST {args.base}/api/vobiz/streams/{call_id}/clear -H "X-Internal-Token: $INTERNAL"\n'
    )

    async with websockets.connect(ws_url, max_size=2**20) as ws:
        await ws.send(json.dumps({
            "sequenceNumber": 0, "event": "start",
            "start": {"callId": call_id, "streamId": stream_id, "accountId": "0", "tracks": ["inbound"],
                      "mediaFormat": {"encoding": encoding, "sampleRate": rate}},
            "extra_headers": "{}",
        }))

        frame = (b"\xff" if encoding == "audio/x-mulaw" else b"\x00") * (rate // 50 * width)  # 20 ms silence
        played_bytes = 0

        async def send_media() -> None:
            for chunk in range(1, args.seconds * 50 + 1):
                await ws.send(json.dumps({
                    "sequenceNumber": chunk, "streamId": stream_id, "event": "media",
                    "media": {"track": "inbound", "timestamp": str(chunk * 20), "chunk": chunk,
                              "payload": base64.b64encode(frame).decode()},
                    "extra_headers": "{}",
                }))
                await asyncio.sleep(0.02)

        async def receive() -> None:
            nonlocal played_bytes
            async for raw in ws:
                message = json.loads(raw)
                event = message.get("event")
                if event == "playAudio":
                    media = message["media"]
                    played_bytes += len(base64.b64decode(media["payload"]))
                    print(f"<- playAudio {media['contentType']} {media['sampleRate']} Hz, total {played_bytes} bytes")
                elif event == "checkpoint":
                    print(f"<- checkpoint {message['name']} -> replying playedStream")
                    await ws.send(json.dumps({"event": "playedStream", "name": message["name"]}))
                elif event == "clearAudio":
                    print("<- clearAudio -> replying clearedAudio")
                    await ws.send(json.dumps({"sequenceNumber": 0, "event": "clearedAudio", "streamId": stream_id}))
                elif event == "stop":
                    print("<- stop (server-initiated); closing")
                    await ws.close()
                    return
                else:
                    print(f"<- {event}")

        receiver = asyncio.create_task(receive())
        await send_media()
        receiver.cancel()
    print("socket closed (this is the end-of-stream signal; Vobiz sends no inbound stop)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="local app base URL")
    parser.add_argument("--format", choices=sorted(FORMATS), default="l16-16k",
                        help="must match VOBIZ_INBOUND_CONTENT_TYPE to mimic Vobiz")
    parser.add_argument("--seconds", type=int, default=20)
    parser.add_argument("--call-id")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
