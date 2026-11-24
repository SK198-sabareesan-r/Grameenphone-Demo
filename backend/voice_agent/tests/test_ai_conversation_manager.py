"""AI voice conversation: ConversationManager with a fake LLM, fake TTS and a real PlaybackController
writing to a fake Vobiz socket. No provider is contacted."""

from __future__ import annotations

import asyncio
import base64
import logging
from types import SimpleNamespace

import pytest

from app.integrations.vobiz.stream_sender import StreamClosedError
from app.services.conversation.manager import ConversationManager
from app.services.conversation.prompts import GREETING_TEXT, SYSTEM_PROMPT, clean_reply
from app.services.conversation.state import Phase
from app.services.llm.base import LlmThrottledError
from app.services.sarvam.errors import SarvamServerError
from tests.ai_fakes import (
    PCM_BYTES,
    FakeLlm,
    FakeTts,
    final,
    make_stream,
    marker_for,
    partial,
    settle,
    speech_end,
    speech_start,
    transcript,
)

A, B = "call-A", "call-B"


class Harness:
    def __init__(self, **kw) -> None:
        self.llm, self.tts = FakeLlm(), FakeTts()
        self.streams: dict[str, object] = {}
        self.wires: dict[str, object] = {}
        self.call_of_stream: dict[str, str] = {}
        opts = dict(barge_in_grace_ms=0, llm_timeout_s=2.0, tts_timeout_s=2.0)
        opts.update(kw)
        self.mgr = ConversationManager(llm=self.llm, tts=self.tts, get_stream=self.streams.get, **opts)

    def add_stream(self, call: str, stream_id: str = "s1", **kw):
        session, wire = make_stream(call, stream_id, **kw)
        self.streams[call] = session
        self.wires[stream_id] = wire
        self.call_of_stream[stream_id] = call
        return session

    async def start(self, call: str, stream_id: str = "s1", **kw):
        session = self.add_stream(call, stream_id, **kw)
        await self.mgr.on_stream_start(session)
        return session

    def wire(self, call: str):
        return self.wires[self.streams[call].stream_id]

    async def ack(self, call: str) -> None:
        """Vobiz confirms the next utterance that was sent on the call's stream."""
        wire, session = self.wire(call), self.streams[call]
        await settle(lambda: any(m["name"] not in wire.acked for m in wire.of("checkpoint")))
        name = next(m["name"] for m in wire.of("checkpoint") if m["name"] not in wire.acked)
        wire.acked.add(name)
        session.playback.handle_played_stream(name)

    async def greeted(self, call: str, stream_id: str = "s1", **kw):
        """Start the stream and let the greeting finish (sent and acknowledged)."""
        session = await self.start(call, stream_id, **kw)
        await self.ack(call)
        await settle(lambda: self.mgr.conversation(call).phase == Phase.IDLE)
        return session

    async def say(self, call: str, text: str, seq: int, **kw) -> None:
        await self.mgr.on_event(final(call, text, seq, **kw))

    async def reply_played(self, call: str, n_checkpoints: int) -> None:
        wire = self.wire(call)
        await settle(lambda: len(wire.of("checkpoint")) >= n_checkpoints)

    def idle_tasks(self) -> list:
        return [t for t in self.mgr._tasks if not t.done()]


# --- greeting ------------------------------------------------------------------------------------


async def test_greeting_is_spoken_once_with_the_exact_text_and_vobiz_message_format() -> None:
    h = Harness()
    await h.start(A)
    await settle(lambda: h.wire(A).of("checkpoint"))
    assert h.tts.texts == [GREETING_TEXT]
    assert GREETING_TEXT == (
        "Hello! Welcome to Grameenphone Customer Care. I'm your virtual assistant. How can I help you today?"
    )
    wire = h.wire(A)
    plays = wire.of("playAudio")
    assert len(plays) == 5  # 100 ms in 20 ms chunks
    for m in plays:
        assert m["streamId"] == "s1"
        assert m["media"]["contentType"] == "audio/x-l16" and m["media"]["sampleRate"] == 16000
        assert len(base64.b64decode(m["media"]["payload"])) == 640
    assert b"".join(base64.b64decode(m["media"]["payload"]) for m in plays) == bytes([marker_for(GREETING_TEXT)]) * PCM_BYTES
    # order: every chunk, then the checkpoint
    assert [m["event"] for m in wire.messages] == ["playAudio"] * 5 + ["checkpoint"]
    # sent but not yet acknowledged
    assert (h.mgr.metrics.greetings_attempted, h.mgr.metrics.greetings_sent, h.mgr.metrics.greetings_acknowledged) == (1, 1, 0)
    await h.ack(A)
    await settle(lambda: h.mgr.metrics.greetings_acknowledged == 1)
    assert h.llm.calls == [] and h.mgr.conversation(A).greeting == "played"


async def test_stream_start_does_not_block_on_tts_and_nothing_is_sent_before_the_stream_exists() -> None:
    h = Harness()
    gate = asyncio.Event()
    h.tts.gates.append(gate)
    assert h.tts.texts == []  # nothing happens before a stream starts
    await asyncio.wait_for(h.start(A), 0.5)  # returns although TTS is still pending
    await settle(lambda: h.tts.texts)
    assert h.wire(A).messages == [] and h.mgr.conversation(A).phase == Phase.SYNTHESIZING
    gate.set()
    await settle(lambda: h.wire(A).of("checkpoint"))


async def test_greeting_is_not_repeated_after_a_stream_reconnect() -> None:
    h = Harness()
    await h.greeted(A, "s1")
    await h.mgr.on_stream_start(h.add_stream(A, "s2"))  # reconnect: new stream id, same logical call
    await asyncio.sleep(0.05)
    assert h.tts.texts == [GREETING_TEXT] and h.wires["s2"].messages == []
    assert h.mgr.metrics.greetings_attempted == 1


async def test_greeting_is_not_repeated_even_after_the_conversation_state_was_released() -> None:
    h = Harness()
    await h.greeted(A)
    await h.mgr.on_call_closed(SimpleNamespace(call_id=A, reason="idle"))
    assert h.mgr.conversation(A) is None
    await h.mgr.on_stream_start(h.add_stream(A, "s2"))
    await asyncio.sleep(0.05)
    assert h.tts.texts == [GREETING_TEXT] and h.wires["s2"].messages == []


async def test_monitor_streams_never_get_a_greeting() -> None:
    h = Harness()
    await h.mgr.on_stream_start(h.add_stream(A, "m1", kind="monitor"))
    await asyncio.sleep(0.05)
    assert h.tts.texts == [] and h.mgr.conversation(A) is None


async def test_greeting_audio_is_synthesized_once_and_reused_for_later_calls() -> None:
    h = Harness()
    await h.greeted(A)
    await h.greeted(B, "s2")
    assert h.tts.texts == [GREETING_TEXT]  # second call used the cached audio
    assert h.wire(B).pcm_of(0) == bytes([marker_for(GREETING_TEXT)]) * PCM_BYTES
    assert h.mgr.metrics.greetings_attempted == 2


async def test_greeting_tts_failure_is_contained_and_the_call_still_converses() -> None:
    h = Harness()
    h.tts.errors.append(SarvamServerError("boom"))
    await h.start(A)
    await settle(lambda: h.mgr.metrics.greetings_failed == 1)
    assert h.mgr.conversation(A).greeting == "failed" and h.wire(A).messages == []
    await h.say(A, "my internet is slow", 1)
    await h.reply_played(A, 1)
    assert h.llm.calls and h.mgr.metrics.tts_failures == 1


async def test_greeting_playback_failure_does_not_crash_anything() -> None:
    h = Harness()
    session = h.add_stream(A)
    h.wires["s1"].fail = True  # the socket rejects every send
    await h.mgr.on_stream_start(session)
    await settle(lambda: h.mgr.metrics.greetings_failed == 1)
    assert h.mgr.metrics.playback_failed == 1 and h.idle_tasks() == []
    await h.mgr.on_event(final(A, "hello", 1))  # still accepts customer speech


async def test_customer_speaking_before_the_greeting_is_ready_does_not_cancel_it() -> None:
    h = Harness()
    gate = asyncio.Event()
    h.tts.gates.append(gate)
    await h.start(A)
    await settle(lambda: h.tts.texts)
    await h.say(A, "hello?", 1)  # spoken while the greeting is still being synthesized
    assert h.llm.calls == [] and h.mgr.metrics.tasks_cancelled == 0
    gate.set()
    await h.ack(A)  # greeting played to the end
    await h.reply_played(A, 2)  # then the caller's words are answered
    assert h.tts.texts[0] == GREETING_TEXT and len(h.llm.calls) == 1
    assert [m.role for m in h.llm.calls[0][1]] == ["assistant", "user"]


# --- transcript -> LLM ---------------------------------------------------------------------------


async def test_partial_and_other_events_do_not_trigger_a_response() -> None:
    h = Harness()
    await h.greeted(A)
    await h.mgr.on_event(partial(A, "my inter", 1))
    await h.mgr.on_event(speech_start(A, 2))
    await h.mgr.on_event(speech_end(A, 3))
    await asyncio.sleep(0.05)
    assert h.llm.calls == [] and len(h.wire(A).of("checkpoint")) == 1


async def test_final_transcript_triggers_exactly_one_response_that_is_spoken() -> None:
    h = Harness()
    await h.greeted(A)
    await h.say(A, "my internet is slow", 1)
    await h.reply_played(A, 2)
    system, messages = h.llm.calls[0]
    assert len(h.llm.calls) == 1 and system == SYSTEM_PROMPT
    assert [(m.role, m.content) for m in messages] == [("assistant", GREETING_TEXT), ("user", "my internet is slow")]
    reply = "Reply to my internet is slow"
    assert h.tts.texts == [GREETING_TEXT, reply]  # the LLM reply is what gets synthesized
    assert h.wire(A).pcm_of(1) == bytes([marker_for(reply)]) * PCM_BYTES
    await h.ack(A)
    await settle(lambda: h.mgr.metrics.playback_acknowledged == 2)
    conv = h.mgr.conversation(A)
    assert conv.messages[-1].content == reply and conv.phase == Phase.IDLE
    assert h.mgr.metrics.utterances_processed == 1 and h.mgr.metrics.llm_requests == 1


async def test_duplicate_final_delivery_does_not_duplicate_the_response() -> None:
    h = Harness()
    await h.greeted(A)
    event = final(A, "my internet is slow", 7)
    await h.mgr.on_event(event)
    await h.mgr.on_event(event)
    await h.reply_played(A, 2)
    await asyncio.sleep(0.05)
    assert len(h.llm.calls) == 1 and len(h.wire(A).of("checkpoint")) == 2
    assert h.mgr.metrics.duplicate_finals == 1 and h.mgr.metrics.utterances_processed == 1


async def test_several_turns_in_one_call_keep_the_history() -> None:
    h = Harness()
    await h.greeted(A)
    for turn, text in enumerate(["first question", "second question", "third question"], start=1):
        await h.say(A, text, turn)
        await h.reply_played(A, turn + 1)
        await h.ack(A)
        await settle(lambda: h.mgr.conversation(A).phase == Phase.IDLE)
    assert len(h.llm.calls) == 3 and h.mgr.metrics.playback_acknowledged == 4
    last = h.llm.calls[2][1]
    assert [m.role for m in last] == ["assistant", "user", "assistant", "user", "assistant", "user"]
    assert last[-1].content == "third question" and last[1].content == "first question"


async def test_calls_have_isolated_histories_and_audio() -> None:
    h = Harness()
    await h.greeted(A, "sA")
    await h.greeted(B, "sB")
    await h.say(A, "question from A", 1)
    await h.say(B, "question from B", 1)
    await h.reply_played(A, 2)
    await h.reply_played(B, 2)
    texts = {c: [m.content for m in h.mgr.conversation(c).messages] for c in (A, B)}
    assert "question from A" in texts[A] and "question from B" not in texts[A]
    assert "question from B" in texts[B] and "question from A" not in texts[B]
    for _system, messages in h.llm.calls:
        contents = {m.content for m in messages}
        assert not {"question from A", "question from B"} <= contents
    assert h.wire(A).pcm_of(1) == bytes([marker_for("Reply to question from A")]) * PCM_BYTES
    assert h.wire(B).pcm_of(1) == bytes([marker_for("Reply to question from B")]) * PCM_BYTES
    assert all(m["streamId"] == "sA" for m in h.wires["sA"].of("playAudio"))
    assert all(m["streamId"] == "sB" for m in h.wires["sB"].of("playAudio"))


@pytest.mark.parametrize(
    "event",
    [
        final(A, "text", 1, track="track_outbound"),
        final(A, "text", 2, speaker="agent"),
        final(A, "text", 3, source="other"),
        final(A, "   ", 4),
        final("unknown-call", "text", 5),
    ],
)
async def test_non_customer_or_empty_transcripts_are_not_customer_input(event) -> None:
    h = Harness()
    await h.greeted(A)
    await h.mgr.on_event(event)
    await asyncio.sleep(0.05)
    assert h.llm.calls == [] and h.mgr.conversation("unknown-call") is None


async def test_inbound_track_label_from_a_monitored_stream_is_accepted() -> None:
    h = Harness()
    await h.greeted(A)
    await h.mgr.on_event(final(A, "hello there", 1, track="track_inbound"))
    await h.reply_played(A, 2)
    assert len(h.llm.calls) == 1


async def test_history_is_bounded() -> None:
    h = Harness(history_max_messages=4)
    await h.greeted(A)
    for turn in range(1, 6):
        await h.say(A, f"question {turn}", turn)
        await h.reply_played(A, turn + 1)
        await h.ack(A)
        await settle(lambda: h.mgr.conversation(A).phase == Phase.IDLE)
    assert len(h.mgr.conversation(A).messages) == 4
    assert max(len(messages) for _s, messages in h.llm.calls) <= 4


# --- LLM -> TTS ----------------------------------------------------------------------------------


async def test_reply_is_cleaned_for_speech_before_tts() -> None:
    h = Harness()
    h.llm.reply_fn = lambda messages: "**Sure!**\n- I can help with that."
    await h.greeted(A)
    await h.say(A, "help", 1)
    await h.reply_played(A, 2)
    assert h.tts.texts[1] == "Sure! I can help with that."
    assert clean_reply("x" * 1000) != "x" * 1000 and len(clean_reply("word " * 200)) <= 350


async def test_llm_failure_is_contained_and_the_next_utterance_still_works() -> None:
    h = Harness()
    await h.greeted(A)
    h.llm.errors.append(LlmThrottledError("throttled"))
    await h.say(A, "first", 1)
    await settle(lambda: h.mgr.metrics.llm_failures == 1)
    assert h.tts.texts == [GREETING_TEXT]  # nothing was synthesized for the failed turn
    await settle(lambda: h.mgr.conversation(A).phase == Phase.IDLE)
    await h.say(A, "second", 2)
    await h.reply_played(A, 2)
    assert h.mgr.metrics.conversation_failures == 1


async def test_llm_timeout_does_not_block_the_caller_of_on_event() -> None:
    h = Harness(llm_timeout_s=0.05)
    await h.greeted(A)
    h.llm.gate = asyncio.Event()  # never released: the provider hangs
    await asyncio.wait_for(h.say(A, "hello", 1), 0.5)  # the subscriber callback returns at once
    await settle(lambda: h.mgr.metrics.llm_failures == 1)
    assert h.llm.cancelled == 1 and h.idle_tasks() == []
    h.llm.gate = None
    await h.say(A, "hello again", 2)
    await h.reply_played(A, 2)


async def test_tts_failure_timeout_and_empty_audio_are_rejected_without_playback() -> None:
    h = Harness(tts_timeout_s=0.05)
    await h.greeted(A)
    h.tts.errors.append(SarvamServerError("boom"))
    await h.say(A, "one", 1)
    await settle(lambda: h.mgr.metrics.tts_failures == 1)
    await settle(lambda: h.mgr.conversation(A).phase == Phase.IDLE)
    h.tts.empty_next = True  # 200 OK with zero audio bytes
    await h.say(A, "two", 2)
    await settle(lambda: h.mgr.metrics.tts_failures == 2)
    await settle(lambda: h.mgr.conversation(A).phase == Phase.IDLE)
    h.tts.gates.append(asyncio.Event())  # TTS hangs
    await asyncio.wait_for(h.say(A, "three", 3), 0.5)
    await settle(lambda: h.mgr.metrics.tts_failures == 3)
    assert h.tts.cancelled == 1 and len(h.wire(A).of("checkpoint")) == 1  # only the greeting was ever played
    await settle(lambda: h.mgr.conversation(A).phase == Phase.IDLE)
    await h.say(A, "four", 4)
    await h.reply_played(A, 2)  # the conversation recovers


# --- playback ------------------------------------------------------------------------------------


async def test_unacknowledged_playback_is_reported_as_a_timeout_not_as_heard() -> None:
    h = Harness()
    await h.greeted(A, checkpoint_timeout_s=0.05)
    await h.say(A, "hello", 1)
    await settle(lambda: h.mgr.metrics.playback_timeouts == 1)
    m = h.mgr.metrics
    assert (m.playback_sent, m.playback_acknowledged) == (2, 1)  # greeting acked; reply sent, never confirmed


async def test_playback_failure_after_a_reply_is_contained() -> None:
    h = Harness()
    await h.greeted(A)
    h.wire(A).fail = True
    await h.say(A, "hello", 1)
    await settle(lambda: h.mgr.metrics.playback_failed == 1)
    assert h.mgr.conversation(A).phase == Phase.IDLE
    await h.mgr.on_event(final(A, "still there?", 2))  # never raises: STT/pipeline keep running


async def test_reply_without_a_live_stream_is_skipped() -> None:
    h = Harness()
    await h.greeted(A)
    h.llm.gate = asyncio.Event()
    await h.say(A, "hello", 1)
    await settle(lambda: h.llm.calls)
    h.streams[A].playback.close()
    h.streams[A].end_reason = "socket_closed"
    h.llm.gate.set()
    await settle(lambda: h.mgr.metrics.playback_failed == 1)


# --- interruption --------------------------------------------------------------------------------


async def test_barge_in_clears_audio_stops_playback_and_the_next_utterance_is_answered() -> None:
    h = Harness()
    session = await h.greeted(A)
    await h.say(A, "tell me about data packs", 1)
    await h.reply_played(A, 2)
    assert h.mgr.conversation(A).phase == Phase.PLAYING
    plays_before = len(h.wire(A).of("playAudio"))
    await h.mgr.on_event(speech_start(A, 2))  # the caller starts talking over the assistant
    assert h.wire(A).messages[-1]["event"] == "clearAudio"
    conv = h.mgr.conversation(A)
    assert conv.phase == Phase.IDLE and conv.interruptions == 1
    m = h.mgr.metrics
    assert (m.interruptions, m.playback_cleared, m.tasks_cancelled) == (1, 1, 1)
    assert session.playback.pending_checkpoints == frozenset()  # voided: the old audio will never be "acked"
    session.playback.handle_cleared_audio()  # Vobiz confirms
    await asyncio.sleep(0.05)
    assert len(h.wire(A).of("playAudio")) == plays_before  # no stale audio after the interruption
    # STT is untouched: the caller's new sentence arrives as a final and gets its own answer
    await h.say(A, "actually I want roaming", 3)
    await h.reply_played(A, 3)
    assert h.llm.calls[-1][1][-1].content == "actually I want roaming"
    assert h.wire(A).pcm_of(2) == bytes([marker_for("Reply to actually I want roaming")]) * PCM_BYTES


async def test_a_new_final_while_the_assistant_is_speaking_interrupts_it_too() -> None:
    h = Harness()
    await h.greeted(A)
    await h.say(A, "one", 1)
    await h.reply_played(A, 2)
    await h.say(A, "two", 2)  # no speech_start was seen, but a final arrived
    assert h.wire(A).of("clearAudio")
    h.streams[A].playback.handle_cleared_audio()
    await h.reply_played(A, 3)
    assert h.llm.calls[-1][1][-1].content == "two"


async def test_new_utterance_during_tts_drops_the_stale_audio() -> None:
    h = Harness()
    await h.greeted(A)
    stale_gate = asyncio.Event()
    h.tts.gates.append(stale_gate)
    await h.say(A, "first", 1)
    await settle(lambda: len(h.tts.texts) == 2)  # reply 1 is being synthesized
    await h.say(A, "second", 2)  # the caller spoke again
    await h.reply_played(A, 2)
    stale_gate.set()  # the stale synthesis would complete now
    await asyncio.sleep(0.05)
    assert len(h.wire(A).of("checkpoint")) == 2  # greeting + the reply to "second" only
    reply = h.llm.calls[1][1]  # what the model saw for the second request
    assert h.wire(A).pcm_of(1) == bytes([marker_for("Reply to second")]) * PCM_BYTES
    assert h.tts.cancelled == 1 and h.wire(A).of("clearAudio") == []  # nothing was playing, nothing to clear
    # the first reply had reached the history before TTS was cancelled; the second request follows it
    assert [m.role for m in reply] == ["assistant", "user", "assistant", "user"]


async def test_new_utterance_while_the_llm_runs_supersedes_the_old_request() -> None:
    h = Harness()
    await h.greeted(A)
    h.llm.gate = asyncio.Event()
    await h.say(A, "first", 1)
    await settle(lambda: len(h.llm.calls) == 1)
    h.llm.gate.set()
    await h.say(A, "second", 2)
    await h.reply_played(A, 2)
    await asyncio.sleep(0.05)
    assert len(h.wire(A).of("checkpoint")) == 2 and h.llm.cancelled == 1


async def test_speech_start_only_interrupts_assistant_audio_that_is_playing() -> None:
    h = Harness()
    await h.greeted(A)
    h.llm.gate = asyncio.Event()
    await h.say(A, "hello", 1)
    await settle(lambda: h.llm.calls)
    await h.mgr.on_event(speech_start(A, 2))  # while thinking: nothing is playing yet
    assert h.llm.cancelled == 0 and h.mgr.metrics.interruptions == 0 and h.wire(A).of("clearAudio") == []
    h.llm.gate.set()
    await h.reply_played(A, 2)


async def test_speech_start_from_a_non_customer_track_is_ignored() -> None:
    h = Harness()
    await h.greeted(A)
    await h.say(A, "hello", 1)
    await h.reply_played(A, 2)
    await h.mgr.on_event(speech_start(A, 2, track="track_outbound"))
    assert h.mgr.metrics.interruptions == 0 and h.wire(A).of("clearAudio") == []


async def test_barge_in_grace_period_and_switch() -> None:
    h = Harness(barge_in_grace_ms=60_000)
    await h.greeted(A)
    await h.say(A, "hello", 1)
    await h.reply_played(A, 2)
    await h.mgr.on_event(speech_start(A, 2))  # right after playback began: ignored (echo / late VAD)
    assert h.mgr.metrics.barge_in_ignored_grace == 1 and h.wire(A).of("clearAudio") == []

    off = Harness(barge_in_enabled=False)
    await off.greeted(A)
    await off.say(A, "hello", 1)
    await off.reply_played(A, 2)
    await off.mgr.on_event(speech_start(A, 2))
    assert off.mgr.metrics.interruptions == 0 and off.wire(A).of("clearAudio") == []


# --- lifecycle -----------------------------------------------------------------------------------


async def test_hangup_during_the_llm_request_cancels_it_and_removes_state() -> None:
    h = Harness()
    await h.greeted(A)
    h.llm.gate = asyncio.Event()
    await h.say(A, "hello", 1)
    await settle(lambda: h.llm.calls)
    await h.mgr.on_call_ended(A, "hangup")
    assert h.llm.cancelled == 1 and h.mgr.conversation(A) is None and h.idle_tasks() == []
    h.llm.gate.set()
    await asyncio.sleep(0.05)
    assert len(h.wire(A).of("checkpoint")) == 1 and h.mgr.metrics.calls_cleaned == 1
    await h.mgr.on_event(final(A, "late final after hangup", 2))  # ignored, state is not re-created
    assert h.mgr.conversation(A) is None and len(h.llm.calls) == 1


async def test_hangup_during_tts_and_during_playback_cancels_the_work() -> None:
    h = Harness()
    await h.greeted(A)
    h.tts.gates.append(asyncio.Event())
    await h.say(A, "hello", 1)
    await settle(lambda: len(h.tts.texts) == 2)
    await h.mgr.on_call_ended(A, "hangup")
    assert h.tts.cancelled == 1 and h.idle_tasks() == []

    await h.greeted(B, "sB")
    await h.say(B, "hello", 1)
    await h.reply_played(B, 2)  # reply is being played, awaiting its acknowledgement
    await h.mgr.on_call_ended(B, "hangup")
    assert h.idle_tasks() == [] and h.mgr.conversation(B) is None


async def test_state_is_released_when_the_transcript_session_closes() -> None:
    h = Harness()
    await h.greeted(A)
    await h.mgr.on_call_closed(SimpleNamespace(call_id=A, reason="idle"))
    assert h.mgr.conversation(A) is None and h.mgr.snapshot()["calls_active"] == 0
    await h.mgr.on_call_ended(A, "hangup")  # idempotent


async def test_reconnect_keeps_working_without_leaking_tasks_and_replies_on_the_new_stream() -> None:
    h = Harness()
    await h.greeted(A, "s1")
    h.llm.gate = asyncio.Event()
    await h.say(A, "hello", 1)
    await settle(lambda: h.llm.calls)
    await h.mgr.on_stream_start(h.add_stream(A, "s2"))  # Vobiz reconnects while the LLM is working
    h.llm.gate.set()
    await settle(lambda: h.wires["s2"].of("checkpoint"))
    assert h.tts.texts.count(GREETING_TEXT) == 1 and len(h.wires["s1"].of("checkpoint")) == 1
    assert all(m["streamId"] == "s2" for m in h.wires["s2"].of("playAudio"))
    await h.ack(A)
    await settle(lambda: h.idle_tasks() == [])


async def test_shutdown_cancels_everything() -> None:
    h = Harness()
    await h.greeted(A)
    await h.greeted(B, "sB")
    h.llm.gate = asyncio.Event()
    await h.say(A, "x", 1)
    await h.say(B, "y", 1)
    await settle(lambda: len(h.llm.calls) == 2)
    await h.mgr.shutdown()
    assert h.llm.cancelled == 2 and h.idle_tasks() == [] and h.mgr.snapshot()["calls_active"] == 0


async def test_logs_never_contain_transcripts_or_replies(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    h = Harness()
    h.llm.reply_fn = lambda messages: "assistant-secret-reply"
    await h.greeted(A)
    await h.say(A, "customer-secret-words", 1)
    await h.reply_played(A, 2)
    await h.mgr.on_event(speech_start(A, 2))
    await h.mgr.on_call_ended(A, "hangup")
    logged = " ".join(f"{r.getMessage()} {sorted(r.__dict__.items(), key=str)}" for r in caplog.records)
    assert "ai_utterance_accepted" in logged and "ai_conversation_cleaned" in logged
    assert "customer-secret-words" not in logged and "assistant-secret-reply" not in logged
    assert GREETING_TEXT not in logged


def test_stream_closed_error_is_the_playback_failure_type() -> None:
    assert issubclass(StreamClosedError, RuntimeError)  # contained by the manager's broad playback handler
