"""Milestone 3: event parsing, outgoing commands, checkpoints, clearAudio, call state.

All sockets are fakes. These are mocked tests: no real Vobiz connection is involved.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os

import pytest

from app.integrations.vobiz.stream_events import (
    ClearedAudioEvent,
    DtmfEvent,
    IncorrectPayloadEvent,
    MediaEvent,
    PlayedStreamEvent,
    StartEvent,
    StreamProtocolError,
    UnknownEvent,
    UnsupportedStreamFormat,
    parse_message,
    token_from_extra_headers,
)
from app.integrations.vobiz.stream_sender import (
    StreamClosedError,
    VobizStreamSender,
    build_checkpoint,
    build_clear_audio,
    build_play_audio,
    build_stop,
    split_chunks,
)
from app.services.audio.audio_buffer import BoundedFrameQueue
from app.services.audio.formats import ENC_L16, ENC_MULAW, AudioFormat, AudioFormatError
from app.services.voice.call_manager import CallEndedError, CallStateManager, StreamSession
from app.services.voice.playback import (
    CLEARED,
    CLOSED,
    PLAYED,
    TIMEOUT,
    CheckpointLimitError,
    CheckpointTracker,
    PlaybackController,
    PlaybackState,
)

L16_8K = AudioFormat(ENC_L16, 8000)
L16_16K = AudioFormat(ENC_L16, 16000)
L16_24K = AudioFormat(ENC_L16, 24000)
MULAW_8K = AudioFormat(ENC_MULAW, 8000)


def msg(**fields) -> str:
    return json.dumps(fields)


START = {
    "sequenceNumber": 0,
    "event": "start",
    "start": {
        "callId": "5401fd2e-6344-40df-a22c-c8ffea7a92e7",
        "streamId": "c4dfd815-a92a-4140-ab85-5ff28c004116",
        "accountId": "500025",
        "tracks": ["inbound"],
        "mediaFormat": {"encoding": "audio/x-l16", "sampleRate": 16000},
    },
    "extra_headers": "{}",
}
MEDIA = {
    "sequenceNumber": 2,
    "streamId": "c4dfd815-a92a-4140-ab85-5ff28c004116",
    "event": "media",
    "media": {"track": "inbound", "timestamp": "1778597597091", "chunk": 2, "payload": "AAAA"},
    "extra_headers": "{}",
}


# --- incoming event parsing ------------------------------------------------------------------


def test_parse_documented_start_event() -> None:
    event = parse_message(json.dumps(START))
    assert isinstance(event, StartEvent)
    assert event.call_id == "5401fd2e-6344-40df-a22c-c8ffea7a92e7"
    assert event.stream_id == "c4dfd815-a92a-4140-ab85-5ff28c004116"
    assert event.account_id == "500025"
    assert event.tracks == ("inbound",)
    assert event.media_format == L16_16K  # encoding and sample rate preserved as announced
    assert event.sequence_number == 0
    assert event.extra_headers == "{}"


@pytest.mark.parametrize(
    ("encoding", "rate"), [("audio/x-l16", 8000), ("audio/x-l16", 16000), ("audio/x-mulaw", 8000)]
)
def test_start_event_supported_formats(encoding: str, rate: int) -> None:
    data = json.loads(json.dumps(START))
    data["start"]["mediaFormat"] = {"encoding": encoding, "sampleRate": rate}
    fmt = parse_message(json.dumps(data)).media_format
    assert (fmt.encoding, fmt.sample_rate) == (encoding, rate)


@pytest.mark.parametrize(
    "media_format",
    [
        {"encoding": "audio/wav", "sampleRate": 8000},
        {"encoding": "audio/x-l16", "sampleRate": 24000},
        {"encoding": "audio/x-mulaw", "sampleRate": 16000},
        {"encoding": "audio/x-l16"},
        {"sampleRate": 8000},
        {},
    ],
)
def test_start_event_unsupported_format(media_format: dict) -> None:
    data = json.loads(json.dumps(START))
    data["start"]["mediaFormat"] = media_format
    with pytest.raises(UnsupportedStreamFormat):
        parse_message(json.dumps(data))


def test_parse_documented_media_event() -> None:
    event = parse_message(json.dumps(MEDIA))
    assert isinstance(event, MediaEvent)
    assert event.stream_id == "c4dfd815-a92a-4140-ab85-5ff28c004116"
    assert event.track == "inbound"
    assert event.timestamp_ms == 1778597597091  # arrives as a string
    assert event.chunk == 2 and event.sequence_number == 2
    assert event.payload == "AAAA"
    assert "AAAA" not in repr(event)  # audio is kept out of reprs/logs


def test_media_timestamp_may_be_a_number() -> None:
    data = json.loads(json.dumps(MEDIA))
    data["media"]["timestamp"] = 1778597597091
    assert parse_message(json.dumps(data)).timestamp_ms == 1778597597091


def test_played_stream_has_no_stream_id() -> None:
    event = parse_message(msg(event="playedStream", name="cp-1-abcd"))
    assert event == PlayedStreamEvent(name="cp-1-abcd")


def test_cleared_audio_with_and_without_stream_id() -> None:
    assert parse_message(msg(event="clearedAudio", sequenceNumber=0, streamId="s1")) == ClearedAudioEvent("s1", 0)
    assert parse_message(msg(event="clearedAudio")) == ClearedAudioEvent(None, None)


def test_dtmf_digit_is_not_exposed_in_repr() -> None:
    event = parse_message(
        msg(event="dtmf", streamId="s1", dtmf={"track": "inbound", "digit": "5", "timestamp": "1763495178506"})
    )
    assert isinstance(event, DtmfEvent)
    assert (event.digit, event.track, event.timestamp_ms, event.stream_id) == ("5", "inbound", 1763495178506, "s1")
    assert "5" not in repr(event).replace("1763495178506", "").replace("inbound", "")


def test_incorrect_payload_and_unknown_events() -> None:
    event = parse_message(msg(event="incorrectPayload", streamId="s1", payload="x" * 40))
    assert event == IncorrectPayloadEvent("s1", 40)
    assert parse_message(msg(event="somethingNew")) == UnknownEvent("somethingNew")


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "",
        "[]",
        "42",
        "null",
        "{}",
        '{"event": 5}',
        '{"event": ""}',
        "[" * 5000,
        msg(event="start"),
        msg(event="start", start="x"),
        msg(event="start", start={"streamId": "s", "mediaFormat": {"encoding": "audio/x-l16", "sampleRate": 8000}}),
        msg(event="start", start={"callId": "c", "mediaFormat": {"encoding": "audio/x-l16", "sampleRate": 8000}}),
        msg(event="start", start={"callId": "c a", "streamId": "s", "mediaFormat": {"encoding": "audio/x-l16", "sampleRate": 8000}}),
        msg(event="start", start={"callId": "c", "streamId": "s", "tracks": "inbound", "mediaFormat": {"encoding": "audio/x-l16", "sampleRate": 8000}}),
        msg(event="media"),
        msg(event="media", media={"track": "inbound", "timestamp": "1", "chunk": 1, "payload": "AA"}),
        msg(event="media", streamId="s", media={"timestamp": "1", "chunk": 1, "payload": "AA"}),
        msg(event="media", streamId="s", media={"track": "inbound", "timestamp": "abc", "chunk": 1, "payload": "AA"}),
        msg(event="media", streamId="s", media={"track": "inbound", "timestamp": "1", "chunk": -1, "payload": "AA"}),
        msg(event="media", streamId="s", media={"track": "inbound", "timestamp": "1", "chunk": True, "payload": "AA"}),
        msg(event="media", streamId="s", media="x"),
        msg(event="playedStream"),
        msg(event="playedStream", name=""),
        msg(event="playedStream", name=5),
        msg(event="clearedAudio", streamId=5),
    ],
)
def test_malformed_messages_raise_protocol_error_only(text: str) -> None:
    with pytest.raises(StreamProtocolError):
        parse_message(text)


def test_media_payload_that_is_not_a_string_parses_but_fails_decoding_later() -> None:
    data = json.loads(json.dumps(MEDIA))
    data["media"]["payload"] = 12345
    assert parse_message(json.dumps(data)).payload == ""  # decode_media_payload rejects the empty payload


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("{X-VH-token: abc123}", "abc123"),
        ("{X-VH-agentType: sales, X-VH-token: ff00ee}", "ff00ee"),
        ('{"token": "abc123"}', "abc123"),
        ('{"X-VH-token": "abc123"}', "abc123"),
        ("token=abc123", "abc123"),
        ("{}", None),
        ("{X-VH-agentType: sales}", None),
        ("", None),
        (None, None),
    ],
)
def test_token_from_extra_headers(raw, expected) -> None:
    assert token_from_extra_headers(raw) == expected


# --- outgoing messages -----------------------------------------------------------------------


def test_message_shapes_match_vobiz_docs() -> None:
    assert build_play_audio("s1", L16_16K, b"\x01\x02") == {
        "event": "playAudio",
        "streamId": "s1",
        "media": {"contentType": "audio/x-l16", "sampleRate": 16000, "payload": "AQI="},
    }
    assert build_checkpoint("s1", "cp-1") == {"event": "checkpoint", "streamId": "s1", "name": "cp-1"}
    assert build_clear_audio("s1") == {"event": "clearAudio", "streamId": "s1"}
    assert build_stop("s1") == {"event": "stop", "streamId": "s1"}


@pytest.mark.parametrize(
    ("fmt", "chunk_ms", "expected"),
    [(L16_8K, 20, 320), (L16_16K, 20, 640), (L16_24K, 20, 960), (MULAW_8K, 20, 160),
     (L16_8K, 40, 640), (L16_16K, 60, 1920), (MULAW_8K, 60, 480)],
)
def test_chunk_sizes(fmt: AudioFormat, chunk_ms: int, expected: int) -> None:
    chunks = split_chunks(os.urandom(expected * 3 + fmt.bytes_per_sample), fmt, chunk_ms)
    assert [len(c) for c in chunks] == [expected] * 3 + [fmt.bytes_per_sample]


@pytest.mark.parametrize("bad", [0, 10, 19, 61, 100, -20, True, 20.0, "20"])
def test_chunk_ms_must_be_in_documented_range(bad) -> None:
    with pytest.raises(ValueError):
        VobizStreamSender("s1", FakeSocket(), chunk_ms=bad)
    with pytest.raises(ValueError):
        split_chunks(b"\x00\x00", L16_8K, bad)


class FakeSocket:
    """Records outgoing text frames and the maximum number of concurrent writers."""

    def __init__(self, *, fail_on_call: int | None = None, yield_inside: bool = True) -> None:
        self.sent: list[dict] = []
        self.raw: list[str] = []
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self._fail_on_call = fail_on_call
        self._yield = yield_inside

    async def __call__(self, text: str) -> None:
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self._yield:
                await asyncio.sleep(0)
            if self._fail_on_call is not None and self.calls >= self._fail_on_call:
                raise ConnectionResetError("socket gone")
            self.raw.append(text)
            self.sent.append(json.loads(text))
        finally:
            self.in_flight -= 1

    def of(self, event: str) -> list[dict]:
        return [m for m in self.sent if m["event"] == event]


async def test_play_audio_sends_valid_base64_chunks_and_reassembles_exactly() -> None:
    sock = FakeSocket()
    sender = VobizStreamSender("s1", sock, chunk_ms=20)
    audio = os.urandom(640 * 5 + 100)

    result = await sender.play_audio(audio, L16_16K)

    assert (result.chunks_sent, result.bytes_sent, result.aborted) == (6, len(audio), False)
    plays = sock.of("playAudio")
    assert all(p["streamId"] == "s1" for p in plays)
    assert all(p["media"]["contentType"] == "audio/x-l16" and p["media"]["sampleRate"] == 16000 for p in plays)
    decoded = [base64.b64decode(p["media"]["payload"], validate=True) for p in plays]
    assert [len(d) for d in decoded] == [640] * 5 + [100]
    assert b"".join(decoded) == audio  # base64 round trip, no header, no change


async def test_play_audio_declares_the_real_format_per_call() -> None:
    sock = FakeSocket()
    sender = VobizStreamSender("s1", sock)
    await sender.play_audio(b"\xff" * 160, MULAW_8K)
    await sender.play_audio(b"\x00" * 960, L16_24K)
    first, second = sock.of("playAudio")
    assert first["media"]["contentType"] == "audio/x-mulaw" and first["media"]["sampleRate"] == 8000
    assert second["media"]["contentType"] == "audio/x-l16" and second["media"]["sampleRate"] == 24000


async def test_play_audio_uses_configured_chunk_size() -> None:
    sock = FakeSocket()
    sender = VobizStreamSender("s1", sock, chunk_ms=40)
    await sender.play_audio(b"\x00" * 1280, L16_16K)
    assert [len(base64.b64decode(p["media"]["payload"])) for p in sock.of("playAudio")] == [1280]
    await sender.play_audio(b"\x00" * 640, L16_16K, chunk_ms=20)
    assert len(sock.of("playAudio")) == 2


@pytest.mark.parametrize(
    "audio",
    [
        b"",
        b"\x00\x00\x00",  # odd length for 16-bit
        b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 64,  # WAV container
        b"ID3\x04" + b"\x00" * 64,
    ],
)
async def test_play_audio_rejects_invalid_raw_audio_without_sending(audio: bytes) -> None:
    sock = FakeSocket()
    with pytest.raises(AudioFormatError):
        await VobizStreamSender("s1", sock).play_audio(audio, L16_8K)
    assert sock.calls == 0


async def test_play_audio_rejects_unsupported_format_and_oversize() -> None:
    sock = FakeSocket()
    sender = VobizStreamSender("s1", sock, max_audio_bytes=1000)
    with pytest.raises(AudioFormatError):
        await sender.play_audio(b"\xff" * 160, AudioFormat(ENC_MULAW, 16000))
    with pytest.raises(AudioFormatError):
        await sender.play_audio(b"\xff" * 1001, MULAW_8K)
    assert sock.calls == 0


async def test_send_failure_closes_the_sender() -> None:
    sock = FakeSocket(fail_on_call=2)
    sender = VobizStreamSender("s1", sock)
    with pytest.raises(StreamClosedError):
        await sender.play_audio(b"\x00" * 640, L16_8K)  # 2 chunks; the second send fails
    assert sender.closed
    calls_before = sock.calls
    with pytest.raises(StreamClosedError):
        await sender.checkpoint("cp")
    assert sock.calls == calls_before  # a closed sender never touches the socket again


async def test_close_blocks_all_commands() -> None:
    sock = FakeSocket()
    sender = VobizStreamSender("s1", sock)
    sender.close()
    for coro in (sender.play_audio(b"\x00" * 320, L16_8K), sender.checkpoint("c"), sender.clear_audio(), sender.stop()):
        with pytest.raises(StreamClosedError):
            await coro
    assert sock.calls == 0


async def test_stop_and_clear_messages() -> None:
    sock = FakeSocket()
    sender = VobizStreamSender("s9", sock)
    await sender.clear_audio()
    await sender.stop()
    assert sock.sent == [{"event": "clearAudio", "streamId": "s9"}, {"event": "stop", "streamId": "s9"}]
    assert all(" " not in raw for raw in sock.raw)  # compact JSON


# --- concurrency -----------------------------------------------------------------------------


async def test_concurrent_senders_never_interleave_writes_or_utterances() -> None:
    sock = FakeSocket()
    sender = VobizStreamSender("s1", sock)
    a, b = b"\xaa" * 160 * 8, b"\xbb" * 160 * 8  # 8 chunks each

    await asyncio.gather(
        sender.play_audio(a, MULAW_8K),
        sender.play_audio(b, MULAW_8K),
        sender.checkpoint("cp-x"),
        sender.checkpoint("cp-y"),
        *[sender.checkpoint(f"cp-{i}") for i in range(20)],
    )

    assert sock.max_in_flight == 1  # one write at a time
    assert len(sock.sent) == 16 + 22
    assert all(isinstance(json.loads(raw), dict) for raw in sock.raw)  # every frame is intact JSON
    plays = [base64.b64decode(m["media"]["payload"])[:1] for m in sock.sent if m["event"] == "playAudio"]
    # Each utterance's chunks are contiguous: the sequence is AAAAAAAABBBBBBBB or the reverse.
    assert plays in ([b"\xaa"] * 8 + [b"\xbb"] * 8, [b"\xbb"] * 8 + [b"\xaa"] * 8)


async def test_clear_audio_stops_an_utterance_mid_send() -> None:
    sock = FakeSocket()
    sender = VobizStreamSender("s1", sock)
    playing = asyncio.create_task(sender.play_audio(b"\x00" * 160 * 500, MULAW_8K))
    for _ in range(10):
        await asyncio.sleep(0)
    await sender.clear_audio()
    result = await playing

    assert result.aborted and 0 < result.chunks_sent < 500
    events = [m["event"] for m in sock.sent]
    clear_at = events.index("clearAudio")
    assert "playAudio" not in events[clear_at + 1 :]  # nothing is sent after the clear


async def test_clear_audio_cancels_utterances_still_waiting_their_turn() -> None:
    sock = FakeSocket()
    sender = VobizStreamSender("s1", sock)
    first = asyncio.create_task(sender.play_audio(b"\x00" * 160 * 300, MULAW_8K))
    second = asyncio.create_task(sender.play_audio(b"\x11" * 160 * 50, MULAW_8K))
    for _ in range(5):
        await asyncio.sleep(0)
    await sender.clear_audio()
    r1, r2 = await asyncio.gather(first, second)
    assert r1.aborted and r2.aborted and r2.chunks_sent == 0
    third = await sender.play_audio(b"\x22" * 160, MULAW_8K)  # a fresh utterance after the clear is fine
    assert not third.aborted and third.chunks_sent == 1


# --- checkpoints -----------------------------------------------------------------------------


def make_controller(sock: FakeSocket | None = None, **kw) -> tuple[PlaybackController, FakeSocket]:
    sock = sock or FakeSocket()
    sender = VobizStreamSender("s1", sock)
    return PlaybackController(sender, **kw), sock


async def test_checkpoint_follows_audio_and_resolves_on_played_stream() -> None:
    ctl, sock = make_controller()
    result = await ctl.play(b"\xff" * 320, MULAW_8K)

    events = [m["event"] for m in sock.sent]
    assert events == ["playAudio", "playAudio", "checkpoint"]  # checkpoint right after the last chunk
    name = sock.of("checkpoint")[0]["name"]
    assert result.checkpoint == name and result.outcome == "not_waited"
    assert ctl.pending_checkpoints == {name} and ctl.state is PlaybackState.PLAYING

    assert ctl.handle_played_stream(name) is True  # ack carries only the name
    assert ctl.pending_checkpoints == frozenset() and ctl.state is PlaybackState.IDLE


async def test_checkpoint_names_are_unique() -> None:
    ctl, sock = make_controller()
    for _ in range(5):
        await ctl.play(b"\xff" * 160, MULAW_8K)
    names = [m["name"] for m in sock.of("checkpoint")]
    assert len(set(names)) == 5 and all(n.startswith("cp-") for n in names)


async def test_waiting_for_playback_resolves_when_acknowledged() -> None:
    ctl, sock = make_controller()

    async def ack_when_sent() -> None:
        while not sock.of("checkpoint"):
            await asyncio.sleep(0.001)
        ctl.handle_played_stream(sock.of("checkpoint")[0]["name"])

    acker = asyncio.create_task(ack_when_sent())
    result = await asyncio.wait_for(ctl.play(b"\xff" * 160, MULAW_8K, wait=True, timeout_s=2), 3)
    await acker
    assert result.outcome == PLAYED and ctl.pending_checkpoints == frozenset()


async def test_checkpoint_timeout_when_no_ack_arrives() -> None:
    ctl, _ = make_controller(checkpoint_timeout_s=30)
    result = await ctl.play(b"\xff" * 160, MULAW_8K, wait=True, timeout_s=0.05)
    assert result.outcome == TIMEOUT
    assert ctl.pending_checkpoints == frozenset()  # cleaned up, not leaked
    assert ctl.state is PlaybackState.IDLE


async def test_unwaited_checkpoints_expire_on_their_own_timer() -> None:
    ctl, sock = make_controller(checkpoint_timeout_s=0.05)
    await ctl.play(b"\xff" * 160, MULAW_8K)
    assert len(ctl.pending_checkpoints) == 1
    await asyncio.sleep(0.12)
    assert ctl.pending_checkpoints == frozenset()
    late = sock.of("checkpoint")[0]["name"]
    assert ctl.handle_played_stream(late) is False  # a late ack is ignored, not an error


async def test_played_stream_for_unknown_name_is_ignored() -> None:
    ctl, _ = make_controller()
    assert ctl.handle_played_stream("never-sent") is False


async def test_checkpoints_cannot_cross_streams() -> None:
    ctl_a, sock_a = make_controller()
    ctl_b, sock_b = make_controller()
    await ctl_a.play(b"\xff" * 160, MULAW_8K)
    await ctl_b.play(b"\xff" * 160, MULAW_8K)
    name_a = sock_a.of("checkpoint")[0]["name"]
    name_b = sock_b.of("checkpoint")[0]["name"]

    assert name_a != name_b  # random suffix: names do not collide across streams either
    assert ctl_b.handle_played_stream(name_a) is False
    assert ctl_b.pending_checkpoints == {name_b}  # B is untouched by A's ack
    assert ctl_a.handle_played_stream(name_a) is True


async def test_checkpoint_limit() -> None:
    tracker = CheckpointTracker(timeout_s=30, max_pending=2)
    tracker.create()
    tracker.create()
    with pytest.raises(CheckpointLimitError):
        tracker.create()
    tracker.close()


async def test_close_resolves_waiters_and_releases_checkpoints() -> None:
    ctl, sock = make_controller()
    waiting = asyncio.create_task(ctl.play(b"\xff" * 160, MULAW_8K, wait=True, timeout_s=5))
    while not sock.of("checkpoint"):
        await asyncio.sleep(0.001)
    ctl.close()
    assert (await asyncio.wait_for(waiting, 1)).outcome == CLOSED
    assert ctl.pending_checkpoints == frozenset() and ctl.state is PlaybackState.IDLE
    ctl.close()  # idempotent
    with pytest.raises(StreamClosedError):
        await ctl.play(b"\xff" * 160, MULAW_8K)


# --- clearAudio / clearedAudio ---------------------------------------------------------------


async def test_clear_voids_pending_checkpoints_and_waits_for_confirmation() -> None:
    ctl, sock = make_controller()
    await ctl.play(b"\xff" * 160, MULAW_8K)
    assert ctl.state is PlaybackState.PLAYING

    clearing = asyncio.create_task(ctl.clear(wait_confirmed=True))
    await asyncio.sleep(0.01)
    assert sock.of("clearAudio") == [{"event": "clearAudio", "streamId": "s1"}]
    assert ctl.state is PlaybackState.CLEARING
    assert ctl.pending_checkpoints == frozenset()  # voided: Vobiz will never ack them
    assert not clearing.done()

    ctl.handle_cleared_audio()  # clearedAudio arrives
    assert await asyncio.wait_for(clearing, 1) is True
    assert ctl.state is PlaybackState.IDLE


async def test_waiter_on_voided_checkpoint_is_told_cleared() -> None:
    ctl, sock = make_controller()
    waiting = asyncio.create_task(ctl.play(b"\xff" * 160, MULAW_8K, wait=True, timeout_s=5))
    while not sock.of("checkpoint"):
        await asyncio.sleep(0.001)
    await ctl.clear(wait_confirmed=False)
    assert (await asyncio.wait_for(waiting, 1)).outcome == CLEARED


async def test_clear_without_confirmation_times_out_cleanly() -> None:
    ctl, _ = make_controller(clear_timeout_s=0.05)
    assert await ctl.clear(wait_confirmed=True) is False
    assert ctl.state is PlaybackState.CLEARING  # still waiting for the ack


async def test_new_playback_waits_for_cleared_audio_after_a_clear() -> None:
    ctl, sock = make_controller(clear_timeout_s=2)
    await ctl.clear(wait_confirmed=False)
    replacement = asyncio.create_task(ctl.play(b"\x11" * 160, MULAW_8K))
    await asyncio.sleep(0.05)
    assert sock.of("playAudio") == []  # held back until clearedAudio, per the Vobiz guidance
    ctl.handle_cleared_audio()
    await asyncio.wait_for(replacement, 1)
    assert len(sock.of("playAudio")) == 1
    events = [m["event"] for m in sock.sent]
    assert events.index("clearAudio") < events.index("playAudio")


async def test_playback_proceeds_if_cleared_audio_never_arrives() -> None:
    ctl, sock = make_controller(clear_timeout_s=0.05)
    await ctl.clear(wait_confirmed=False)
    await asyncio.wait_for(ctl.play(b"\x11" * 160, MULAW_8K), 1)
    assert len(sock.of("playAudio")) == 1


async def test_unsolicited_cleared_audio_is_harmless() -> None:
    ctl, _ = make_controller()
    ctl.handle_cleared_audio()
    assert ctl.state is PlaybackState.IDLE


async def test_audio_is_never_sent_after_close() -> None:
    ctl, sock = make_controller()
    ctl.close()
    for coro in (ctl.play(b"\xff" * 160, MULAW_8K), ctl.clear()):
        with pytest.raises(StreamClosedError):
            await coro
    assert sock.calls == 0


async def test_clear_during_long_utterance_aborts_it() -> None:
    ctl, sock = make_controller(clear_timeout_s=0.05)
    playing = asyncio.create_task(ctl.play(b"\x00" * 160 * 400, MULAW_8K))
    for _ in range(10):
        await asyncio.sleep(0)
    await ctl.clear(wait_confirmed=False)
    result = await playing
    assert result.aborted and result.outcome == CLEARED and result.checkpoint is None
    assert sock.of("checkpoint") == []  # no checkpoint for an interrupted utterance


# --- call state manager ----------------------------------------------------------------------


def make_session(call_id: str = "call-1", stream_id: str = "stream-1", fmt: AudioFormat = L16_16K) -> StreamSession:
    sender = VobizStreamSender(stream_id, FakeSocket())
    closed_with: list[tuple[int, str]] = []

    async def closer(code: int, reason: str) -> None:
        closed_with.append((code, reason))

    session = StreamSession(
        call_id=call_id, stream_id=stream_id, account_id="1", tracks=("inbound",), audio_format=fmt,
        queue=BoundedFrameQueue(10), sender=sender, playback=PlaybackController(sender), socket_closer=closer,
    )
    session.closed_with = closed_with  # type: ignore[attr-defined]
    return session


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str | None]] = []

    async def on_stream_start(self, session) -> None:
        self.events.append(("start", session.stream_id, None))

    async def on_stream_end(self, session, reason) -> None:
        self.events.append(("end", session.stream_id, reason))

    async def on_dtmf(self, session, event) -> None:
        self.events.append(("dtmf", session.stream_id, None))


async def test_attach_creates_one_state_per_call() -> None:
    mgr = CallStateManager()
    s1 = make_session()
    call = await mgr.attach_stream(s1)
    assert len(mgr) == 1 and mgr.current_stream("call-1") is s1 and call.streams_seen == 1
    await mgr.attach_stream(make_session("call-2", "stream-2"))
    assert len(mgr) == 2


async def test_reconnect_with_new_stream_id_replaces_the_old_stream() -> None:
    mgr, rec = CallStateManager(), Recorder()
    mgr.add_listener(rec)
    old = make_session(stream_id="stream-old")
    await mgr.attach_stream(old)
    await old.playback.play(b"\xff" * 160, MULAW_8K)  # leaves a pending checkpoint behind

    new = make_session(stream_id="stream-new")
    call = await mgr.attach_stream(new)

    assert len(mgr) == 1  # no duplicate call state
    assert mgr.current_stream("call-1") is new
    assert call.streams_seen == 2 and call.reconnects == 1
    assert old.closed and old.end_reason == "superseded"
    assert old.queue.closed and old.playback.pending_checkpoints == frozenset()  # stream-level cleanup
    assert old.closed_with == [(1000, "superseded")]  # old socket told to close
    assert not new.closed
    assert rec.events == [("start", "stream-old", None), ("end", "stream-old", "superseded"), ("start", "stream-new", None)]


async def test_stale_stream_cleanup_does_not_remove_the_new_stream() -> None:
    mgr = CallStateManager()
    old, new = make_session(stream_id="a"), make_session(stream_id="b")
    await mgr.attach_stream(old)
    await mgr.attach_stream(new)
    await mgr.detach_stream(old, "socket_closed")  # the old socket's cleanup runs late
    assert mgr.current_stream("call-1") is new and not new.closed


async def test_detach_after_socket_close_is_idempotent_and_notifies_once() -> None:
    mgr, rec = CallStateManager(), Recorder()
    mgr.add_listener(rec)
    session = make_session()
    await mgr.attach_stream(session)
    await mgr.detach_stream(session, "socket_closed")
    await mgr.detach_stream(session, "socket_closed")
    assert session.closed and session.queue.closed
    assert mgr.current_stream("call-1") is None
    assert [e for e in rec.events if e[0] == "end"] == [("end", "stream-1", "socket_closed")]


async def test_hangup_closes_the_open_stream_and_later_socket_close_is_safe() -> None:
    mgr, rec = CallStateManager(), Recorder()
    mgr.add_listener(rec)
    session = make_session()
    await mgr.attach_stream(session)

    assert await mgr.on_hangup("call-1") is True  # hangup arrives BEFORE the socket closes
    assert session.closed and session.end_reason == "hangup"
    assert session.closed_with == [(1000, "hangup")]
    assert len(mgr) == 0 and mgr.current_stream("call-1") is None

    await mgr.detach_stream(session, "socket_closed")  # the socket's own cleanup, afterwards
    assert session.end_reason == "hangup"  # first reason wins
    assert [e for e in rec.events if e[0] == "end"] == [("end", "stream-1", "hangup")]
    assert await mgr.on_hangup("call-1") is False  # a retried webhook is a no-op


async def test_hangup_for_unknown_call_and_call_without_stream() -> None:
    mgr = CallStateManager()
    assert await mgr.on_hangup("never-seen") is False
    session = make_session()
    await mgr.attach_stream(session)
    await mgr.detach_stream(session, "socket_closed")
    assert await mgr.on_hangup("call-1") is True
    assert len(mgr) == 0


async def test_streams_cannot_attach_after_hangup() -> None:
    mgr = CallStateManager()
    await mgr.on_hangup("call-1")
    assert mgr.is_ended("call-1")
    with pytest.raises(CallEndedError):
        await mgr.attach_stream(make_session())
    assert len(mgr) == 0


async def test_ended_tombstones_expire_and_are_bounded() -> None:
    now = [0.0]
    mgr = CallStateManager(ended_ttl_s=10, max_ended=3, clock=lambda: now[0])
    for i in range(10):
        await mgr.on_hangup(f"c{i}")
    assert len(mgr._ended) <= 3
    now[0] = 100
    assert not mgr.is_ended("c9")


async def test_idle_calls_without_streams_are_forgotten() -> None:
    now = [0.0]
    mgr = CallStateManager(idle_ttl_s=60, clock=lambda: now[0])
    s = make_session("idle-call")
    await mgr.attach_stream(s)
    await mgr.detach_stream(s, "socket_closed")
    assert mgr.get("idle-call") is not None  # kept briefly so a reconnect finds its call
    now[0] = 120
    await mgr.attach_stream(make_session("other", "o1"))
    assert mgr.get("idle-call") is None


async def test_failing_listener_does_not_break_lifecycle() -> None:
    class Boom(Recorder):
        async def on_stream_start(self, session) -> None:
            raise RuntimeError("listener bug")

    mgr = CallStateManager()
    mgr.add_listener(Boom())
    await mgr.attach_stream(make_session())
    assert mgr.current_stream("call-1") is not None
