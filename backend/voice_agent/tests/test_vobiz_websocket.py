"""Milestone 3: the /ws/vobiz/stream/{call_uuid} endpoint, driven by Starlette's in-process
WebSocket test client.

These are MOCKED tests: a simulated client plays Vobiz's part. They prove our server's behaviour
against the documented protocol, not that real Vobiz behaves the same way.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from app.core.config import Settings
from app.core.logging import RedactTokenFilter
from app.integrations.vobiz.stream_token import issue_stream_token, token_to_header_safe
from app.main import create_app
from app.services.audio.synthetic import tone
from app.services.audio.formats import AudioFormat

AUTH_TOKEN = "vobiz-auth-token-for-tests"
INTERNAL = "internal-token"
CALL = "5401fd2e-6344-40df-a22c-c8ffea7a92e7"
STREAM = "c4dfd815-a92a-4140-ab85-5ff28c004116"
WS = "/ws/vobiz/stream"
HEADERS = {"X-Internal-Token": INTERNAL}


def make_settings(**overrides) -> Settings:
    values = dict(
        vobiz_auth_id="MA_TEST",
        vobiz_auth_token=SecretStr(AUTH_TOKEN),
        internal_api_token=SecretStr(INTERNAL),
        public_base_url="https://abc.ngrok.app",
        vobiz_validate_signature=False,
    )
    values.update(overrides)
    return Settings(**values)


def token_for(call: str = CALL, **kw) -> str:
    return issue_stream_token(AUTH_TOKEN, call, **kw)


def url(call: str = CALL, token: str | None = None) -> str:
    return f"{WS}/{call}?token={token if token is not None else token_for(call)}"


def start_msg(call: str = CALL, stream: str = STREAM, encoding: str = "audio/x-l16", rate: int = 16000, **extra) -> dict:
    return {
        "sequenceNumber": 0,
        "event": "start",
        "start": {
            "callId": call, "streamId": stream, "accountId": "500025", "tracks": ["inbound"],
            "mediaFormat": {"encoding": encoding, "sampleRate": rate},
        },
        "extra_headers": extra.get("extra_headers", "{}"),
    }


def media_msg(payload: bytes | str, *, stream: str = STREAM, chunk: int = 1, seq: int = 1) -> dict:
    if isinstance(payload, bytes):
        payload = base64.b64encode(payload).decode()
    return {
        "sequenceNumber": seq, "streamId": stream, "event": "media",
        "media": {"track": "inbound", "timestamp": str(1778597597091 + chunk * 20), "chunk": chunk, "payload": payload},
        "extra_headers": "{}",
    }


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not reached in time")


def recv_json(ws, timeout: float = 5.0) -> dict:
    """receive_json() with a deadline, so a bug fails the test instead of hanging it."""
    box: dict = {}

    def run() -> None:
        try:
            box["value"] = ws.receive_json()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise AssertionError("no message received from the server")
    if "error" in box:
        raise box["error"]
    return box["value"]


@pytest.fixture
def client():
    with TestClient(create_app(make_settings())) as c:
        yield c


def calls_of(client: TestClient):
    return client.app.state.call_manager


def open_stream(client: TestClient, call: str = CALL, stream: str = STREAM, **start_kw):
    """Context manager: connect, send `start`, wait until the server registered the stream."""
    ws_cm = client.websocket_connect(url(call))

    class _Ctx:
        def __enter__(self_inner):
            self_inner.ws = ws_cm.__enter__()
            self_inner.ws.send_json(start_msg(call, stream, **start_kw))
            self_inner.session = wait_until(lambda: calls_of(client).current_stream(call))
            return self_inner

        def __exit__(self_inner, *exc):
            return ws_cm.__exit__(*exc)

    return _Ctx()


# --- authentication --------------------------------------------------------------------------


def test_valid_token_is_accepted_and_stream_registered(client: TestClient) -> None:
    with open_stream(client) as stream:
        session = stream.session
        assert (session.call_id, session.stream_id) == (CALL, STREAM)
        assert session.audio_format.label == "audio/x-l16;rate=16000"
        assert session.account_id == "500025" and session.tracks == ("inbound",)
        assert len(calls_of(client)) == 1


@pytest.mark.parametrize("bad_token", ["", "garbage", "1.2.3", "9999999999.AAAA.AAAA", "not.a.token.at.all"])
def test_invalid_token_is_rejected_before_accept(client: TestClient, bad_token: str) -> None:
    with pytest.raises(WebSocketDisconnect) as info:
        with client.websocket_connect(url(token=bad_token)):
            pass
    assert info.value.code == 1008
    assert calls_of(client).current_stream(CALL) is None


def test_missing_token_is_rejected(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as info:
        with client.websocket_connect(f"{WS}/{CALL}"):
            pass
    assert info.value.code == 1008


def test_expired_token_is_rejected(client: TestClient) -> None:
    expired = token_for(now=time.time() - 10_000, ttl_s=900)
    with pytest.raises(WebSocketDisconnect) as info:
        with client.websocket_connect(url(token=expired)):
            pass
    assert info.value.code == 1008


def test_token_for_a_different_call_is_rejected(client: TestClient) -> None:
    other = token_for("some-other-call")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(url(CALL, token=other)):
            pass
    assert calls_of(client).current_stream(CALL) is None


def test_token_signed_with_another_key_is_rejected(client: TestClient) -> None:
    forged = issue_stream_token("some-other-secret", CALL)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(url(token=forged)):
            pass


def test_rejects_when_auth_token_not_configured() -> None:
    with TestClient(create_app(make_settings(vobiz_auth_token=SecretStr("")))) as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(url(token="x.y.z")):
                pass


def test_token_never_appears_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    with TestClient(create_app(make_settings())) as c:
        logging.getLogger().addHandler(caplog.handler)  # create_app() replaced the root handlers
        caplog.set_level(logging.DEBUG)
        token = token_for()
        with c.websocket_connect(url(token=token)) as ws:
            ws.send_json(start_msg())
            wait_until(lambda: c.app.state.call_manager.current_stream(CALL))
            ws.send_text("not json")
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(url(token="1.bad.token")):
                pass
        wait_until(lambda: any(r.getMessage() == "vobiz_stream_ended" for r in caplog.records))
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert token not in text and "token=" not in text and AUTH_TOKEN not in text


def test_uvicorn_style_log_lines_have_the_token_redacted() -> None:
    record = logging.LogRecord(
        "uvicorn.error", logging.INFO, __file__, 1,
        '%s - "WebSocket %s" [accepted]', ("1.2.3.4:5", f"{WS}/{CALL}?token=1778.abc_DEF-9.sig&x=1"), None,
    )
    assert RedactTokenFilter().filter(record) is True
    line = record.getMessage()
    assert "1778.abc" not in line and "token=[redacted]" in line and "x=1" in line


def test_start_for_a_different_call_than_the_url_is_rejected(client: TestClient) -> None:
    with client.websocket_connect(url(CALL)) as ws:
        ws.send_json(start_msg(call="a-different-call"))
        with pytest.raises(WebSocketDisconnect) as info:
            recv_json(ws)
        assert info.value.code == 1008
    assert calls_of(client).current_stream(CALL) is None
    assert calls_of(client).current_stream("a-different-call") is None


# --- start / media ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("encoding", "rate", "frame_bytes"),
    [("audio/x-l16", 8000, 320), ("audio/x-l16", 16000, 640), ("audio/x-mulaw", 8000, 160)],
)
def test_media_frames_are_decoded_and_queued_untouched(client: TestClient, encoding: str, rate: int, frame_bytes: int) -> None:
    frames = [os.urandom(frame_bytes) for _ in range(5)]
    with open_stream(client, encoding=encoding, rate=rate) as stream:
        for i, raw in enumerate(frames, start=1):
            stream.ws.send_json(media_msg(raw, chunk=i, seq=i))
        session = stream.session
        wait_until(lambda: session.stats.frames_received == 5)

        assert session.audio_format.encoding == encoding and session.audio_format.sample_rate == rate
        assert session.stats.bytes_received == 5 * frame_bytes
        stored = list(session.queue._items)
        assert [f.data for f in stored] == frames  # raw bytes preserved, not converted
        assert all(f.format == session.audio_format and f.track == "inbound" for f in stored)
        assert [f.chunk for f in stored] == [1, 2, 3, 4, 5]
        assert stored[0].timestamp_ms == 1778597597091 + 20 and stored[0].stream_id == STREAM


def test_media_before_start_is_ignored(client: TestClient) -> None:
    with client.websocket_connect(url()) as ws:
        ws.send_json(media_msg(b"\x00" * 320))
        ws.send_json(start_msg())
        session = wait_until(lambda: calls_of(client).current_stream(CALL))
        assert session.stats.frames_received == 0


def test_invalid_json_does_not_kill_the_connection(client: TestClient) -> None:
    with open_stream(client) as stream:
        for junk in ("not json", "[1,2,3]", "{}", '{"event": 7}', "\x00\x01", "{" * 50):
            stream.ws.send_text(junk)
        stream.ws.send_json(media_msg(b"\x00\x01" * 320))
        wait_until(lambda: stream.session.stats.frames_received == 1)
        assert stream.session.stats.malformed == 6 and not stream.session.closed


def test_invalid_base64_is_counted_and_skipped(client: TestClient) -> None:
    with open_stream(client) as stream:
        stream.ws.send_json(media_msg("!!!not-base64!!!"))
        stream.ws.send_json(media_msg(""))
        stream.ws.send_json(media_msg(b"\x01\x02\x03"))  # odd length for 16-bit audio
        stream.ws.send_json(media_msg(b"\x00" * 640))
        wait_until(lambda: stream.session.stats.frames_received == 1)
        assert stream.session.stats.malformed == 3 and len(stream.session.queue) == 1


def test_unsupported_encoding_in_start_closes_with_1003(client: TestClient) -> None:
    with client.websocket_connect(url()) as ws:
        ws.send_json(start_msg(encoding="audio/x-alaw", rate=8000))
        with pytest.raises(WebSocketDisconnect) as info:
            recv_json(ws)
        assert info.value.code == 1003
    assert calls_of(client).current_stream(CALL) is None


def test_unexpected_stream_id_is_not_processed(client: TestClient) -> None:
    with open_stream(client) as stream:
        stream.ws.send_json(media_msg(b"\x00" * 640, stream="a-different-stream"))
        stream.ws.send_json(media_msg(b"\x00" * 640))
        wait_until(lambda: stream.session.stats.frames_received == 1)
        assert stream.session.stats.unexpected_stream == 1
        assert [f.stream_id for f in stream.session.queue._items] == [STREAM]


def test_second_start_on_the_same_socket_is_ignored(client: TestClient) -> None:
    with open_stream(client) as stream:
        stream.ws.send_json(start_msg(stream="another-stream"))
        stream.ws.send_json(media_msg(b"\x00" * 640))
        wait_until(lambda: stream.session.stats.frames_received == 1)
        assert stream.session.stream_id == STREAM and len(calls_of(client)) == 1
        assert calls_of(client).get(CALL).streams_seen == 1


def test_binary_frames_are_ignored(client: TestClient) -> None:
    with open_stream(client) as stream:
        stream.ws.send_bytes(b"\x00\x01\x02")
        stream.ws.send_json(media_msg(b"\x00" * 640))
        wait_until(lambda: stream.session.stats.frames_received == 1)
        assert stream.session.stats.malformed == 1


def test_too_many_malformed_messages_closes_the_connection(client: TestClient) -> None:
    with open_stream(client) as stream:
        for _ in range(40):
            stream.ws.send_text("garbage")
        wait_until(lambda: stream.session.closed)
        assert stream.session.end_reason == "too_many_malformed_messages"


def test_oversized_message_closes_the_connection() -> None:
    with TestClient(create_app(make_settings(vobiz_ws_max_message_bytes=1000))) as c:
        with c.websocket_connect(url()) as ws:
            ws.send_text("x" * 2000)
            with pytest.raises(WebSocketDisconnect) as info:
                recv_json(ws)
            assert info.value.code == 1009


def test_dtmf_is_counted_but_digit_not_logged(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    logging.getLogger().addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG)
    with open_stream(client) as stream:
        stream.ws.send_json({"event": "dtmf", "sequenceNumber": 5, "streamId": STREAM,
                             "dtmf": {"track": "inbound", "digit": "7", "timestamp": "1763495178506"}})
        wait_until(lambda: stream.session.stats.dtmf_events == 1)
    assert not any("'digit'" in str(r.__dict__) or '"digit"' in r.getMessage() for r in caplog.records)


def test_unknown_and_incorrect_payload_events_are_tolerated(client: TestClient) -> None:
    with open_stream(client) as stream:
        stream.ws.send_json({"event": "somethingNew", "x": 1})
        stream.ws.send_json({"event": "incorrectPayload", "streamId": STREAM, "payload": "{...}"})
        stream.ws.send_json(media_msg(b"\x00" * 640))
        wait_until(lambda: stream.session.stats.frames_received == 1)
        assert not stream.session.closed


def test_start_timeout_closes_idle_connections() -> None:
    with TestClient(create_app(make_settings(vobiz_ws_start_timeout_s=0.2))) as c:
        with c.websocket_connect(url()) as ws:
            with pytest.raises(WebSocketDisconnect) as info:
                recv_json(ws)
            assert info.value.code == 1008


def test_idle_timeout_closes_silent_streams() -> None:
    with TestClient(create_app(make_settings(vobiz_ws_idle_timeout_s=0.2))) as c:
        with c.websocket_connect(url()) as ws:
            ws.send_json(start_msg())
            session = wait_until(lambda: c.app.state.call_manager.current_stream(CALL))
            with pytest.raises(WebSocketDisconnect) as info:
                recv_json(ws)
            assert info.value.code == 1001
            wait_until(lambda: session.closed)
            assert session.end_reason == "idle_timeout"


def test_connection_limit() -> None:
    with TestClient(create_app(make_settings(vobiz_ws_max_connections=1))) as c:
        with c.websocket_connect(url()) as ws:
            ws.send_json(start_msg())
            wait_until(lambda: c.app.state.call_manager.current_stream(CALL))
            with pytest.raises(WebSocketDisconnect) as info:
                with c.websocket_connect(url("second-call")):
                    pass
            assert info.value.code == 1013
        wait_until(lambda: c.app.state.stream_handler.connection_count == 0)
        with c.websocket_connect(url("second-call")) as ws2:  # the slot is free again
            ws2.send_json(start_msg(call="second-call"))
            wait_until(lambda: c.app.state.call_manager.current_stream("second-call"))


# --- queue overflow --------------------------------------------------------------------------


def test_queue_overflow_drops_oldest_and_cleans_up_on_close() -> None:
    with TestClient(create_app(make_settings(vobiz_ws_queue_frames=3))) as c:
        with c.websocket_connect(url()) as ws:
            ws.send_json(start_msg())
            session = wait_until(lambda: c.app.state.call_manager.current_stream(CALL))
            for i in range(1, 11):
                ws.send_json(media_msg(bytes([i]) * 640, chunk=i, seq=i))
            wait_until(lambda: session.stats.frames_received == 10)

            assert len(session.queue) == 3 and session.queue.dropped == 7  # memory stays bounded
            assert [f.chunk for f in session.queue._items] == [8, 9, 10]  # newest kept
        wait_until(lambda: session.closed)
        assert session.queue.closed and len(session.queue) == 0  # buffers released
        assert c.app.state.call_manager.current_stream(CALL) is None


# --- disconnect and reconnect ----------------------------------------------------------------


def test_disconnect_releases_stream_resources(client: TestClient) -> None:
    with open_stream(client) as stream:
        session = stream.session
        stream.ws.send_json(media_msg(b"\x00" * 640))
        wait_until(lambda: session.stats.frames_received == 1)
    wait_until(lambda: session.closed)
    assert session.end_reason == "socket_closed"
    assert session.queue.closed and session.playback.pending_checkpoints == frozenset()
    assert session.sender.closed
    assert calls_of(client).current_stream(CALL) is None
    wait_until(lambda: client.app.state.stream_handler.connection_count == 0)


def test_abrupt_disconnect_is_handled(client: TestClient) -> None:
    ws_cm = client.websocket_connect(url())
    ws = ws_cm.__enter__()
    ws.send_json(start_msg())
    session = wait_until(lambda: calls_of(client).current_stream(CALL))
    ws_cm.__exit__(None, None, None)
    wait_until(lambda: session.closed)
    assert client.get("/health").json() == {"status": "ok"}  # server still healthy


def test_reconnect_with_new_stream_id_replaces_old_stream(client: TestClient) -> None:
    first = client.websocket_connect(url())
    ws1 = first.__enter__()
    try:
        ws1.send_json(start_msg(stream="stream-one"))
        s1 = wait_until(lambda: calls_of(client).current_stream(CALL))
        assert s1.stream_id == "stream-one"

        with client.websocket_connect(url()) as ws2:  # Vobiz reconnects: new socket, new streamId, same callId
            ws2.send_json(start_msg(stream="stream-two"))
            s2 = wait_until(
                lambda: s if (s := calls_of(client).current_stream(CALL)) is not None and s is not s1 else None
            )
            assert s2.stream_id == "stream-two"
            assert len(calls_of(client)) == 1  # one call state, not two
            state = calls_of(client).get(CALL)
            assert state.streams_seen == 2 and state.reconnects == 1
            assert s1.closed and s1.end_reason == "superseded"
            assert s1.queue.closed and s1.sender.closed

            # media for the old stream id is no longer accepted; the new stream works
            ws2.send_json(media_msg(b"\x00" * 640, stream="stream-one"))
            ws2.send_json(media_msg(b"\x00" * 640, stream="stream-two"))
            wait_until(lambda: s2.stats.frames_received == 1)
            assert s2.stats.unexpected_stream == 1

            # the old socket was told to close; its late cleanup must not disturb the new stream
            with pytest.raises(WebSocketDisconnect):
                recv_json(ws1)
            first.__exit__(None, None, None)
            first = None
            wait_until(lambda: client.app.state.stream_handler.connection_count == 1)
            assert calls_of(client).current_stream(CALL) is s2 and not s2.closed
    finally:
        if first is not None:
            first.__exit__(None, None, None)


# --- outgoing audio through the real endpoint ------------------------------------------------


def test_play_audio_checkpoint_and_played_stream_round_trip(client: TestClient) -> None:
    with open_stream(client) as stream:
        response = client.post(
            f"/api/v1/vobiz/streams/{CALL}/test-tone",
            json={"duration_ms": 100, "frequency_hz": 440, "byte_order": "little"},
            headers=HEADERS,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["audio_format"] == "audio/x-l16;rate=16000" and body["chunks_sent"] == 5  # 100 ms / 20 ms
        assert body["outcome"] == "not_waited" and body["aborted"] is False

        messages = [recv_json(stream.ws) for _ in range(6)]
        plays, checkpoint = messages[:5], messages[5]
        assert [m["event"] for m in plays] == ["playAudio"] * 5 and checkpoint["event"] == "checkpoint"
        assert all(m["streamId"] == STREAM for m in messages)
        assert all(m["media"]["contentType"] == "audio/x-l16" and m["media"]["sampleRate"] == 16000 for m in plays)
        decoded = [base64.b64decode(m["media"]["payload"], validate=True) for m in plays]
        assert [len(d) for d in decoded] == [640] * 5
        expected = tone(AudioFormat("audio/x-l16", 16000), 100, 440.0, byte_order="little")
        assert b"".join(decoded) == expected  # correct base64, raw samples, no container header
        assert not b"".join(decoded).startswith(b"RIFF")

        name = checkpoint["name"]
        assert body["checkpoint"] == name
        assert stream.session.playback.pending_checkpoints == {name}

        stream.ws.send_json({"event": "playedStream", "name": name})  # no streamId, as documented
        wait_until(lambda: stream.session.playback.pending_checkpoints == frozenset())
        assert stream.session.stats.played_stream_unmatched == 0


def test_played_stream_for_another_calls_checkpoint_is_ignored(client: TestClient) -> None:
    with open_stream(client, "call-a", "stream-a") as a, open_stream(client, "call-b", "stream-b") as b:
        for call in ("call-a", "call-b"):
            assert client.post(f"/api/v1/vobiz/streams/{call}/test-tone", json={"duration_ms": 100}, headers=HEADERS).status_code == 200
        for _ in range(6):
            recv_json(a.ws)
        name_a = [m for m in [recv_json(b.ws) for _ in range(6)] if m["event"] == "checkpoint"][0]["name"]
        name_b = next(iter(b.session.playback.pending_checkpoints))
        assert name_a == name_b  # (the checkpoint read above came from b)

        a_name = next(iter(a.session.playback.pending_checkpoints))
        b.ws.send_json({"event": "playedStream", "name": a_name})  # a's name arrives on b's socket
        wait_until(lambda: b.session.stats.played_stream_unmatched == 1)
        assert a.session.playback.pending_checkpoints == {a_name}  # a's checkpoint untouched
        assert b.session.playback.pending_checkpoints == {name_b}


def test_mulaw_stream_gets_mulaw_playback(client: TestClient) -> None:
    with open_stream(client, encoding="audio/x-mulaw", rate=8000) as stream:
        assert client.post(f"/api/v1/vobiz/streams/{CALL}/test-tone", json={"duration_ms": 100}, headers=HEADERS).status_code == 200
        plays = [recv_json(stream.ws) for _ in range(5)]
        assert all(m["media"]["contentType"] == "audio/x-mulaw" and m["media"]["sampleRate"] == 8000 for m in plays)
        assert all(len(base64.b64decode(m["media"]["payload"])) == 160 for m in plays)


def test_clear_audio_round_trip_with_cleared_audio_ack(client: TestClient) -> None:
    with open_stream(client) as stream:
        client.post(f"/api/v1/vobiz/streams/{CALL}/test-tone", json={"duration_ms": 100}, headers=HEADERS)
        for _ in range(6):
            recv_json(stream.ws)
        assert len(stream.session.playback.pending_checkpoints) == 1

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(lambda: client.post(f"/api/v1/vobiz/streams/{CALL}/clear", headers=HEADERS))
            assert recv_json(stream.ws) == {"event": "clearAudio", "streamId": STREAM}
            assert stream.session.playback.state.value == "clearing"
            assert stream.session.playback.pending_checkpoints == frozenset()  # voided immediately
            stream.ws.send_json({"event": "clearedAudio", "sequenceNumber": 0, "streamId": STREAM})
            response = pending.result(timeout=5)

        assert response.status_code == 200
        assert response.json()["cleared_audio_confirmed"] is True
        assert response.json()["playback_state"] == "idle"


def test_cleared_audio_for_another_stream_does_not_confirm(client: TestClient) -> None:
    with open_stream(client) as stream:
        wait_until(lambda: stream.session.playback.state.value == "idle")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(lambda: client.post(f"/api/v1/vobiz/streams/{CALL}/clear", headers=HEADERS))
            recv_json(stream.ws)
            stream.ws.send_json({"event": "clearedAudio", "streamId": "someone-else"})
            wait_until(lambda: stream.session.stats.unexpected_stream == 1)
            assert stream.session.playback.state.value == "clearing"
            stream.ws.send_json({"event": "clearedAudio", "streamId": STREAM})
            assert pending.result(timeout=5).json()["cleared_audio_confirmed"] is True


def test_nothing_is_sent_after_the_stream_closes(client: TestClient) -> None:
    with open_stream(client) as stream:
        session = stream.session
    wait_until(lambda: session.closed)
    response = client.post(f"/api/v1/vobiz/streams/{CALL}/test-tone", json={}, headers=HEADERS)
    assert response.status_code == 404  # no active stream
    assert calls_of(client).current_stream(CALL) is None


def test_control_routes_require_internal_token_and_a_stream(client: TestClient) -> None:
    for path in (f"/api/v1/vobiz/streams/{CALL}/test-tone", f"/api/v1/vobiz/streams/{CALL}/clear"):
        assert client.post(path, json={}).status_code == 401
        assert client.post(path, json={}, headers={"X-Internal-Token": "wrong"}).status_code == 401
        assert client.post(path, json={}, headers=HEADERS).status_code == 404
    assert client.get("/api/v1/vobiz/streams").status_code == 401


def test_test_tone_validates_input(client: TestClient) -> None:
    with open_stream(client):
        for body in ({"duration_ms": 10}, {"duration_ms": 999999}, {"byte_order": "middle"}, {"frequency_hz": 5}, {"x": 1}):
            response = client.post(f"/api/v1/vobiz/streams/{CALL}/test-tone", json=body, headers=HEADERS)
            assert response.status_code == 422


def test_stream_listing_contains_counters_but_no_audio(client: TestClient) -> None:
    with open_stream(client) as stream:
        stream.ws.send_json(media_msg(b"\x00" * 640))
        wait_until(lambda: stream.session.stats.frames_received == 1)
        body = client.get("/api/v1/vobiz/streams", headers=HEADERS).json()
        assert body["connections"] == 1
        current = body["calls"][0]["current_stream"]
        assert current["frames_received"] == 1 and current["audio_format"] == "audio/x-l16;rate=16000"
        assert "payload" not in json.dumps(body) and AUTH_TOKEN not in json.dumps(body)


# --- hangup webhook vs socket closure --------------------------------------------------------


HANGUP_FORM = {"CallUUID": CALL, "Direction": "outbound", "Event": "Hangup", "CallStatus": "completed"}


def test_hangup_before_socket_closure_cleans_up_and_closes_socket(client: TestClient) -> None:
    with open_stream(client) as stream:
        session = stream.session
        stream.ws.send_json(media_msg(b"\x00" * 640))
        wait_until(lambda: session.stats.frames_received == 1)

        response = client.post("/api/v1/vobiz/hangup", data=HANGUP_FORM)  # webhook wins the race
        assert response.status_code == 200 and response.json()["status"] == "received"

        assert session.closed and session.end_reason == "hangup"
        assert session.queue.closed and session.sender.closed
        assert calls_of(client).current_stream(CALL) is None and calls_of(client).get(CALL) is None
        with pytest.raises(WebSocketDisconnect) as info:  # the server closes our socket
            recv_json(stream.ws)
        assert info.value.code == 1000
    # the socket's own late cleanup must not raise or resurrect state
    wait_until(lambda: client.app.state.stream_handler.connection_count == 0)
    assert session.end_reason == "hangup" and len(calls_of(client)) == 0


def test_hangup_with_no_stream_is_fine_and_blocks_late_connections(client: TestClient) -> None:
    assert client.post("/api/v1/vobiz/hangup", data=HANGUP_FORM).status_code == 200
    assert client.post("/api/v1/vobiz/hangup", data=HANGUP_FORM).json()["duplicate"] is True  # retried callback

    with client.websocket_connect(url()) as ws:  # a stream that connects after the call ended
        ws.send_json(start_msg())
        with pytest.raises(WebSocketDisconnect) as info:
            recv_json(ws)
        assert info.value.code == 1000
    assert calls_of(client).current_stream(CALL) is None and len(calls_of(client)) == 0


def test_socket_closure_before_hangup_then_hangup_is_safe(client: TestClient) -> None:
    with open_stream(client) as stream:
        session = stream.session
    wait_until(lambda: session.closed)
    assert session.end_reason == "socket_closed"
    assert client.post("/api/v1/vobiz/hangup", data=HANGUP_FORM).status_code == 200
    assert session.end_reason == "socket_closed" and len(calls_of(client)) == 0
    assert calls_of(client).is_ended(CALL)


# --- configurable authentication: token via extraHeaders on the start event -------------------


def test_answer_webhook_in_start_event_mode_puts_token_in_extra_headers_not_url() -> None:
    settings = make_settings(vobiz_ws_auth_mode="start_event")
    with TestClient(create_app(settings)) as c:
        form = {"CallUUID": CALL, "From": "+14155551234", "To": "+8801711111234", "Direction": "outbound"}
        xml = c.post("/api/v1/vobiz/answer", data=form).text
        stream = ET.fromstring(xml).find("Stream")
        assert stream.text == f"wss://abc.ngrok.app/ws/vobiz/stream/{CALL}"  # no query string
        assert "token" not in urlsplit(stream.text).query
        key, value = stream.attrib["extraHeaders"].split("=", 1)
        assert key == "token" and value.isalnum()  # Vobiz allows only letters and digits here
        assert len(stream.attrib["extraHeaders"]) <= 512
        assert stream.attrib["bidirectional"] == "true" and stream.attrib["audioTrack"] == "inbound"


def test_query_mode_answer_still_uses_query_token() -> None:
    with TestClient(create_app(make_settings())) as c:
        form = {"CallUUID": CALL, "From": "+14155551234", "To": "+8801711111234", "Direction": "outbound"}
        stream = ET.fromstring(c.post("/api/v1/vobiz/answer", data=form).text).find("Stream")
        assert "token" in parse_qs(urlsplit(stream.text).query)
        assert "extraHeaders" not in stream.attrib


def start_event_client() -> TestClient:
    return TestClient(create_app(make_settings(vobiz_ws_auth_mode="start_event")))


def vh_headers(token: str) -> str:
    return "{X-VH-token: %s}" % token_to_header_safe(token)  # the documented extra_headers string format


def test_start_event_mode_accepts_valid_token() -> None:
    with start_event_client() as c:
        with c.websocket_connect(f"{WS}/{CALL}") as ws:  # no query token needed in this mode
            ws.send_json(start_msg(extra_headers=vh_headers(token_for())))
            session = wait_until(lambda: c.app.state.call_manager.current_stream(CALL))
            assert session.stream_id == STREAM


@pytest.mark.parametrize("which", ["missing", "empty", "garbage", "expired", "other_call"])
def test_start_event_mode_rejects_bad_tokens(which: str) -> None:
    extra = {
        "missing": "{}",
        "empty": "{X-VH-token: }",
        "garbage": "{X-VH-token: abcdef}",
        "expired": vh_headers(token_for(now=time.time() - 10_000)),
        "other_call": vh_headers(token_for("another-call")),
    }[which]
    with start_event_client() as c:
        with c.websocket_connect(f"{WS}/{CALL}") as ws:
            ws.send_json(start_msg(extra_headers=extra))
            with pytest.raises(WebSocketDisconnect) as info:
                recv_json(ws)
            assert info.value.code == 1008
        assert c.app.state.call_manager.current_stream(CALL) is None


def test_start_event_mode_ignores_query_token_and_processes_nothing_before_start() -> None:
    with start_event_client() as c:
        with c.websocket_connect(url()) as ws:  # a valid query token is not what this mode checks
            ws.send_json(media_msg(b"\x00" * 640))
            ws.send_json(start_msg(extra_headers="{}"))
            with pytest.raises(WebSocketDisconnect):
                recv_json(ws)
        assert c.app.state.call_manager.current_stream(CALL) is None


def test_invalid_auth_mode_fails_at_startup() -> None:
    with pytest.raises(ValueError):
        create_app(make_settings(vobiz_ws_auth_mode="header"))
    with pytest.raises(ValueError):
        create_app(make_settings(vobiz_playback_chunk_ms=10))


def test_existing_routes_still_registered(client: TestClient) -> None:
    paths = set(client.get("/openapi.json").json()["paths"])
    assert {"/health", "/api/v1/vobiz/calls",
            "/api/v1/vobiz/answer", "/api/v1/vobiz/hangup", "/api/v1/vobiz/diagnostics",
            "/api/v1/sarvam/diagnostics", "/api/v1/vobiz/streams"} <= paths
