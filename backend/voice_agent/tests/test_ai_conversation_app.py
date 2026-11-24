"""AI voice conversation wired into the real app: Vobiz WebSocket (TestClient) + transcript pipeline, with a
fake LLM, fake TTS and a null audio sink standing in for STT. No provider is contacted."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.main import create_app
from app.services.conversation.prompts import GREETING_TEXT
from app.services.llm.base import LlmConfigError
from app.services.sarvam.errors import SarvamConfigError
from tests.ai_fakes import FakeLlm, FakeTts, PCM_BYTES, final, marker_for, speech_start
from tests.test_vobiz_websocket import CALL, HEADERS, STREAM, make_settings, media_msg, open_stream, recv_json, wait_until

ARN = "arn:aws:bedrock:ap-south-1:123456789012:application-inference-profile/abc123xyz"


class NullSink:
    def __init__(self) -> None:
        self.frames = 0

    async def handle_frame(self, frame) -> None:
        self.frames += 1

    async def close(self, reason: str) -> None:
        return None


SINKS: list[NullSink] = []


async def null_factory(session):
    sink = NullSink()
    SINKS.append(sink)
    return sink


def ai_settings(**kw):
    values = dict(ai_conversation_enabled=True, sarvam_api_key=SecretStr("k"), ai_barge_in_grace_ms=0)
    values.update(kw)
    return make_settings(**values)


def read_utterance(ws) -> tuple[list[dict], str]:
    """Read playAudio messages up to and including the checkpoint; return (chunks, checkpoint name)."""
    chunks = []
    while True:
        message = recv_json(ws)
        if message["event"] == "playAudio":
            chunks.append(message)
        elif message["event"] == "checkpoint":
            return chunks, message["name"]
        else:
            raise AssertionError(f"unexpected message {message['event']}")


def app_with_fakes(**settings_kw):
    llm, tts = FakeLlm(), FakeTts()
    app = create_app(ai_settings(**settings_kw), audio_sink_factory=null_factory, conversation_llm=llm,
                     conversation_tts=tts)
    return app, llm, tts


def test_inbound_call_greets_then_holds_a_multi_turn_conversation_and_cleans_up() -> None:
    app, llm, tts = app_with_fakes()
    with TestClient(app) as client:
        manager = client.app.state.conversation
        with open_stream(client) as stream:
            ws = stream.ws
            # 1. The greeting is the first thing the server sends once the stream has started.
            chunks, checkpoint = read_utterance(ws)
            assert all(c["streamId"] == STREAM and c["media"]["contentType"] == "audio/x-l16" for c in chunks)
            assert b"".join(base64.b64decode(c["media"]["payload"]) for c in chunks) == bytes([marker_for(GREETING_TEXT)]) * PCM_BYTES
            ws.send_json({"event": "playedStream", "name": checkpoint})
            wait_until(lambda: manager.metrics.greetings_acknowledged == 1)

            # inbound audio keeps flowing to the STT sink while the assistant works and speaks
            ws.send_json(media_msg(b"\x01\x00" * 320, chunk=1, seq=1))
            wait_until(lambda: SINKS and SINKS[-1].frames >= 1)

            # 2. Two consecutive customer turns through the real transcript pipeline.
            pipeline = client.app.state.transcript_pipeline
            for turn, text in enumerate(["my internet is slow", "it started yesterday"], start=1):
                client.portal.call(pipeline.publish, final(CALL, text, turn, stream_id=STREAM))
                chunks, checkpoint = read_utterance(ws)
                reply = f"Reply to {text}"
                assert b"".join(base64.b64decode(c["media"]["payload"]) for c in chunks) == bytes([marker_for(reply)]) * PCM_BYTES
                ws.send_json({"event": "playedStream", "name": checkpoint})
                wait_until(lambda: manager.metrics.playback_acknowledged == turn + 1)

            assert len(llm.calls) == 2 and tts.texts[0] == GREETING_TEXT
            assert [m.content for m in llm.calls[1][1]][-1] == "it started yesterday"
            assert manager.metrics.greetings_attempted == 1 and manager.metrics.utterances_processed == 2

            # 3. Barge-in through the pipeline: VAD speech start while a reply is playing.
            client.portal.call(pipeline.publish, final(CALL, "one more thing", 3, stream_id=STREAM))
            read_utterance(ws)  # the reply is now playing (never acknowledged)
            client.portal.call(pipeline.publish, speech_start(CALL, 4, stream_id=STREAM))
            assert recv_json(ws)["event"] == "clearAudio"
            assert manager.metrics.interruptions == 1

            # 4. Hangup: state and tasks are released.
            client.portal.call(client.app.state.call_manager.on_hangup, CALL)
        wait_until(lambda: manager.conversation(CALL) is None)
        wait_until(lambda: not [t for t in manager._tasks if not t.done()])
        assert manager.metrics.calls_cleaned == 1


def test_reconnect_does_not_repeat_the_greeting() -> None:
    app, llm, tts = app_with_fakes()
    with TestClient(app) as client:
        manager = client.app.state.conversation
        with open_stream(client, stream=STREAM) as first:
            _chunks, checkpoint = read_utterance(first.ws)
            first.ws.send_json({"event": "playedStream", "name": checkpoint})
            wait_until(lambda: manager.metrics.greetings_acknowledged == 1)
            with open_stream(client, stream="second-stream") as second:
                wait_until(lambda: client.app.state.call_manager.current_stream(CALL).stream_id == "second-stream")
                assert tts.texts == [GREETING_TEXT] and manager.metrics.greetings_attempted == 1
                # the conversation continues on the new stream
                pipeline = client.app.state.transcript_pipeline
                client.portal.call(pipeline.publish, final(CALL, "hello again", 1, stream_id="second-stream"))
                chunks, _name = read_utterance(second.ws)
                assert all(c["streamId"] == "second-stream" for c in chunks)


def test_two_simultaneous_calls_never_cross_audio() -> None:
    app, llm, tts = app_with_fakes()
    other_call, other_stream = "9f0d2d43-1111-4222-8333-444455556666", "other-stream"
    with TestClient(app) as client:
        manager = client.app.state.conversation
        with open_stream(client) as a, open_stream(client, call=other_call, stream=other_stream) as b:
            for s in (a, b):
                _c, name = read_utterance(s.ws)
                s.ws.send_json({"event": "playedStream", "name": name})
            wait_until(lambda: manager.metrics.greetings_acknowledged == 2)
            pipeline = client.app.state.transcript_pipeline
            client.portal.call(pipeline.publish, final(other_call, "only the second caller", 1, stream_id=other_stream))
            chunks, _name = read_utterance(b.ws)
            assert b"".join(base64.b64decode(c["media"]["payload"]) for c in chunks) == bytes([marker_for("Reply to only the second caller")]) * PCM_BYTES
            assert [m.content for m in manager.conversation(CALL).messages] == [GREETING_TEXT]  # call A untouched


def test_feature_is_off_by_default_and_adds_no_behaviour() -> None:
    with TestClient(create_app(make_settings(), audio_sink_factory=null_factory)) as client:
        assert client.app.state.conversation is None
        with open_stream(client) as stream:
            assert stream.session is not None  # the normal streaming path is unchanged: no unsolicited audio
        assert client.get("/api/ai/conversations", headers=HEADERS).json() == {"enabled": False}


def test_status_endpoint_reports_metrics_without_text() -> None:
    app, _llm, _tts = app_with_fakes()
    with TestClient(app) as client:
        assert client.get("/api/ai/conversations").status_code in (401, 403)
        body = client.get("/api/ai/conversations", headers=HEADERS).json()
        assert body["enabled"] is True and "greetings_attempted" in body["metrics"]


@pytest.mark.parametrize(
    "kwargs, error",
    [
        (dict(audio_sink_factory=None, conversation_llm=FakeLlm(), conversation_tts=FakeTts()), LlmConfigError),  # no STT
        (dict(audio_sink_factory=null_factory, conversation_tts=FakeTts()), LlmConfigError),  # no BEDROCK ARN
    ],
)
def test_enabling_without_prerequisites_fails_fast(kwargs, error) -> None:
    with pytest.raises(error) as info:
        create_app(ai_settings(), **kwargs)
    assert ARN not in str(info.value)


def test_enabling_without_a_sarvam_key_fails_fast() -> None:
    with pytest.raises(SarvamConfigError):
        create_app(ai_settings(sarvam_api_key=SecretStr("")), audio_sink_factory=null_factory,
                   conversation_llm=FakeLlm())


def test_bedrock_arn_setting_builds_the_converse_client_without_any_network_call() -> None:
    app = create_app(ai_settings(bedrock_inference_profile_arn=ARN), audio_sink_factory=null_factory,
                     conversation_tts=FakeTts())
    manager = app.state.conversation
    assert manager is not None and manager._llm._model_id == ARN and manager._llm.region == "ap-south-1"
