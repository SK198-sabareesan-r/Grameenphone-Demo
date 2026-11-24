"""Development-only inbound capture (M4.2). Mocked: no real Vobiz call."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.main import create_app
from app.services.audio.audio_buffer import AudioFrame, BoundedFrameQueue
from app.services.audio.formats import ENC_L16, AudioFormat
from app.services.voice import dev_capture as dc
from app.services.voice.call_manager import StreamSession
from app.services.voice.playback import PlaybackController
from app.integrations.vobiz.stream_sender import VobizStreamSender
from tests.test_vobiz_websocket import CALL, HEADERS, media_msg, open_stream, wait_until

TEST_PHONE = "+919876543210"
OTHER_PHONE = "+919999999999"
L16_16K = AudioFormat(ENC_L16, 16000)


def capture_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        app_env="development",
        vobiz_dev_capture_enabled=True,
        vobiz_dev_capture_destination=TEST_PHONE,
        vobiz_dev_capture_max_seconds=1,
        vobiz_dev_capture_dir=str(tmp_path / "captures"),
        vobiz_auth_id="MA_TEST",
        vobiz_auth_token=SecretStr("vobiz-auth-token-for-tests"),
        internal_api_token=SecretStr("internal-token"),
        public_base_url="https://abc.ngrok.app",
        vobiz_validate_signature=False,
    )
    values.update(overrides)
    return Settings(**values)


async def _noop(_: str) -> None:
    return None


def make_session(call_id: str = "call-1", stream_id: str = "stream-abcdef12") -> StreamSession:
    sender = VobizStreamSender(stream_id, _noop)
    return StreamSession(call_id=call_id, stream_id=stream_id, account_id="1", tracks=("inbound",),
                         audio_format=L16_16K, queue=BoundedFrameQueue(10), sender=sender,
                         playback=PlaybackController(sender))


def frame(n: int, data: bytes) -> AudioFrame:
    return AudioFrame(data=data, format=L16_16K, track="inbound", timestamp_ms=n * 20, chunk=n, sequence=n,
                      stream_id="stream-abcdef12")


# --- enablement guards -----------------------------------------------------------------------


def test_disabled_by_default() -> None:
    assert dc.build_dev_capture(Settings()) is None
    s = Settings()
    assert (s.app_env, s.vobiz_dev_capture_enabled) == ("production", False)


def test_refused_outside_development(tmp_path) -> None:
    assert dc.build_dev_capture(capture_settings(tmp_path, app_env="production")) is None
    assert dc.build_dev_capture(capture_settings(tmp_path, app_env="staging")) is None


@pytest.mark.parametrize(
    "overrides",
    [{"vobiz_dev_capture_destination": ""}, {"vobiz_dev_capture_destination": "not-a-number"},
     {"vobiz_dev_capture_max_seconds": 0}, {"vobiz_dev_capture_max_seconds": 31}],
)
def test_incomplete_or_unsafe_config_is_an_error(tmp_path, overrides: dict) -> None:
    with pytest.raises(dc.DevCaptureConfigError):
        dc.build_dev_capture(capture_settings(tmp_path, **overrides))


def test_capture_dir_inside_project_is_refused(tmp_path) -> None:
    with pytest.raises(dc.DevCaptureConfigError):
        dc.resolve_capture_dir(str(dc.PROJECT_ROOT / "captures"))
    with pytest.raises(dc.DevCaptureConfigError):
        dc.resolve_capture_dir(str(dc.PROJECT_ROOT))
    assert dc.resolve_capture_dir(str(tmp_path / "x")) == (tmp_path / "x").resolve()
    default = dc.resolve_capture_dir("")
    assert dc.PROJECT_ROOT.resolve() not in default.parents


def test_app_without_capture_config_installs_no_capture() -> None:
    app = create_app(Settings())
    assert app.state.dev_capture is None and app.state.stream_handler.audio_sink_factory is None


def test_explicit_sink_factory_is_never_overridden(tmp_path) -> None:
    def explicit(session):
        return None

    app = create_app(capture_settings(tmp_path), audio_sink_factory=explicit)
    assert app.state.dev_capture is None and app.state.stream_handler.audio_sink_factory is explicit


# --- registry: only test calls ---------------------------------------------------------------


def test_registry_only_accepts_the_test_destination() -> None:
    reg = dc.DevCaptureRegistry(TEST_PHONE)
    assert reg.register_if_test_call("c1", "919876543210") is True  # normalized E.164
    assert reg.register_if_test_call("c2", OTHER_PHONE) is False
    assert reg.register_if_test_call("c3", None) is False and reg.register_if_test_call("", TEST_PHONE) is False
    assert reg.is_registered("c1") and not reg.is_registered("c2")


def test_registry_is_bounded() -> None:
    reg = dc.DevCaptureRegistry(TEST_PHONE, max_entries=3)
    for i in range(10):
        reg.register(f"c{i}")
    assert [reg.is_registered(f"c{i}") for i in range(10)] == [False] * 7 + [True] * 3


def test_factory_declines_unregistered_calls(tmp_path) -> None:
    registry, factory = dc.build_dev_capture(capture_settings(tmp_path))
    assert factory(make_session("customer-call")) is None
    registry.register("test-call")
    assert isinstance(factory(make_session("test-call")), dc.DevCaptureSink)


# --- sink: limits, raw bytes, files ----------------------------------------------------------


async def test_capture_keeps_raw_bytes_and_respects_the_limit(tmp_path) -> None:
    session = make_session()
    sink = dc.DevCaptureSink(session, tmp_path / "cap", max_seconds=1)
    frames = [os.urandom(640) for _ in range(60)]  # 1.2 s offered, limit 1.0 s
    for i, data in enumerate(frames, start=1):
        await sink.handle_frame(frame(i, data))
    assert sink.max_bytes == 32000
    assert sink.frames_captured == 50 and sink.frames_after_limit == 10
    await sink.close("socket_closed")

    raw = sink.path.read_bytes()
    assert raw == b"".join(frames[:50])  # exactly as received, nothing converted
    meta = json.loads(sink.path.with_suffix(".json").read_text())
    assert meta["encoding"] == "audio/x-l16" and meta["sample_rate"] == 16000 and meta["bytes"] == 32000
    assert meta["byte_order"] == "unverified" and meta["frames_after_limit"] == 10
    assert meta["first_chunk"] == 1 and meta["last_chunk"] == 50
    assert "call-1" not in sink.path.read_bytes().decode("latin-1") + json.dumps(meta)  # call id stored hashed
    assert meta["call_hash"] == dc.call_hash("call-1")
    assert not list((tmp_path / "cap").glob("*.tmp"))


async def test_partial_frame_at_the_limit_is_truncated_not_overrun(tmp_path) -> None:
    sink = dc.DevCaptureSink(make_session(), tmp_path, max_seconds=1)
    sink.max_bytes = 1000
    await sink.handle_frame(frame(1, b"\x01" * 640))
    await sink.handle_frame(frame(2, b"\x02" * 640))
    await sink.close("x")
    assert sink.path.read_bytes() == b"\x01" * 640 + b"\x02" * 360


async def test_empty_capture_writes_nothing(tmp_path) -> None:
    sink = dc.DevCaptureSink(make_session(), tmp_path / "cap", max_seconds=1)
    await sink.close("socket_closed")
    assert sink.path is None and not (tmp_path / "cap").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not enforced on Windows")
async def test_files_are_owner_only(tmp_path) -> None:
    sink = dc.DevCaptureSink(make_session(), tmp_path / "cap", max_seconds=1)
    await sink.handle_frame(frame(1, b"\x00" * 640))
    await sink.close("x")
    assert (sink.path.stat().st_mode & 0o777) == 0o600
    assert ((tmp_path / "cap").stat().st_mode & 0o777) == 0o700


def test_purge_removes_only_old_capture_files(tmp_path) -> None:
    old, new, other = tmp_path / "old.l16", tmp_path / "new.json", tmp_path / "notes.txt"
    for p in (old, new, other):
        p.write_bytes(b"x")
    past = time.time() - 48 * 3600
    os.utime(old, (past, past))
    os.utime(other, (past, past))
    assert dc.purge_old_captures(tmp_path, 24 * 3600) == 1
    assert not old.exists() and new.exists() and other.exists()  # unrelated files untouched
    assert dc.purge_old_captures(tmp_path, -1) == 1 and not new.exists()
    assert dc.purge_old_captures(tmp_path / "missing", 1) == 0


async def test_no_raw_audio_in_logs(tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    data = os.urandom(640)
    sink = dc.DevCaptureSink(make_session(), tmp_path, max_seconds=1)
    sink.max_bytes = 640
    await sink.handle_frame(frame(1, data))
    await sink.handle_frame(frame(2, data))
    await sink.close("x")
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert data.hex() not in text and repr(data) not in text
    import base64

    assert base64.b64encode(data).decode() not in text
    assert any(r.getMessage() == "vobiz_dev_capture_saved" for r in caplog.records)


# --- end to end through the real endpoint (simulated Vobiz) ----------------------------------


def test_registered_test_call_is_captured_through_the_stream(tmp_path) -> None:
    settings = capture_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        # the answer webhook registers the call because To is the configured test phone
        form = {"CallUUID": CALL, "From": "+919262175535", "To": TEST_PHONE, "Direction": "outbound"}
        assert client.post("/api/v1/vobiz/answer", data=form).status_code == 200
        frames = [os.urandom(640) for _ in range(10)]
        with open_stream(client) as stream:
            for i, data in enumerate(frames, start=1):
                stream.ws.send_json(media_msg(data, chunk=i, seq=i))
            wait_until(lambda: stream.session.consumer.frames_processed == 10)
        wait_until(lambda: list((tmp_path / "captures").glob("*.l16")))
    (raw_file,) = (tmp_path / "captures").glob("*.l16")
    assert raw_file.read_bytes() == b"".join(frames)


def test_other_calls_are_never_captured(tmp_path) -> None:
    with TestClient(create_app(capture_settings(tmp_path))) as client:
        form = {"CallUUID": CALL, "From": "+919262175535", "To": OTHER_PHONE, "Direction": "outbound"}
        client.post("/api/v1/vobiz/answer", data=form)
        with open_stream(client) as stream:
            stream.ws.send_json(media_msg(os.urandom(640)))
            wait_until(lambda: stream.session.stats.frames_received == 1)
            wait_until(lambda: stream.session.consumer.state == "unavailable")
            assert stream.session.consumer.frames_processed == 0
        listing = client.get("/api/v1/vobiz/streams", headers=HEADERS)
    assert not (tmp_path / "captures").exists() or not list((tmp_path / "captures").iterdir())
    assert listing.status_code == 200


def test_inbound_calls_are_not_registered_even_to_the_test_number(tmp_path) -> None:
    app = create_app(capture_settings(tmp_path))
    with TestClient(app) as client:
        form = {"CallUUID": "in-1", "From": TEST_PHONE, "To": TEST_PHONE, "Direction": "inbound"}
        client.post("/api/v1/vobiz/answer", data=form)
    assert not app.state.dev_capture.is_registered("in-1")


def test_diagnostics_warn_when_capture_enabled(tmp_path) -> None:
    with TestClient(create_app(capture_settings(tmp_path))) as client:
        warnings = " ".join(client.get("/api/v1/vobiz/diagnostics").json()["warnings"])
    assert "VOBIZ_DEV_CAPTURE_ENABLED" in warnings and TEST_PHONE not in warnings


def test_no_capture_http_endpoint_exists(tmp_path) -> None:
    with TestClient(create_app(capture_settings(tmp_path))) as client:
        paths = set(client.get("/openapi.json").json()["paths"])
    assert not [p for p in paths if "capture" in p]
