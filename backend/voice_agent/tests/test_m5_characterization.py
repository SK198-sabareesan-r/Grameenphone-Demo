"""M5.1 characterization tests: pin CURRENT behaviour that the M5 handoff design depends on.

These tests describe today's code. They are not a specification of M5 and must be revisited, not
bent, when M5 changes the behaviour they pin. Everything is synthetic: no network, no Vobiz, no
Sarvam, no real WebSocket provider, no credentials, no phone numbers.

Safe bootstrap (must run BEFORE anything imports `app.main`)
------------------------------------------------------------
`app/main.py` ends with `app = create_app()`. That calls `get_settings()` ->
`Settings.from_env()` -> `load_dotenv()`, which would read `.env` from the project root. To make
sure that never happens here, this module, before the first `app.main` import:

  1. installs an audit hook that records (basename only) any attempt to open a file named `.env*`;
  2. replaces `app.core.config.load_dotenv` with a function that counts the call and raises;
  3. replaces `app.core.config.get_settings` with a function returning a synthetic, credential-free
     `Settings()` (a plain pydantic model: it does not read the environment or any file);
  4. imports `app.main`, then restores the original functions so no other test module is affected.

No application code and no existing test is modified. If `app.main` was already imported by an
earlier module in the same process, isolation cannot be guaranteed, so this module skips itself.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import dataclasses
import inspect
import json
import os
import sys
import time

import pytest

# --- safe bootstrap -------------------------------------------------------------------------------

_BOOT: dict[str, object] = {"load_dotenv_calls": 0, "env_file_opens": []}


def _audit(event: str, args: tuple) -> None:  # records only the basename, never a path or content
    if event != "open" or not args:
        return
    try:
        target = args[0]
        if isinstance(target, (str, bytes, os.PathLike)):
            name = os.path.basename(os.fsdecode(target)).lower()
            if name.startswith(".env") or name.endswith(".env"):
                _BOOT["env_file_opens"].append(name)  # type: ignore[union-attr]
    except Exception:  # an audit hook must never break the interpreter
        pass


if "app.main" in sys.modules:
    pytest.skip(
        "app.main was imported before this module; .env isolation cannot be guaranteed. "
        "Run this file on its own.",
        allow_module_level=True,
    )

sys.addaudithook(_audit)

import app.core.config as _config  # noqa: E402  (does not load .env on import)

_orig_load_dotenv, _orig_get_settings = _config.load_dotenv, _config.get_settings


def _forbidden_load_dotenv(*_args: object, **_kwargs: object) -> bool:
    _BOOT["load_dotenv_calls"] = int(_BOOT["load_dotenv_calls"]) + 1  # type: ignore[call-overload]
    raise RuntimeError("load_dotenv() must not be called by the M5.1 characterization tests")


def _synthetic_get_settings() -> _config.Settings:
    return _config.Settings(app_name="m5-characterization-synthetic")


_config.load_dotenv, _config.get_settings = _forbidden_load_dotenv, _synthetic_get_settings
try:
    import app.main as _app_main  # noqa: E402  (module-level create_app() now uses synthetic settings)
finally:
    _config.load_dotenv, _config.get_settings = _orig_load_dotenv, _orig_get_settings
_app_main.get_settings = _orig_get_settings  # main bound the synthetic name at import: undo that too

# --- imports below may import app.main safely (already loaded with synthetic settings) -----------

from fastapi.testclient import TestClient  # noqa: E402

from app.integrations.vobiz.stream_events import MediaEvent, StartEvent, StreamProtocolError, parse_message  # noqa: E402
from app.integrations.vobiz.webhooks import EventDeduper  # noqa: E402
from app.publishers.pipeline import PipelineCallListener  # noqa: E402
from app.publishers.transcripts import TranscriptEvent  # noqa: E402
from app.services.audio.audio_buffer import AudioFrame  # noqa: E402
from app.services.voice import sarvam_stt_sink  # noqa: E402
from app.services.voice.call_manager import CallStateManager  # noqa: E402
from tests.test_transcript_pipeline import Rec, ev, final, leftover_tasks, pipe  # noqa: E402
from tests.test_transcript_pipeline import settle as pipeline_settle  # noqa: E402
from tests.test_vobiz_sarvam_stt import Collector, FakeSarvam, factory_for, make_session, start  # noqa: E402
from tests.test_vobiz_sarvam_stt import settle as stt_settle  # noqa: E402
from tests.test_vobiz_websocket import (  # noqa: E402
    CALL, STREAM, client, media_msg, open_stream, recv_json, start_msg, wait_until,
)
from starlette.websockets import WebSocketDisconnect  # noqa: E402

HANGUP = "/api/v1/vobiz/hangup"
assert client is not None  # imported as a pytest fixture


class _Call:
    """Minimal stand-in for a StreamSession as seen by PipelineCallListener (same idiom as M4.4 tests)."""

    def __init__(self, call_id: str = "c1") -> None:
        self.call_id = call_id


# =============================================================================================
# bootstrap verification (no secrets are printed or compared by value)
# =============================================================================================


def test_bootstrap_load_dotenv_was_never_called() -> None:
    assert _BOOT["load_dotenv_calls"] == 0


def test_bootstrap_no_dotenv_file_was_opened_in_this_process() -> None:
    assert _BOOT["env_file_opens"] == []  # basenames only; would list '.env' / '.env.example' if opened


def test_bootstrap_original_config_functions_are_restored() -> None:
    assert _config.load_dotenv is _orig_load_dotenv and _config.get_settings is _orig_get_settings
    assert _app_main.get_settings is _orig_get_settings


def test_bootstrap_module_level_app_uses_synthetic_credential_free_settings() -> None:
    s = _app_main.app.state.settings
    assert s.app_name == "m5-characterization-synthetic"
    secrets = [s.sarvam_api_key, s.vobiz_auth_token, s.internal_api_token]
    assert all(secret.get_secret_value() == "" for secret in secrets)
    assert s.vobiz_auth_id == "" and s.public_base_url == "" and s.vobiz_caller_id == ""


# =============================================================================================
# R3: track metadata
# =============================================================================================


def _media_json(track: str | None, **kw) -> str:
    msg = copy.deepcopy(media_msg(b"\x00\x01" * 320, **kw))
    if track is None:
        del msg["media"]["track"]
    else:
        msg["media"]["track"] = track
    return json.dumps(msg)


@pytest.mark.parametrize("track", ["inbound", "outbound", "both", "track_outbound", "anything-else"])
def test_r3_parser_passes_any_nonempty_track_string_through(track: str) -> None:
    event = parse_message(_media_json(track))
    assert isinstance(event, MediaEvent) and event.track == track


def test_r3_parser_does_not_validate_track_against_documented_values() -> None:
    assert parse_message(_media_json("not-a-documented-track")).track == "not-a-documented-track"


def test_r3_missing_track_is_a_protocol_error() -> None:
    with pytest.raises(StreamProtocolError):
        parse_message(_media_json(None))


def test_r3_empty_track_is_a_protocol_error() -> None:
    with pytest.raises(StreamProtocolError):
        parse_message(_media_json(""))


def test_r3_start_tracks_list_is_kept_as_is() -> None:
    msg = start_msg()
    msg["start"]["tracks"] = ["inbound", "outbound"]
    event = parse_message(json.dumps(msg))
    assert isinstance(event, StartEvent) and event.tracks == ("inbound", "outbound")


def test_r3_audio_frame_carries_the_track_and_tracks_are_not_separated(client: TestClient) -> None:
    with open_stream(client) as stream:
        session = stream.session
        for i, track in enumerate(["inbound", "outbound", "unknown-track", "inbound"], start=1):
            msg = copy.deepcopy(media_msg(bytes([i]) * 640, chunk=i, seq=i))
            msg["media"]["track"] = track
            stream.ws.send_json(msg)
        wait_until(lambda: session.stats.frames_received == 4)
        frames = list(session.queue._items)
        assert [f.track for f in frames] == ["inbound", "outbound", "unknown-track", "inbound"]
        # one queue, arrival order, all tracks interleaved: nothing routes by track today
        assert [f.chunk for f in frames] == [1, 2, 3, 4]
        assert all(isinstance(f, AudioFrame) and f.stream_id == STREAM for f in frames)
        assert session.stats.malformed == 0


def test_r3_media_without_track_is_counted_malformed_and_not_queued(client: TestClient) -> None:
    with open_stream(client) as stream:
        session = stream.session
        stream.ws.send_text(_media_json(None))
        wait_until(lambda: session.stats.malformed == 1)
        assert session.stats.frames_received == 0 and len(session.queue) == 0 and not session.closed
        stream.ws.send_json(media_msg(b"\x00" * 640))  # the stream keeps working afterwards
        wait_until(lambda: session.stats.frames_received == 1)


# =============================================================================================
# R7: a newer stream supersedes the previous one for the same call
# =============================================================================================


async def test_r7_newer_stream_supersedes_older_and_cleans_it_up() -> None:
    p, r = pipe(idle_close_s=0.05), Rec()
    p.add_global_subscriber(r, "r")
    mgr = CallStateManager()
    mgr.add_listener(PipelineCallListener(p))
    old, new = make_session("call-1", "stream-old"), make_session("call-1", "stream-new")

    await mgr.attach_stream(old)
    await p.publish(ev("call-1", 1, stream="stream-old"))
    state = await mgr.attach_stream(new)

    assert old.closed and old.end_reason == "superseded"
    assert old.queue.closed and old.sender.closed and old.playback.pending_checkpoints == frozenset()
    assert mgr.current_stream("call-1") is new and not new.closed
    assert state.streams_seen == 2 and state.reconnects == 1 and len(mgr) == 1

    # "superseded" must not schedule the transcript idle close: the call is still being served
    await asyncio.sleep(0.15)  # three times idle_close_s
    assert p.get_session("call-1") is not None and p.calls_opened == 1 and r.closed == []

    await mgr.on_hangup("call-1")
    assert new.end_reason == "hangup" and p.get_session("call-1") is None
    await asyncio.sleep(0.02)
    assert leftover_tasks() == []


# =============================================================================================
# R8: transcript idle close
# =============================================================================================


async def test_r8_session_stays_open_below_idle_timeout_then_closes_after_it() -> None:
    p, r = pipe(idle_close_s=0.4), Rec()
    p.add_global_subscriber(r, "r")
    listener = PipelineCallListener(p)

    await listener.on_stream_start(_Call())
    await p.publish(final(seq=0))
    await listener.on_stream_end(_Call(), "socket_closed")  # last stream gone, no hangup yet

    await asyncio.sleep(0.05)  # well below 0.4 s
    assert p.get_session("c1") is not None and r.closed == []

    await pipeline_settle(lambda: p.get_session("c1") is None, timeout=3.0)
    assert [c.reason for c in r.closed] == ["idle"] and r.seqs() == [0]


async def test_r8_events_after_the_idle_close_are_rejected_not_reopened() -> None:
    p, r = pipe(idle_close_s=0.03), Rec()
    p.add_global_subscriber(r, "r")
    listener = PipelineCallListener(p)
    await listener.on_stream_start(_Call())
    await listener.on_stream_end(_Call(), "socket_closed")
    await pipeline_settle(lambda: p.get_session("c1") is None)

    await p.publish(ev(seq=1, stream="s-late"))  # a monitor stream that arrives after the gap
    assert p.get_session("c1") is None and p.rejected_after_close == 1 and r.seqs() == []


async def test_r8_a_new_stream_inside_the_idle_window_cancels_the_close() -> None:
    p = pipe(idle_close_s=0.2)
    listener = PipelineCallListener(p)
    await listener.on_stream_start(_Call())
    await listener.on_stream_end(_Call(), "socket_closed")
    await asyncio.sleep(0.03)
    await listener.on_stream_start(_Call())  # e.g. a reconnect or a later stream
    await asyncio.sleep(0.3)
    assert p.get_session("c1") is not None and p.calls_opened == 1
    await p.close_call("c1")


# =============================================================================================
# R11: hangup de-duplication key
# =============================================================================================


def test_r11_deduper_matches_on_the_exact_key_only() -> None:
    d = EventDeduper()
    assert d.is_duplicate("call-1:Hangup") is False
    assert d.is_duplicate("call-1:Hangup") is True
    assert d.is_duplicate("call-1:DialHangup") is False  # a different Event name is a different key
    assert d.is_duplicate("call-2:Hangup") is False


def test_r11_hangup_key_is_call_uuid_plus_event_and_a_missing_event_means_hangup(client: TestClient) -> None:
    form = {"CallUUID": "r11-a", "Direction": "outbound"}  # no Event
    assert client.post(HANGUP, data=form).json()["duplicate"] is False
    assert client.post(HANGUP, data={**form, "Event": "Hangup"}).json()["duplicate"] is True
    assert client.post(HANGUP, data={**form, "Event": "DialHangup"}).json()["duplicate"] is False


def test_r11_limitation_leg_identifiers_are_not_part_of_the_key(client: TestClient) -> None:
    """Two payloads that differ only by a B-leg identifier collide today. Dial callbacks (which
    carry DialBLegUUID) therefore cannot reuse this key. Pins current behaviour; adds no new scheme."""
    base = {"CallUUID": "r11-b", "Direction": "outbound", "Event": "Hangup"}
    first = client.post(HANGUP, data={**base, "DialBLegUUID": "leg-b-1"})
    second = client.post(HANGUP, data={**base, "DialBLegUUID": "leg-b-2"})
    assert first.status_code == second.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True  # leg-blind


# =============================================================================================
# R17: sequence collision in the transcript pipeline
# =============================================================================================


async def test_r17_same_stream_id_and_sequence_is_a_duplicate_even_for_different_tracks() -> None:
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    # `speaker` is only a carrier for the neutral label here; the pipeline does not look at it
    await p.publish(ev(seq=1, stream="phys-1", speaker="track_inbound", text="a"))
    await p.publish(ev(seq=1, stream="phys-1", speaker="track_outbound", text="b"))  # other track, same key
    await pipeline_settle(lambda: len(r.events) >= 1)
    await asyncio.sleep(0.02)
    metrics = p.get_session("c1").metrics()
    assert metrics["duplicates"] == 1
    assert [e.speaker for e in r.events] == ["track_inbound"]  # the second track's event is dropped
    await p.close_call("c1")


async def test_r17_namespaced_stream_keys_do_not_collide_and_keep_m44_semantics() -> None:
    """The approved design uses `{physical_stream_id}:track_inbound|track_outbound` as the pipeline
    stream key. With unchanged M4.4 code that already isolates the sequences."""
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    for seq in (1, 2, 3):
        await p.publish(ev(seq=seq, stream="phys-1:track_inbound", speaker="track_inbound"))
        await p.publish(ev(seq=seq, stream="phys-1:track_outbound", speaker="track_outbound"))
    await pipeline_settle(lambda: len(r.events) == 6)
    m = p.get_session("c1").metrics()
    assert m["duplicates"] == 0 and m["sequence_anomalies"] == 0 and m["streams"] == 2
    for track in ("track_inbound", "track_outbound"):
        assert [e.sequence for e in r.events if e.speaker == track] == [1, 2, 3]  # order kept per track

    await p.publish(ev(seq=2, stream="phys-1:track_inbound", speaker="track_inbound"))  # real duplicate
    await asyncio.sleep(0.02)
    assert p.get_session("c1").metrics()["duplicates"] == 1  # duplicate detection itself is unchanged
    await p.close_call("c1")


# =============================================================================================
# R18: speaker default
# =============================================================================================


def test_r18_transcript_event_speaker_defaults_to_customer() -> None:
    field = {f.name: f for f in dataclasses.fields(TranscriptEvent)}["speaker"]
    assert field.default == "customer"
    event = TranscriptEvent(call_id="c", stream_id="s", sequence=1, type="transcript.final", received_at="t")
    assert event.speaker == "customer"


def test_r18_sarvam_sink_never_passes_speaker_to_transcript_event() -> None:
    tree = ast.parse(inspect.getsource(sarvam_stt_sink))
    constructions = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "TranscriptEvent"
    ]
    assert constructions, "expected the sink to build TranscriptEvent objects"
    assert all("speaker" not in {kw.arg for kw in node.keywords} for node in constructions)


async def test_r18_events_from_outbound_track_frames_still_carry_the_default_speaker() -> None:
    fake, pub = FakeSarvam(), Collector()
    session = make_session("call-18", "stream-18")
    await start(session, factory_for(fake, pub))
    for i in range(1, 11):
        session.queue.put(AudioFrame(
            data=bytes([i, 1]) * 320, format=session.audio_format, track="outbound", timestamp_ms=i * 20,
            chunk=i, sequence=i, stream_id=session.stream_id, received_at=time.monotonic(),
        ))
        session.stats.frames_received += 1
    await stt_settle(lambda: any(e.is_partial for e in pub.events))
    await session.close("socket_closed")

    assert pub.events and any(e.is_final for e in pub.events)
    assert {e.speaker for e in pub.events} == {"customer"}  # the track is not reflected anywhere
    assert {e.stream_id for e in pub.events} == {"stream-18"}  # and the key has no track component


# =============================================================================================
# hangup idempotency
# =============================================================================================


async def test_hangup_is_idempotent_and_transcript_cleanup_stays_consistent() -> None:
    p, r = pipe(), Rec()
    p.add_global_subscriber(r, "r")
    mgr = CallStateManager()
    mgr.add_listener(PipelineCallListener(p))
    session = make_session("call-h", "stream-h")
    await mgr.attach_stream(session)
    await p.publish(ev("call-h", 1, stream="stream-h"))

    assert await mgr.on_hangup("call-h") is True  # first: there was state to clean up
    assert await mgr.on_hangup("call-h") is False  # repeat: nothing left, no error
    assert await mgr.on_hangup("never-seen") is False  # unknown call: harmless

    assert session.end_reason == "hangup" and session.queue.closed and session.sender.closed
    assert mgr.is_ended("call-h") and mgr.get("call-h") is None and mgr.current_stream("call-h") is None
    assert p.get_session("call-h") is None and p.calls_closed == 1  # closed exactly once
    assert [c.reason for c in r.closed] == ["hangup"] and r.seqs("call-h") == [1]

    await p.publish(ev("call-h", 2, stream="stream-h"))  # a late event cannot reopen the call
    assert p.get_session("call-h") is None and p.rejected_after_close == 1
    await asyncio.sleep(0.02)
    assert leftover_tasks() == []


def test_hangup_webhook_twice_closes_stream_and_transcript_session_once(client: TestClient) -> None:
    form = {"CallUUID": CALL, "Direction": "outbound", "Event": "Hangup", "CallStatus": "completed"}
    with open_stream(client) as stream:
        session = stream.session
        wait_until(lambda: client.app.state.transcript_pipeline.get_session(CALL) is not None)

        first = client.post(HANGUP, data=form).json()
        second = client.post(HANGUP, data=form).json()
        assert (first["duplicate"], second["duplicate"]) == (False, True)

        assert session.closed and session.end_reason == "hangup"
        snapshot = client.app.state.transcript_pipeline.snapshot()
        assert snapshot["calls_closed"] == 1 and snapshot["calls_active"] == 0
        with pytest.raises(WebSocketDisconnect) as info:  # the server closes our socket
            recv_json(stream.ws)
        assert info.value.code == 1000
    wait_until(lambda: client.app.state.stream_handler.connection_count == 0)
    assert client.app.state.call_manager.is_ended(CALL)
