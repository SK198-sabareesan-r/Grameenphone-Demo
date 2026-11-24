"""Sarvam Realtime STT client (M4.1). MOCKED: a scripted fake WebSocket plays Sarvam's part.

These tests check our client against the documented protocol. They do not prove that the live
Sarvam service behaves the same way; see scripts/validate_sarvam_realtime_stt.py for that.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from app.core.config import Settings
from app.main import create_app
from app.services.sarvam.errors import (
    SarvamAuthError,
    SarvamConfigError,
    SarvamConnectionError,
    SarvamRequestError,
    SarvamServerError,
    SarvamStreamClosedError,
    SarvamTimeoutError,
)
from app.services.sarvam.realtime_protocol import (
    PcmChunker,
    RealtimeSttConfig,
    SarvamProtocolError,
    build_audio_input,
    build_end,
    build_ping,
    parse_server_message,
)
from app.services.sarvam.realtime_stt import (
    CLOSED,
    FAILED,
    OPEN,
    SarvamRealtimeSttClient,
    error_from_close,
    error_from_event,
)

KEY = "sk-test-SECRET-key-123"


# --- fake Sarvam -----------------------------------------------------------------------------


def session_begin(request_id: str = "req-1") -> str:
    return json.dumps({
        "event": "session.begin", "request_id": request_id,
        "config": {"model": "saaras:v4", "language_code": "auto", "encoding": "linear16", "sample_rate": 16000,
                   "stream_type": "balanced", "mode": "transcribe", "turn_detection": "vad",
                   "return_timestamps": True},
    })


class FakeWS:
    """Scripted server socket. `push()` queues server messages; `push_close()` closes from the server."""

    def __init__(self, script: list | None = None, *, on_send=None) -> None:
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[str] = []
        self.closed = False
        self.on_send = on_send
        for item in script or []:
            self.incoming.put_nowait(item)

    def push(self, message: dict | str) -> None:
        self.incoming.put_nowait(message if isinstance(message, str) else json.dumps(message))

    def push_close(self, code: int, reason: str = "") -> None:
        self.incoming.put_nowait(Close(code, reason))

    async def recv(self):
        item = await self.incoming.get()
        if isinstance(item, Close):
            self.closed = True
            raise ConnectionClosedError(item, None)
        return item

    async def send(self, text: str) -> None:
        if self.closed:
            raise ConnectionClosedError(Close(1006, ""), None)
        self.sent.append(text)
        if self.on_send:
            self.on_send(self, json.loads(text))

    async def close(self) -> None:
        self.closed = True

    def sent_events(self) -> list[dict]:
        return [json.loads(t) for t in self.sent]


class FakeConnect:
    """Stands in for websockets.connect; returns queued sockets or raises queued exceptions."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0) if self.outcomes else FakeWS([session_begin()])

        async def _open():
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome == "hang":
                await asyncio.Event().wait()
            return outcome

        return _open()


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def config(**overrides) -> RealtimeSttConfig:
    base = RealtimeSttConfig(connect_timeout_s=1.0, end_timeout_s=1.0, keepalive_interval_s=60.0)
    return replace(base, **overrides).validated()


def client_for(connect: FakeConnect, *, events: list | None = None, sleeps: Sleeps | None = None,
               **kw) -> SarvamRealtimeSttClient:
    cfg = kw.pop("cfg", None) or config()
    return SarvamRealtimeSttClient(
        cfg, KEY, on_event=(events.append if events is not None else None), connect_fn=connect,
        sleep=sleeps or Sleeps(), **kw,
    )


async def settle(predicate, timeout: float = 2.0) -> None:
    async def _wait() -> None:
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(_wait(), timeout)


def stt_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t.get_name().startswith("sarvam-stt-") and not t.done()]


def invalid_status(code: int) -> InvalidStatus:
    return InvalidStatus(Response(code, "x", Headers()))


# =============================================================================================
# configuration and URL
# =============================================================================================


def test_default_connection_url_and_parameters() -> None:
    cfg = RealtimeSttConfig().validated()
    parts = urlsplit(cfg.build_url())
    assert (parts.scheme, parts.netloc, parts.path) == ("wss", "api.sarvam.ai", "/speech-to-text-realtime/ws")
    assert {k: v[0] for k, v in parse_qs(parts.query).items()} == {
        "model": "saaras:v4", "language_code": "auto", "mode": "transcribe", "stream_type": "balanced",
        "encoding": "linear16", "sample_rate": "16000", "endpointing": "vad", "return_timestamps": "true",
    }
    assert "legacy" not in cfg.url and "/speech-to-text/ws" not in cfg.build_url()


def test_config_from_settings_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.core.config.load_dotenv", lambda *_a, **_k: None)
    for name, value in {
        "SARVAM_STT_MODEL": "saaras:v4", "SARVAM_STT_LANGUAGE_CODE": "bn-IN", "SARVAM_STT_MODE": "codemix",
        "SARVAM_STT_STREAM_TYPE": "fast", "SARVAM_STT_ENCODING": "linear16", "SARVAM_STT_SAMPLE_RATE": "8000",
        "SARVAM_STT_ENDPOINTING": "manual", "SARVAM_STT_RETURN_TIMESTAMPS": "false",
    }.items():
        monkeypatch.setenv(name, value)
    cfg = RealtimeSttConfig.from_settings(Settings.from_env())
    assert cfg.query_params() == {
        "model": "saaras:v4", "language_code": "bn-IN", "mode": "codemix", "stream_type": "fast",
        "encoding": "linear16", "sample_rate": "8000", "endpointing": "manual", "return_timestamps": "false",
    }
    assert cfg.chunk_bytes == 1600  # 100 ms at 8 kHz linear16


def test_settings_defaults_for_realtime() -> None:
    s = Settings()
    assert (s.sarvam_stt_mode, s.sarvam_stt_stream_type, s.sarvam_stt_encoding) == ("transcribe", "balanced", "linear16")
    assert (s.sarvam_stt_sample_rate, s.sarvam_stt_endpointing, s.sarvam_stt_return_timestamps) == (16000, "vad", True)
    assert s.sarvam_stream_type == "fast"  # legacy field untouched


@pytest.mark.parametrize(
    "overrides",
    [
        {"sample_rate": 44100}, {"sample_rate": 24000}, {"encoding": "opus"}, {"mode": "summarize"},
        {"stream_type": "turbo"}, {"endpointing": "auto"}, {"language_code": "bn-BD"}, {"language_code": "od-IN"},
        {"model": ""}, {"url": "ws://api.sarvam.ai/speech-to-text-realtime/ws"},
        {"url": "wss://api.sarvam.ai/speech-to-text-realtime/ws?api_key=x"}, {"chunk_ms": 10},
        {"chunk_ms": 600}, {"max_reconnect_attempts": 99},
    ],
)
def test_invalid_config_is_rejected_not_replaced(overrides: dict) -> None:
    with pytest.raises(SarvamConfigError):
        replace(RealtimeSttConfig(), **overrides).validated()


def test_backoff_is_exponential_and_capped() -> None:
    cfg = RealtimeSttConfig()
    assert [cfg.backoff_delay(n) for n in range(1, 8)] == [0.5, 1, 2, 4, 8, 8, 8]


# =============================================================================================
# audio chunking and encoding
# =============================================================================================


def test_100ms_chunk_is_3200_bytes() -> None:
    assert RealtimeSttConfig().chunk_bytes == 3200


def test_five_20ms_frames_become_one_3200_byte_chunk_in_order() -> None:
    chunker = PcmChunker(3200, 2)
    frames = [bytes([i]) * 640 for i in range(1, 11)]  # ten 20 ms Vobiz-sized frames
    out: list[bytes] = []
    for i, frame in enumerate(frames, start=1):
        produced = chunker.push(frame)
        assert len(produced) == (1 if i % 5 == 0 else 0)
        out += produced
    assert [len(c) for c in out] == [3200, 3200]
    assert out[0] == b"".join(frames[:5]) and out[1] == b"".join(frames[5:])
    assert chunker.buffered == 0 and chunker.flush() is None


def test_chunker_flush_and_large_input() -> None:
    chunker = PcmChunker(3200, 2)
    data = os.urandom(3200 * 3 + 1000)
    chunks = chunker.push(data)
    rest = chunker.flush()
    assert [len(c) for c in chunks] == [3200] * 3 and len(rest) == 1000
    assert b"".join(chunks) + rest == data  # bytes unchanged, nothing lost, nothing reordered


def test_chunker_rejects_misaligned_input() -> None:
    chunker = PcmChunker(3200, 2)
    with pytest.raises(ValueError):
        chunker.push(b"\x00\x01\x02")
    with pytest.raises(ValueError):
        PcmChunker(3201, 2)


def test_audio_input_message_is_base64_of_raw_pcm() -> None:
    pcm = os.urandom(3200)
    message = json.loads(build_audio_input(pcm))
    assert set(message) == {"event", "audio"} and message["event"] == "audio_input"
    assert base64.b64decode(message["audio"], validate=True) == pcm
    assert not base64.b64decode(message["audio"]).startswith(b"RIFF")
    assert json.loads(build_end()) == {"event": "end"} and json.loads(build_ping()) == {"event": "ping"}


# =============================================================================================
# event parsing
# =============================================================================================


def test_parse_session_begin() -> None:
    event = parse_server_message(session_begin("req-42"))
    assert event.type == "session.begin" and event.request_id == "req-42"
    assert event.session_config["turn_detection"] == "vad"
    with pytest.raises(SarvamProtocolError):
        parse_server_message(json.dumps({"event": "session.begin"}))


def test_parse_partial_transcript_with_language() -> None:
    event = parse_server_message(
        json.dumps({"event": "transcript.partial", "utterance_idx": 0, "text": "আমার ইন্টারনেট", "language": "bn-IN"}),
        request_id="req-1",
    )
    assert event.type == "transcript.partial" and event.is_partial and not event.is_final
    assert event.text == "আমার ইন্টারনেট" and event.language == "bn-IN" and event.utterance_idx == 0
    assert event.request_id == "req-1" and event.received_at.endswith("+00:00")
    assert event.language_confidence is None and event.start_s is None  # not sent on partials
    assert "ইন্টারনেট" not in repr(event)


def test_parse_final_transcript_with_language_and_timestamps() -> None:
    event = parse_server_message(json.dumps({
        "event": "transcript.final", "utterance_idx": 2, "text": "My internet is not working",
        "language": "en-IN", "language_confidence": "0.93", "start_s": "1.20", "end_s": "3.45",
    }))
    assert event.type == "transcript.final" and event.is_final and not event.is_partial
    assert (event.language, event.language_confidence) == ("en-IN", 0.93)
    assert (event.start_s, event.end_s, event.utterance_idx) == (1.2, 3.45, 2)


def test_final_without_auto_language_has_no_language_metadata() -> None:
    event = parse_server_message(json.dumps({"event": "transcript.final", "utterance_idx": 0, "text": "hi"}))
    assert event.language is None and event.language_confidence is None and event.start_s is None


def test_parse_vad_config_pong_and_session_end() -> None:
    assert parse_server_message('{"event":"vad.speech_start","utterance_idx":0,"confidence":"0.8"}').vad_confidence == 0.8
    assert parse_server_message('{"event":"vad.speech_end","utterance_idx":0}').type == "vad.speech_end"
    assert parse_server_message('{"event":"config.updated","applied":["mode",5]}').applied == ("mode",)
    assert parse_server_message('{"event":"pong"}').type == "pong"
    end = parse_server_message(json.dumps({"event": "session.end", "request_id": "r", "audio_duration_s": "4.5",
                                           "total_utterances": 2, "total_duration_s": "6.0"}))
    assert (end.audio_duration_s, end.total_utterances, end.total_duration_s, end.request_id) == (4.5, 2, 6.0, "r")


def test_parse_structured_error() -> None:
    event = parse_server_message(json.dumps({"event": "error", "code": "invalid_subscription_key", "is_fatal": True,
                                             "message": "bad key", "status_code": 401}))
    assert event.type == "error"
    assert (event.error.code, event.error.is_fatal, event.error.status_code) == ("invalid_subscription_key", True, 401)
    non_fatal = parse_server_message('{"event":"error","code":"chunk_too_large","is_fatal":false,"message":"too big"}')
    assert non_fatal.error.is_fatal is False and non_fatal.error.status_code is None


@pytest.mark.parametrize(
    ("err", "exc"),
    [("invalid_subscription_key", SarvamAuthError), ("invalid_request", SarvamRequestError),
     ("invalid_model", SarvamRequestError), ("inactivity_timeout", SarvamStreamClosedError)],
)
def test_error_event_mapping(err: str, exc: type) -> None:
    event = parse_server_message(json.dumps({"event": "error", "code": err, "is_fatal": True, "message": "m"}))
    assert isinstance(error_from_event(event.error), exc)


@pytest.mark.parametrize(
    ("code", "exc"),
    [(1003, SarvamAuthError), (4000, SarvamRequestError), (1011, SarvamServerError),
     (1008, SarvamStreamClosedError), (1000, SarvamStreamClosedError), (1006, SarvamConnectionError), (None, SarvamConnectionError)],
)
def test_close_code_mapping(code, exc: type) -> None:
    assert isinstance(error_from_close(code, "reason"), exc)


@pytest.mark.parametrize(
    "raw",
    ["not json", "", "[]", "42", "null", "{}", '{"event":5}', '{"event":""}', "[" * 5000,
     '{"event":"transcript.final","utterance_idx":0}', '{"event":"transcript.partial","text":7}',
     '{"event":"error","is_fatal":true,"message":"x"}', b'{"event":"pong"}'],
)
def test_malformed_messages_raise_protocol_error(raw) -> None:
    with pytest.raises(SarvamProtocolError):
        parse_server_message(raw)


def test_unexpected_event_is_typed_unknown() -> None:
    event = parse_server_message('{"event":"brand.new_thing"}')
    assert event.type == "unknown" and event.raw_event == "brand.new_thing"


# =============================================================================================
# client: connect, auth, session
# =============================================================================================


async def test_connect_sends_key_in_header_only_and_waits_for_session_begin() -> None:
    events: list = []
    connect = FakeConnect(FakeWS([session_begin("req-7")]))
    client = client_for(connect, events=events)
    begin = await client.connect()

    url, kwargs = connect.calls[0]
    assert kwargs["additional_headers"] == {"api-subscription-key": KEY}
    assert KEY not in url and "api-subscription-key" not in url and "key" not in urlsplit(url).query
    assert kwargs["open_timeout"] == 1.0
    assert begin.type == "session.begin" and client.request_id == "req-7" and client.state == OPEN
    assert [e.type for e in events] == ["session.begin"]
    await client.close()


async def test_missing_api_key_is_a_config_error_without_connecting() -> None:
    connect = FakeConnect()
    client = SarvamRealtimeSttClient(config(), "", connect_fn=connect)
    with pytest.raises(SarvamConfigError):
        await client.connect()
    assert connect.calls == []


@pytest.mark.parametrize("status", [401, 403])
async def test_handshake_rejection_is_auth_error_and_not_retried(status: int) -> None:
    connect = FakeConnect(invalid_status(status), FakeWS([session_begin()]))
    sleeps = Sleeps()
    client = client_for(connect, sleeps=sleeps)
    with pytest.raises(SarvamAuthError) as info:
        await client.connect()
    assert info.value.upstream_status == status
    assert len(connect.calls) == 1 and sleeps.delays == [] and client.state == FAILED
    assert KEY not in info.value.message


async def test_invalid_key_error_event_then_1003_close_is_not_retried() -> None:
    ws = FakeWS([json.dumps({"event": "error", "code": "invalid_subscription_key", "is_fatal": True,
                             "message": "Invalid API key", "status_code": 401}), Close(1003, "invalid key")])
    connect = FakeConnect(ws)
    client = client_for(connect)
    with pytest.raises(SarvamAuthError):
        await client.connect()
    assert len(connect.calls) == 1 and ws.closed


@pytest.mark.parametrize("script", [[Close(4000, "invalid language_code")],
                                    [json.dumps({"event": "error", "code": "invalid_request", "is_fatal": True,
                                                 "message": "sample_rate"}), Close(4000, "")]])
async def test_invalid_parameters_are_not_retried(script: list) -> None:
    connect = FakeConnect(FakeWS(script))
    with pytest.raises(SarvamRequestError):
        await client_for(connect).connect()
    assert len(connect.calls) == 1


async def test_connection_timeout_retries_with_bounded_backoff() -> None:
    connect = FakeConnect("hang", "hang", "hang", "hang", "hang")
    sleeps = Sleeps()
    client = client_for(connect, sleeps=sleeps, cfg=config(connect_timeout_s=0.02, max_reconnect_attempts=3))
    with pytest.raises(SarvamTimeoutError):
        await client.connect()
    assert len(connect.calls) == 4  # 1 attempt + 3 retries, then stop
    assert sleeps.delays == [0.5, 1.0, 2.0]
    assert client.state == FAILED


async def test_session_begin_timeout_closes_the_socket() -> None:
    ws = FakeWS([])  # connects but never sends session.begin
    client = client_for(FakeConnect(ws), cfg=config(connect_timeout_s=0.05, max_reconnect_attempts=0))
    with pytest.raises(SarvamTimeoutError):
        await client.connect()
    assert ws.closed


async def test_transient_failure_then_success() -> None:
    connect = FakeConnect(OSError("dns"), invalid_status(503), FakeWS([session_begin("req-ok")]))
    sleeps = Sleeps()
    client = client_for(connect, sleeps=sleeps)
    await client.connect()
    assert client.request_id == "req-ok" and len(connect.calls) == 3 and sleeps.delays == [0.5, 1.0]
    await client.close()


async def test_events_before_session_begin_are_tolerated() -> None:
    client = client_for(FakeConnect(FakeWS(['{"event":"pong"}', session_begin("r2")])))
    await client.connect()
    assert client.request_id == "r2"
    await client.close()


# =============================================================================================
# client: audio, transcripts, errors
# =============================================================================================


async def test_send_audio_aggregates_and_preserves_order() -> None:
    ws = FakeWS([session_begin()])
    client = client_for(FakeConnect(ws))
    await client.connect()
    frames = [bytes([i]) * 640 for i in range(1, 13)]  # 12 frames = 240 ms
    for frame in frames:
        await client.send_audio(frame)
    assert [len(base64.b64decode(m["audio"])) for m in ws.sent_events()] == [3200, 3200]
    assert await client.flush_audio() is True
    payloads = [base64.b64decode(m["audio"]) for m in ws.sent_events()]
    assert [len(p) for p in payloads] == [3200, 3200, 1280]
    assert b"".join(payloads) == b"".join(frames)
    assert client.stats["chunks_sent"] == 3 and client.stats["bytes_sent"] == 7680
    await client.close()


async def test_concurrent_send_audio_keeps_chunks_whole_and_ordered() -> None:
    ws = FakeWS([session_begin()])
    client = client_for(FakeConnect(ws))
    await client.connect()
    await asyncio.gather(*(client.send_audio(bytes([i]) * 3200) for i in range(20)))
    payloads = [base64.b64decode(m["audio"]) for m in ws.sent_events()]
    assert all(len(set(p)) == 1 for p in payloads)  # no chunk mixes two calls' audio
    assert sorted(p[0] for p in payloads) == list(range(20))
    await client.close()


async def test_partial_and_final_events_reach_the_handler_distinctly() -> None:
    events: list = []
    ws = FakeWS([session_begin("req-9")])
    client = client_for(FakeConnect(ws), events=events)
    await client.connect()
    ws.push({"event": "vad.speech_start", "utterance_idx": 0})
    ws.push({"event": "transcript.partial", "utterance_idx": 0, "text": "my inter", "language": "en-IN"})
    ws.push({"event": "transcript.final", "utterance_idx": 0, "text": "My internet is not working",
             "language": "en-IN", "language_confidence": "0.97", "start_s": "0.4", "end_s": "2.1"})
    ws.push({"event": "vad.speech_end", "utterance_idx": 0})
    await settle(lambda: len(events) == 5)
    types = [e.type for e in events]
    assert types == ["session.begin", "vad.speech_start", "transcript.partial", "transcript.final", "vad.speech_end"]
    partial, final = events[2], events[3]
    assert partial.is_partial and not partial.is_final and final.is_final and not final.is_partial
    assert final.request_id == "req-9" and final.language_confidence == 0.97 and final.start_s == 0.4
    assert client.stats["partials"] == 1 and client.stats["finals"] == 1
    await client.close()


async def test_malformed_and_unknown_messages_do_not_break_the_session() -> None:
    events: list = []
    ws = FakeWS([session_begin()])
    client = client_for(FakeConnect(ws), events=events)
    await client.connect()
    ws.push("not json")
    ws.push('{"no":"event"}')
    ws.push('{"event":"brand.new"}')
    ws.push({"event": "transcript.final", "utterance_idx": 0, "text": "ok"})
    await settle(lambda: client.stats["finals"] == 1)
    assert client.stats["protocol_errors"] == 2 and client.stats["unknown_events"] == 1
    assert client.state == OPEN
    await client.close()


async def test_too_many_malformed_messages_fail_the_stream() -> None:
    ws = FakeWS([session_begin()])
    client = client_for(FakeConnect(ws), auto_reconnect=True)
    await client.connect()
    for _ in range(60):
        ws.push("garbage")
    await settle(lambda: client.state == FAILED)
    assert isinstance(client.last_error, SarvamProtocolError) and ws.closed
    with pytest.raises(SarvamConnectionError):
        await client.send_audio(b"\x00" * 640)
    await client.close()


async def test_non_fatal_error_keeps_session_open() -> None:
    events: list = []
    ws = FakeWS([session_begin()])
    client = client_for(FakeConnect(ws), events=events)
    await client.connect()
    ws.push({"event": "error", "code": "chunk_too_large", "is_fatal": False, "message": "frame too big"})
    await settle(lambda: client.stats["errors"] == 1)
    assert client.state == OPEN and events[-1].error.code == "chunk_too_large"
    await client.close()


async def test_fatal_error_then_close_reports_the_specific_error() -> None:
    ws = FakeWS([session_begin()])
    client = client_for(FakeConnect(ws), auto_reconnect=True)
    await client.connect()
    ws.push({"event": "error", "code": "invalid_subscription_key", "is_fatal": True, "message": "key revoked",
             "status_code": 401})
    ws.push_close(1003, "invalid key")
    await settle(lambda: client.state == FAILED)
    assert isinstance(client.last_error, SarvamAuthError)  # not retried despite auto_reconnect
    assert client.close_code == 1003 and client.stats["reconnects"] == 0
    await client.close()


async def test_inactivity_close_is_not_retried() -> None:
    ws = FakeWS([session_begin()])
    client = client_for(FakeConnect(ws), auto_reconnect=True)
    await client.connect()
    ws.push({"event": "error", "code": "inactivity_timeout", "is_fatal": True, "message": "idle", "status_code": 408})
    ws.push_close(1008, "inactivity")
    await settle(lambda: client.state == FAILED)
    assert isinstance(client.last_error, SarvamStreamClosedError) and client.stats["reconnects"] == 0
    await client.close()


async def test_handler_exceptions_are_counted_and_do_not_stop_receiving() -> None:
    seen: list = []

    def handler(event) -> None:
        seen.append(event.type)
        if event.type == "transcript.partial":
            raise RuntimeError("consumer bug")

    ws = FakeWS([session_begin()])
    client = SarvamRealtimeSttClient(config(), KEY, on_event=handler, connect_fn=FakeConnect(ws), sleep=Sleeps())
    await client.connect()
    ws.push({"event": "transcript.partial", "utterance_idx": 0, "text": "a"})
    ws.push({"event": "transcript.final", "utterance_idx": 0, "text": "a b"})
    await settle(lambda: "transcript.final" in seen)
    assert client.stats["handler_errors"] == 1 and client.state == OPEN
    await client.close()


# =============================================================================================
# client: disconnects, reconnects, keepalive
# =============================================================================================


async def test_unexpected_disconnect_without_auto_reconnect_fails_and_refuses_audio() -> None:
    ws = FakeWS([session_begin()])
    client = client_for(FakeConnect(ws))
    await client.connect()
    ws.push_close(1011, "internal")
    await settle(lambda: client.state == FAILED)
    assert isinstance(client.last_error, SarvamServerError) and client.close_code == 1011
    with pytest.raises(SarvamConnectionError):
        await client.send_audio(b"\x00" * 640)  # never silently dropped
    await client.close()


async def test_auto_reconnect_after_server_error_gets_a_new_session() -> None:
    first, second = FakeWS([session_begin("req-a")]), FakeWS([session_begin("req-b")])
    sleeps = Sleeps()
    client = client_for(FakeConnect(first, second), sleeps=sleeps, auto_reconnect=True)
    await client.connect()
    first.push_close(1011, "internal")
    await settle(lambda: client.request_id == "req-b" and client.state == OPEN)
    assert client.stats["reconnects"] == 1 and first.closed and sleeps.delays == [0.5]
    await client.send_audio(b"\x00" * 3200)
    assert len(second.sent) == 1 and len(first.sent) == 0
    await client.close()


async def test_reconnects_are_capped_for_the_client_lifetime() -> None:
    sockets = [FakeWS([session_begin(f"req-{i}")]) for i in range(5)]
    client = client_for(FakeConnect(*sockets), auto_reconnect=True, cfg=config(max_reconnect_attempts=2))
    await client.connect()
    for i in range(3):
        await settle(lambda i=i: client.request_id == f"req-{i}" and client.state == OPEN)
        sockets[i].push_close(1011, "internal")
    await settle(lambda: client.state == FAILED)
    assert client.stats["reconnects"] == 2  # third drop is not reconnected
    await client.close()


async def test_keepalive_ping_when_idle_and_pong_counted() -> None:
    ws = FakeWS([session_begin()], on_send=lambda w, m: w.push({"event": "pong"}) if m["event"] == "ping" else None)
    client = client_for(FakeConnect(ws), cfg=config(keepalive_interval_s=0.05))
    await client.connect()
    await settle(lambda: client.stats["pongs"] >= 1)
    assert {"event": "ping"} in ws.sent_events() and client.stats["pings_sent"] >= 1
    await client.close()


async def test_keepalive_does_not_ping_while_audio_flows() -> None:
    ws = FakeWS([session_begin()])
    client = client_for(FakeConnect(ws), cfg=config(keepalive_interval_s=0.1))
    await client.connect()
    for _ in range(6):
        await client.send_audio(b"\x00" * 3200)
        await asyncio.sleep(0.03)
    assert all(m["event"] == "audio_input" for m in ws.sent_events())
    await client.close()


# =============================================================================================
# client: shutdown and cancellation
# =============================================================================================


async def test_graceful_end_flushes_sends_end_and_waits_for_session_end() -> None:
    def server(ws, message) -> None:
        if message["event"] == "end":
            ws.push({"event": "session.end", "request_id": "req-1", "audio_duration_s": "0.15",
                     "total_utterances": 0, "total_duration_s": "0.4"})
            ws.push_close(1000, "")

    ws = FakeWS([session_begin()], on_send=server)
    client = client_for(FakeConnect(ws))
    await client.connect()
    await client.send_audio(b"\x01\x00" * 2400)  # 150 ms: one full chunk + 50 ms remainder
    await client.end()
    events = [m["event"] for m in ws.sent_events()]
    assert events == ["audio_input", "audio_input", "end"]
    assert len(base64.b64decode(ws.sent_events()[1]["audio"])) == 1600
    assert client.state == CLOSED and client.stats["audio_duration_s"] == 0.15 and ws.closed
    assert stt_tasks() == []


async def test_end_without_session_end_is_bounded() -> None:
    ws = FakeWS([session_begin()])
    client = client_for(FakeConnect(ws), cfg=config(end_timeout_s=0.05))
    await client.connect()
    await asyncio.wait_for(client.end(), 1)
    assert client.state == CLOSED and stt_tasks() == []


async def test_close_is_idempotent_and_leaves_no_tasks() -> None:
    client = client_for(FakeConnect(FakeWS([session_begin()])), cfg=config(keepalive_interval_s=0.05))
    await client.connect()
    assert len(stt_tasks()) == 2  # receiver + keepalive
    await client.close()
    await client.close()
    assert client.state == CLOSED and stt_tasks() == []
    with pytest.raises(SarvamConnectionError):
        await client.connect()  # a closed client cannot be reused


async def test_async_context_manager() -> None:
    async with client_for(FakeConnect(FakeWS([session_begin("ctx")]))) as client:
        assert client.request_id == "ctx"
    assert client.state == CLOSED and stt_tasks() == []


async def test_cancelling_connect_closes_the_half_open_socket() -> None:
    ws = FakeWS([])  # never sends session.begin
    client = client_for(FakeConnect(ws), cfg=config(connect_timeout_s=5))
    task = asyncio.create_task(client.connect())
    await settle(lambda: client.stats["connect_attempts"] == 1)
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ws.closed and client.state != OPEN and stt_tasks() == []


async def test_cancelling_a_task_mid_session_then_close_cleans_up() -> None:
    ws = FakeWS([session_begin()])
    client = client_for(FakeConnect(ws))
    await client.connect()

    async def streamer() -> None:
        while True:
            await client.send_audio(b"\x00" * 640)
            await asyncio.sleep(0)

    task = asyncio.create_task(streamer())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await client.close()
    assert stt_tasks() == [] and all(len(base64.b64decode(m["audio"])) == 3200 for m in ws.sent_events())


# =============================================================================================
# no secret / audio / transcript leakage
# =============================================================================================


async def test_logs_never_contain_key_audio_or_transcript(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    pcm = os.urandom(3200)
    secret_text = "my account number is 0171 secret"

    def server(ws, message) -> None:
        if message["event"] == "end":
            ws.push({"event": "session.end", "request_id": "req-1", "audio_duration_s": "0.1"})

    ws = FakeWS([session_begin()], on_send=server)
    client = client_for(FakeConnect(invalid_status(503), ws), auto_reconnect=True)
    await client.connect()
    await client.send_audio(pcm)
    ws.push({"event": "transcript.partial", "utterance_idx": 0, "text": secret_text})
    ws.push({"event": "transcript.final", "utterance_idx": 0, "text": secret_text, "language": "en-IN"})
    ws.push({"event": "error", "code": "chunk_too_large", "is_fatal": False, "message": "too big"})
    await settle(lambda: client.stats["finals"] == 1 and client.stats["errors"] == 1)
    await client.end()

    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for forbidden in (KEY, base64.b64encode(pcm).decode(), secret_text, "0171"):
        assert forbidden not in text
    final_log = next(r for r in caplog.records if r.getMessage() == "sarvam_stt_final")
    assert final_log.text_length == len(secret_text) and final_log.language == "en-IN"
    assert any(r.getMessage() == "sarvam_stt_session_begin" for r in caplog.records)
    assert any(r.getMessage() == "sarvam_stt_closed" for r in caplog.records)


def test_repr_of_client_config_and_events_has_no_secrets() -> None:
    client = SarvamRealtimeSttClient(config(), KEY, connect_fn=FakeConnect())
    assert KEY not in repr(client.config) and KEY not in repr(client.__dict__.get("_key"))


def test_diagnostics_show_realtime_params_without_the_key() -> None:
    settings = Settings(sarvam_api_key=SecretStr(KEY))
    response = TestClient(create_app(settings)).get("/api/sarvam/diagnostics")
    body = response.json()
    assert KEY not in response.text
    assert body["stt"]["realtime_params"]["stream_type"] == "balanced"
    assert body["stt"]["realtime_params"]["encoding"] == "linear16"
    assert body["stt"]["realtime_config_valid"] is True


def test_diagnostics_flag_invalid_realtime_config() -> None:
    body = TestClient(create_app(Settings(sarvam_stt_sample_rate=44100))).get("/api/sarvam/diagnostics").json()
    assert body["stt"]["realtime_config_valid"] is False
    assert any("sample_rate" in w for w in body["warnings"])
