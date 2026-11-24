"""Diagnostic logging (scope A) and test-tone fixes (scope B). Mocked: a simulated client plays Vobiz."""

from __future__ import annotations

import asyncio
import base64
import logging
import os

import pytest
from fastapi.testclient import TestClient

from app.integrations.vobiz.stream_sender import VobizStreamSender
from app.main import create_app
from app.services.audio.formats import ENC_L16, ENC_MULAW, AudioFormat
from app.services.audio.synthetic import tone
from app.services.voice.playback import (
    PLAYED,
    TIMEOUT,
    CheckpointLimitError,
    PlaybackController,
)
from tests.test_vobiz_websocket import (
    AUTH_TOKEN,
    CALL,
    HEADERS,
    INTERNAL,
    STREAM,
    calls_of,
    make_settings,
    media_msg,
    open_stream,
    recv_json,
    wait_until,
)

MULAW_8K = AudioFormat(ENC_MULAW, 8000)
L16_16K = AudioFormat(ENC_L16, 16000)
TONE = f"/api/v1/vobiz/streams/{CALL}/test-tone"


@pytest.fixture
def client():
    with TestClient(create_app(make_settings())) as c:
        yield c


@pytest.fixture
def logs(client: TestClient, caplog: pytest.LogCaptureFixture):
    """Capture app logs. create_app() replaces root handlers, so caplog's handler is re-attached."""
    root = logging.getLogger()
    root.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG)
    yield caplog
    root.removeHandler(caplog.handler)


def records(caplog: pytest.LogCaptureFixture, message: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == message]


def wait_record(caplog: pytest.LogCaptureFixture, message: str, count: int = 1) -> list[logging.LogRecord]:
    return wait_until(lambda: (found := records(caplog, message)) and len(found) >= count and found)


def drain(ws, n: int) -> list[dict]:
    return [recv_json(ws) for _ in range(n)]


# =============================================================================================
# Scope A: observability
# =============================================================================================


def test_first_media_logged_once_per_stream_with_track_and_size(client: TestClient, logs) -> None:
    with open_stream(client) as stream:
        for i in range(1, 6):
            stream.ws.send_json(media_msg(os.urandom(640), chunk=i, seq=i))
        wait_until(lambda: stream.session.stats.frames_received == 5)

    (record,) = records(logs, "vobiz_stream_first_media")  # exactly one, not one per frame
    assert record.call_id == CALL and record.stream_id == STREAM
    assert record.track == "inbound"
    assert record.frame_bytes == 640 and record.frame_ms == 20.0
    assert record.audio_format == "audio/x-l16;rate=16000"
    assert isinstance(record.ms_since_start, int) and record.ms_since_start >= 0


def test_first_media_not_logged_for_rejected_frames(client: TestClient, logs) -> None:
    with open_stream(client) as stream:
        stream.ws.send_json(media_msg("!!bad-base64!!"))
        stream.ws.send_json(media_msg(b"\x00" * 640, stream="other-stream"))
        stream.ws.send_text("garbage")
        stream.ws.send_json(media_msg(b"\x00" * 640))
        wait_until(lambda: stream.session.stats.frames_received == 1)
    assert len(records(logs, "vobiz_stream_first_media")) == 1


def test_each_reconnected_stream_gets_its_own_first_media(client: TestClient, logs) -> None:
    for stream_id in ("s-one", "s-two"):
        with open_stream(client, stream=stream_id) as stream:
            stream.ws.send_json(media_msg(b"\x00" * 640, stream=stream_id))
            wait_until(lambda: stream.session.stats.frames_received == 1)
    assert [r.stream_id for r in records(logs, "vobiz_stream_first_media")] == ["s-one", "s-two"]


def test_stream_ended_reports_close_code_duration_and_counters(client: TestClient, logs) -> None:
    with open_stream(client) as stream:
        for i in range(1, 4):
            stream.ws.send_json(media_msg(b"\x00" * 640, chunk=i, seq=i))
        wait_until(lambda: stream.session.stats.frames_received == 3)

        assert client.post(TONE, json={"duration_ms": 100}, headers=HEADERS).status_code == 200
        checkpoint = [m for m in drain(stream.ws, 6) if m["event"] == "checkpoint"][0]["name"]
        stream.ws.send_json({"event": "playedStream", "name": checkpoint})
        wait_record(logs, "vobiz_checkpoint_played")

        assert client.post(TONE, json={"duration_ms": 100}, headers=HEADERS).status_code == 200
        drain(stream.ws, 6)  # second checkpoint is never acknowledged
        stream.ws.close(4001)

    (ended,) = wait_record(logs, "vobiz_stream_ended")
    assert ended.call_id == CALL and ended.stream_id == STREAM
    assert ended.reason == "socket_closed"
    assert ended.close_code == 4001 and ended.closed_by == "client"
    assert isinstance(ended.duration_s, float) and ended.duration_s >= 0
    assert ended.frames_received == 3 and ended.bytes_received == 1920 and ended.frames_dropped == 0
    assert ended.play_audio_messages == 10  # two 100 ms tones in 20 ms chunks
    assert ended.utterances_sent == 2 and ended.utterances_aborted == 0
    assert ended.checkpoint_outcomes == {"played": 1, "timeout": 0, "cleared": 0, "closed": 1}


def test_stream_ended_when_server_closes_after_hangup(client: TestClient, logs) -> None:
    with open_stream(client) as stream:
        form = {"CallUUID": CALL, "Direction": "outbound", "Event": "Hangup"}
        assert client.post("/api/v1/vobiz/hangup", data=form).status_code == 200
        with pytest.raises(Exception):
            recv_json(stream.ws)
    (ended,) = wait_record(logs, "vobiz_stream_ended")
    assert ended.reason == "hangup" and ended.closed_by == "server" and ended.close_code == 1000


def test_stream_ended_when_server_closes_for_protocol_violation(logs) -> None:
    with TestClient(create_app(make_settings(vobiz_ws_idle_timeout_s=0.2))) as c:
        logging.getLogger().addHandler(logs.handler)
        with open_stream(c) as stream:
            with pytest.raises(Exception):
                recv_json(stream.ws)
        (ended,) = wait_record(logs, "vobiz_stream_ended")
    assert ended.reason == "idle_timeout" and ended.closed_by == "server" and ended.close_code == 1001


def test_playback_sent_logged_with_chunks_bytes_format_and_checkpoint(client: TestClient, logs) -> None:
    with open_stream(client) as stream:
        body = client.post(TONE, json={"duration_ms": 100}, headers=HEADERS).json()
        drain(stream.ws, 6)
    (sent,) = records(logs, "vobiz_playback_sent")
    assert sent.call_id == CALL and sent.stream_id == STREAM
    assert sent.chunks_sent == 5 and sent.bytes_sent == 3200
    assert sent.audio_format == "audio/x-l16;rate=16000" and sent.duration_ms == 100
    assert sent.checkpoint == body["checkpoint"] and sent.aborted is False


def test_checkpoint_played_logged_with_latency(client: TestClient, logs) -> None:
    with open_stream(client) as stream:
        name = client.post(TONE, json={"duration_ms": 100}, headers=HEADERS).json()["checkpoint"]
        drain(stream.ws, 6)
        stream.ws.send_json({"event": "playedStream", "name": name})
        (played,) = wait_record(logs, "vobiz_checkpoint_played")
    assert played.checkpoint == name and played.call_id == CALL and played.stream_id == STREAM
    assert isinstance(played.ack_latency_ms, int) and played.ack_latency_ms >= 0


def test_unknown_played_stream_does_not_log_checkpoint_played(client: TestClient, logs) -> None:
    with open_stream(client) as stream:
        stream.ws.send_json({"event": "playedStream", "name": "never-sent"})
        wait_until(lambda: stream.session.stats.played_stream_unmatched == 1)
    assert records(logs, "vobiz_checkpoint_played") == []


class _Socket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, text: str) -> None:
        self.sent.append(text)


async def test_checkpoint_timeout_logged_from_tracker_timer(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    ctl = PlaybackController(VobizStreamSender("s1", _Socket()), checkpoint_timeout_s=0.05, call_id="c1")
    result = await ctl.play(b"\xff" * 160, MULAW_8K)
    await asyncio.sleep(0.15)
    (record,) = records(caplog, "vobiz_checkpoint_timeout")
    assert record.checkpoint == result.checkpoint and record.call_id == "c1" and record.stream_id == "s1"
    assert record.timeout_s == 0.05 and record.elapsed_ms >= 40
    assert ctl.checkpoint_outcomes[TIMEOUT] == 1


async def test_checkpoint_timeout_logged_for_caller_wait_timeout(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    ctl = PlaybackController(VobizStreamSender("s1", _Socket()), checkpoint_timeout_s=30)
    result = await ctl.play(b"\xff" * 160, MULAW_8K, wait=True, timeout_s=0.05)
    assert result.outcome == TIMEOUT
    (record,) = records(caplog, "vobiz_checkpoint_timeout")
    assert record.checkpoint == result.checkpoint and record.timeout_s == 0.05


async def test_cleared_and_closed_checkpoints_are_counted_but_not_logged_as_timeouts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    ctl = PlaybackController(VobizStreamSender("s1", _Socket()), clear_timeout_s=0.01)
    await ctl.play(b"\xff" * 160, MULAW_8K)
    await ctl.clear(wait_confirmed=False)
    ctl.handle_cleared_audio()
    await ctl.play(b"\xff" * 160, MULAW_8K)
    ctl.close()
    assert ctl.checkpoint_outcomes == {PLAYED: 0, TIMEOUT: 0, "cleared": 1, "closed": 1}
    assert records(caplog, "vobiz_checkpoint_timeout") == []


def test_cleared_audio_received_logged(client: TestClient, logs) -> None:
    from concurrent.futures import ThreadPoolExecutor

    with open_stream(client) as stream:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(lambda: client.post(f"/api/v1/vobiz/streams/{CALL}/clear", headers=HEADERS))
            assert recv_json(stream.ws)["event"] == "clearAudio"
            stream.ws.send_json({"event": "clearedAudio", "sequenceNumber": 0, "streamId": STREAM})
            assert pending.result(timeout=5).json()["cleared_audio_confirmed"] is True
        stream.ws.send_json({"event": "clearedAudio", "streamId": STREAM})  # unsolicited
        wait_record(logs, "vobiz_cleared_audio_received", 2)
    first, second = records(logs, "vobiz_cleared_audio_received")
    assert first.call_id == CALL and first.stream_id == STREAM and first.awaited is True
    assert second.awaited is False


def test_cleared_audio_for_another_stream_is_not_logged(client: TestClient, logs) -> None:
    with open_stream(client) as stream:
        stream.ws.send_json({"event": "clearedAudio", "streamId": "someone-else"})
        wait_until(lambda: stream.session.stats.unexpected_stream == 1)
    assert records(logs, "vobiz_cleared_audio_received") == []


def test_no_secrets_audio_or_numbers_in_any_log(client: TestClient, logs) -> None:
    raw_frame = os.urandom(640)
    frame_b64 = base64.b64encode(raw_frame).decode()
    with open_stream(client) as stream:
        stream.ws.send_json(media_msg(raw_frame))
        body = client.post(TONE, json={"duration_ms": 100}, headers=HEADERS).json()
        played = drain(stream.ws, 6)
        stream.ws.send_json({"event": "playedStream", "name": body["checkpoint"]})
        wait_record(logs, "vobiz_checkpoint_played")
        hangup = {"CallUUID": CALL, "Direction": "outbound", "From": "+919262175535", "To": "+919876543210"}
        client.post("/api/v1/vobiz/hangup", data=hangup)
    wait_record(logs, "vobiz_stream_ended")

    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in logs.records)
    outgoing_payloads = [m["media"]["payload"] for m in played if m["event"] == "playAudio"]
    for forbidden in [AUTH_TOKEN, INTERNAL, frame_b64, "9262175535", "9876543210", *outgoing_payloads]:
        assert forbidden not in text
    assert "token=" not in text


# =============================================================================================
# Scope B: test-tone endpoint
# =============================================================================================


def test_mulaw_tone_on_an_l16_stream(client: TestClient) -> None:
    with open_stream(client) as stream:  # inbound stream is L16/16 kHz
        response = client.post(
            TONE,
            json={"duration_ms": 100, "frequency_hz": 440, "content_type": "audio/x-mulaw", "sample_rate": 8000},
            headers=HEADERS,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["audio_format"] == "audio/x-mulaw;rate=8000"
        assert (body["content_type"], body["sample_rate"]) == ("audio/x-mulaw", 8000)
        assert body["stream_inbound_format"] == "audio/x-l16;rate=16000"
        assert body["byte_order"] == "n/a" and body["chunks_sent"] == 5

        messages = drain(stream.ws, 6)
        plays, checkpoint = messages[:5], messages[5]
        assert all(m["event"] == "playAudio" and m["streamId"] == STREAM for m in plays)
        assert all(m["media"]["contentType"] == "audio/x-mulaw" and m["media"]["sampleRate"] == 8000 for m in plays)
        decoded = [base64.b64decode(m["media"]["payload"], validate=True) for m in plays]
        assert [len(d) for d in decoded] == [160] * 5  # 20 ms of 1-byte samples
        assert b"".join(decoded) == tone(MULAW_8K, 100, 440.0)  # raw G.711, no header
        assert checkpoint == {"event": "checkpoint", "streamId": STREAM, "name": body["checkpoint"]}  # after last chunk


def test_l16_24k_tone_on_a_16k_stream(client: TestClient) -> None:
    with open_stream(client) as stream:
        body = client.post(
            TONE, json={"duration_ms": 100, "content_type": "audio/x-l16", "sample_rate": 24000, "byte_order": "big"},
            headers=HEADERS,
        ).json()
        plays = drain(stream.ws, 6)[:5]
    assert body["audio_format"] == "audio/x-l16;rate=24000" and body["byte_order"] == "big"
    assert all(m["media"]["sampleRate"] == 24000 for m in plays)
    decoded = b"".join(base64.b64decode(m["media"]["payload"]) for m in plays)
    assert decoded == tone(AudioFormat(ENC_L16, 24000), 100, 440.0, byte_order="big")


def test_omitting_format_uses_the_stream_format(client: TestClient) -> None:
    with open_stream(client, encoding="audio/x-mulaw", rate=8000) as stream:
        body = client.post(TONE, json={"duration_ms": 100}, headers=HEADERS).json()
        drain(stream.ws, 6)
    assert body["audio_format"] == "audio/x-mulaw;rate=8000" and body["byte_order"] == "n/a"


@pytest.mark.parametrize(
    "fields",
    [
        {"content_type": "audio/x-mulaw", "sample_rate": 16000},
        {"content_type": "audio/x-mulaw", "sample_rate": 24000},
        {"content_type": "audio/x-l16", "sample_rate": 44100},
        {"content_type": "audio/x-l16", "sample_rate": 12000},
        {"content_type": "audio/x-l16", "sample_rate": 0},
        {"content_type": "audio/wav", "sample_rate": 8000},
        {"content_type": "audio/x-alaw", "sample_rate": 8000},
        {"content_type": "audio/x-mulaw"},
        {"sample_rate": 8000},
        {"content_type": "audio/x-l16", "sample_rate": "fast"},
    ],
)
def test_invalid_format_or_rate_rejected_before_sending(client: TestClient, fields: dict) -> None:
    with open_stream(client) as stream:
        response = client.post(TONE, json={"duration_ms": 100, **fields}, headers=HEADERS)
        assert response.status_code == 422
        assert stream.session.playback.play_audio_messages == 0
        assert stream.session.playback.pending_checkpoints == frozenset()


def test_checkpoint_limit_returns_429_and_sends_nothing(client: TestClient) -> None:
    with open_stream(client) as stream:
        stream.session.playback._tracker._max_pending = 2  # test hook: a small limit
        for _ in range(2):
            assert client.post(TONE, json={"duration_ms": 100}, headers=HEADERS).status_code == 200
        drain(stream.ws, 12)
        sent_before = stream.session.playback.play_audio_messages

        response = client.post(TONE, json={"duration_ms": 100}, headers=HEADERS)

        assert response.status_code == 429
        assert "2 checkpoints" in response.json()["detail"]
        assert stream.session.playback.play_audio_messages == sent_before  # no untracked audio queued
        assert len(stream.session.playback.pending_checkpoints) == 2

        # acknowledging one frees a slot, and playback works again
        stream.ws.send_json({"event": "playedStream", "name": sorted(stream.session.playback.pending_checkpoints)[0]})
        wait_until(lambda: len(stream.session.playback.pending_checkpoints) == 1)
        assert client.post(TONE, json={"duration_ms": 100}, headers=HEADERS).status_code == 200


async def test_controller_checks_capacity_before_sending_audio() -> None:
    sock = _Socket()
    ctl = PlaybackController(VobizStreamSender("s1", sock), max_pending_checkpoints=1)
    await ctl.play(b"\xff" * 160, MULAW_8K)
    sent = len(sock.sent)
    with pytest.raises(CheckpointLimitError):
        await ctl.play(b"\xff" * 160, MULAW_8K)
    assert len(sock.sent) == sent
    ctl.close()


def test_socket_failure_during_send_returns_409_not_500(client: TestClient) -> None:
    with open_stream(client) as stream:
        async def broken(_: str) -> None:
            raise ConnectionResetError("gone")

        stream.session.sender._send_text = broken  # the socket dies mid-request
        response = client.post(TONE, json={"duration_ms": 100}, headers=HEADERS)
        assert response.status_code == 409
        assert "closed" in response.json()["detail"].lower()
        assert stream.session.sender.closed

        again = client.post(TONE, json={"duration_ms": 100}, headers=HEADERS)
        assert again.status_code == 409


def test_closed_session_returns_409_and_ended_stream_returns_404(client: TestClient) -> None:
    with open_stream(client) as stream:
        stream.session.playback.close()  # closed but not yet detached (cleanup race)
        assert client.post(TONE, json={"duration_ms": 100}, headers=HEADERS).status_code == 409
    wait_until(lambda: calls_of(client).current_stream(CALL) is None)
    assert client.post(TONE, json={"duration_ms": 100}, headers=HEADERS).status_code == 404


def test_wait_for_playback_reports_played(client: TestClient) -> None:
    from concurrent.futures import ThreadPoolExecutor

    with open_stream(client) as stream:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                lambda: client.post(
                    TONE,
                    json={"duration_ms": 100, "content_type": "audio/x-mulaw", "sample_rate": 8000,
                          "wait_for_playback": True},
                    headers=HEADERS,
                )
            )
            checkpoint = drain(stream.ws, 6)[5]
            stream.ws.send_json({"event": "playedStream", "name": checkpoint["name"]})
            body = pending.result(timeout=5).json()
    assert body["outcome"] == "played" and body["checkpoint"] == checkpoint["name"]
