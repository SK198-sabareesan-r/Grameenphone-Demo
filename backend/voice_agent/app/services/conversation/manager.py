"""Orchestrates the inbound AI voice conversation for every live call.

It plugs into the existing architecture in two places and adds no audio path of its own:

* as a call-manager `StreamListener`: a PRIMARY stream starting triggers the one-time greeting
  (spawned as a task: `on_stream_start` is awaited before the STT consumer starts, so it must not block);
  `on_call_ended` removes the call's state.
* as a global transcript-pipeline subscriber: customer FINAL transcripts drive LLM -> TTS -> playback;
  `vad.speech_start` drives barge-in; `on_call_closed` is a second cleanup path.

Each call has one `CallConversation` and at most ONE response task (greeting or turn). The task runs
LLM -> TTS -> `session.playback.play(wait=True)` on the call's current primary stream, resolved at send
time, so it survives a stream reconnect and can never touch another call's stream. Nothing here waits on
the WebSocket receive loop or the STT sink; subscriber callbacks only start/cancel tasks.

Interruption: a new final transcript, or (barge-in) customer speech while assistant audio is playing,
bumps the turn id (so stale work is dropped), cancels the task and sends clearAudio if audio was being
played. Logs and metrics carry ids, counts and timings, never transcript or reply text.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Protocol

from app.publishers.transcripts import TRANSCRIPT_FINAL, VAD_SPEECH_START, TranscriptEvent
from app.services.conversation.prompts import (
    CONTACT_DETAILS_BLOCKED_REPLY,
    GREETING_TEXT,
    SYSTEM_PROMPT,
    clean_reply,
    contains_contact_details,
)
from app.services.conversation.resolution import ResolutionPolicy
from app.services.conversation.state import CallConversation, ConversationMetrics, Phase
from app.services.llm.base import LlmClient, LlmError
from app.services.voice.playback import CLEARED, PLAYED, TIMEOUT
from app.services.voice.track_router import TRACK_INBOUND

logger = logging.getLogger(__name__)

MAX_UTTERANCE_CHARS = 1000
MAX_GREETED_CALLS = 10_000
CANCEL_WAIT_S = 1.0  # bound on awaiting a cancelled task inside a (5 s bounded) subscriber callback


class TtsClient(Protocol):
    async def synthesize(self, text: str) -> Any: ...  # -> app.services.sarvam.tts.TtsAudio


def _error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) else type(exc).__name__


class ConversationManager:
    def __init__(
        self,
        *,
        llm: LlmClient,
        tts: TtsClient,
        get_stream: Callable[[str], Any],
        history_max_messages: int = 20,
        llm_timeout_s: float = 14.0,
        tts_timeout_s: float = 12.0,
        barge_in_enabled: bool = True,
        barge_in_grace_ms: int = 500,
        greeting_text: str = GREETING_TEXT,
        on_assistant_message: Callable[[str, str], None] | None = None,
        handoff_trigger: Callable[[str, str], bool] | None = None,
        resolution: ResolutionPolicy | None = None,
        unresolved_handoff: Callable[[str, str], bool] | None = None,
        on_resolution_event: Callable[[str, str, dict], None] | None = None,
        knowledge: Any = None,  # KnowledgeLookup: approved-article text added to the system prompt per turn
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._llm = llm
        self._tts = tts
        self._get_stream = get_stream  # call_id -> the call's live PRIMARY StreamSession (or None)
        self._history_max = history_max_messages
        self._llm_timeout_s = llm_timeout_s
        self._tts_timeout_s = tts_timeout_s
        self._barge_in = barge_in_enabled
        self._grace_s = barge_in_grace_ms / 1000
        self._greeting_text = greeting_text
        # (call_id, text) when the AI produces a reply. Must be quick and non-blocking; failures are ignored.
        self._on_assistant_message = on_assistant_message
        # (call_id, customer_text) -> True if a human hand-off was started; the AI then does not answer.
        self._handoff_trigger = handoff_trigger
        # AI-first routing: `resolution` counts the customer's "it still does not work" reports; when a call becomes
        # eligible and the model's structured assessment says escalate, `unresolved_handoff(call_id, reason)` starts the
        # transfer (it returns False when no transfer is possible, and the AI simply keeps helping).
        self._resolution = resolution
        self._unresolved_handoff = unresolved_handoff
        self._on_resolution_event = on_resolution_event
        self._knowledge = knowledge
        self._clock = clock
        self._calls: dict[str, CallConversation] = {}
        self._greeted: OrderedDict[str, None] = OrderedDict()  # logical calls that already got a greeting
        self._tasks: set[asyncio.Task[None]] = set()
        self._greeting_audio: Any = None  # synthesized once per process; the text never changes
        self.metrics = ConversationMetrics()

    # --- diagnostics ----------------------------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        return {
            "enabled": True,
            "calls_active": len(self._calls),
            "metrics": self.metrics.snapshot(),
            "calls": [c.summary() for c in self._calls.values()],
        }

    def conversation(self, call_id: str) -> CallConversation | None:
        return self._calls.get(call_id)

    # --- StreamListener (call manager) ----------------------------------------------------------

    async def on_stream_start(self, session: Any) -> None:
        if session.kind != "primary":  # monitored streams never carry playback
            return
        call_id = session.call_id
        conv = self._calls.get(call_id)
        if conv is None:
            conv = self._calls[call_id] = CallConversation(call_id, history_max=self._history_max)
            self.metrics.calls_started += 1
        if conv.pending_notice:  # a failed hand-off brought the caller back: tell them, do not re-greet
            conv.greeting = "played"
            self._launch(conv, "notice")
            return
        if call_id in self._greeted:  # reconnect (or a re-created state): never greet twice
            logger.info("ai_greeting_skipped", extra={"call_id": call_id, "reason": "already_greeted"})
            return
        self._greeted[call_id] = None
        while len(self._greeted) > MAX_GREETED_CALLS:
            self._greeted.popitem(last=False)
        conv.greeting = "started"
        self.metrics.greetings_attempted += 1
        logger.info("ai_greeting_started", extra={"call_id": call_id, "stream_id": session.stream_id})
        self._launch(conv, "greeting")  # non-blocking: inbound audio and STT startup are not delayed

    async def on_stream_end(self, session: Any, reason: str) -> None:
        return None  # the call may reconnect; state is released by on_call_ended / on_call_closed

    async def on_dtmf(self, session: Any, event: Any) -> None:
        return None

    async def on_call_ended(self, call_id: str, reason: str) -> None:
        await self._end_call(call_id, reason)

    # --- transcript subscriber (pipeline) -------------------------------------------------------

    async def on_call_closed(self, event: Any) -> None:
        await self._end_call(event.call_id, "transcript_session_closed")

    async def on_event(self, event: TranscriptEvent) -> None:
        if event.type not in (TRANSCRIPT_FINAL, VAD_SPEECH_START):
            return  # partials and everything else never trigger a response
        conv = self._calls.get(event.call_id)
        if conv is None or conv.closed:
            return  # no live conversation for this call (ended, or no primary stream was ever seen)
        if conv.suspended:
            self.metrics.ignored_events += 1
            return  # a human agent is handling the call: the AI never answers
        if not (event.source == "sarvam" and event.speaker == "customer" and event.track in (None, TRACK_INBOUND)):
            self.metrics.ignored_events += 1
            return  # assistant/outbound audio is never customer input
        if event.type == VAD_SPEECH_START:
            await self._on_speech_start(conv, event)
        else:
            await self._on_final(conv, event)

    async def _on_final(self, conv: CallConversation, event: TranscriptEvent) -> None:
        if conv.seen_before((event.stream_id, event.track, event.sequence)):
            self.metrics.duplicate_finals += 1
            logger.info("ai_duplicate_final_ignored", extra=event.log_fields())
            return
        text = (event.text or "").strip()[:MAX_UTTERANCE_CHARS]
        if not text:
            return
        self.metrics.utterances_processed += 1
        logger.info("ai_utterance_accepted", extra={**event.log_fields(), "phase": conv.phase.value})
        # The customer asked for a person: this is checked first, so neither a queued greeting reply nor the LLM
        # (which would invent a phone number) answers it.
        if self._handoff_trigger is not None:
            try:
                if self._handoff_trigger(conv.call_id, text):
                    return  # hand-off started: the AI does not also answer this sentence
            except Exception as exc:
                logger.warning("ai_handoff_trigger_failed", extra={"call_id": conv.call_id, "error": type(exc).__name__})
        if conv.task_kind == "greeting" and conv.task_active and conv.phase != Phase.PLAYING:
            # The caller spoke (e.g. "hello?") before the greeting reached the line: let the greeting play
            # and answer right after it, instead of cancelling it.
            conv.add("user", text)
            conv.pending_reply = True
            return
        eligible = self._observe_resolution(conv, text)
        await self._interrupt(conv, "new_utterance")
        conv.add("user", text)
        self._launch(conv, "assess" if eligible else "turn")

    def _observe_resolution(self, conv: CallConversation, text: str) -> bool:
        """Count this customer sentence for AI-first routing. True if the call is now eligible for escalation."""
        if self._resolution is None:
            return False
        try:
            seen = self._resolution.observe(conv.call_id, text, conv.turns)
            if seen.signal and self._on_resolution_event is not None:
                self._on_resolution_event(
                    conv.call_id, "ai_resolution_attempt",
                    {"signal": seen.signal, "attempt": seen.attempt, "max_attempts": self._resolution.max_attempts},
                )
            return seen.eligible and self._unresolved_handoff is not None
        except Exception as exc:  # resolution tracking must never break the conversation
            logger.warning("ai_resolution_observe_failed", extra={"call_id": conv.call_id, "error": type(exc).__name__})
            return False

    async def _on_speech_start(self, conv: CallConversation, event: TranscriptEvent) -> None:
        if not self._barge_in or conv.phase != Phase.PLAYING:
            return  # only assistant audio that is being played can be talked over
        if conv.playing_since is not None and self._clock() - conv.playing_since < self._grace_s:
            self.metrics.barge_in_ignored_grace += 1
            return
        self.metrics.interruptions += 1
        conv.interruptions += 1
        logger.info("ai_barge_in_detected", extra={**event.log_fields(), "phase": conv.phase.value})
        await self._interrupt(conv, "barge_in")

    # --- task control ---------------------------------------------------------------------------

    def _launch(self, conv: CallConversation, kind: str) -> None:
        conv.turn_id += 1
        turn_id = conv.turn_id
        conv.task_kind = kind
        run = {"greeting": self._run_greeting, "notice": self._run_notice, "assess": self._run_assess}.get(kind, self._run_turn)
        task = asyncio.create_task(run(conv, turn_id), name=f"ai-conversation:{kind}:{conv.call_id}")
        conv.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _current(self, conv: CallConversation, turn_id: int) -> bool:
        return not conv.closed and conv.turn_id == turn_id

    async def _interrupt(self, conv: CallConversation, reason: str) -> bool:
        """Cancel the in-flight response (if any), invalidate its turn, and clear queued assistant audio."""
        task = conv.task
        if task is None or task.done():
            return False
        was_playing = conv.phase == Phase.PLAYING
        phase = conv.phase.value
        conv.turn_id += 1  # anything the cancelled task still holds is now stale
        conv.phase = Phase.IDLE
        conv.playing_since = None
        task.cancel()
        await asyncio.wait({task}, timeout=CANCEL_WAIT_S)
        self.metrics.tasks_cancelled += 1
        logger.info("ai_task_cancelled", extra={"call_id": conv.call_id, "reason": reason, "phase": phase})
        if conv.task_kind == "greeting" and conv.greeting == "started":
            conv.greeting = "interrupted"
        if was_playing:
            await self._clear_audio(conv, reason)
        return True

    async def _clear_audio(self, conv: CallConversation, reason: str) -> None:
        session = self._get_stream(conv.call_id)
        if session is None or session.closed:
            return
        try:
            await session.playback.clear(wait_confirmed=False)  # the next play() waits for clearedAudio
        except Exception as exc:
            logger.warning("ai_playback_clear_failed", extra={"call_id": conv.call_id, "error": _error_code(exc)})
            return
        self.metrics.playback_cleared += 1
        logger.info(
            "ai_playback_cleared", extra={"call_id": conv.call_id, "stream_id": session.stream_id, "reason": reason}
        )

    async def _end_call(self, call_id: str, reason: str) -> None:
        conv = self._calls.pop(call_id, None)
        if conv is None:
            return
        conv.closed = True
        conv.turn_id += 1
        task = conv.task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=CANCEL_WAIT_S)
            self.metrics.tasks_cancelled += 1
            logger.info("ai_task_cancelled", extra={"call_id": call_id, "reason": reason, "phase": conv.phase.value})
        conv.messages.clear()
        self.metrics.calls_cleaned += 1
        logger.info(
            "ai_conversation_cleaned",
            extra={"call_id": call_id, "reason": reason, "turns": conv.turns, "interruptions": conv.interruptions},
        )

    async def shutdown(self) -> None:
        """Server shutdown: cancel every response task and drop all state."""
        for call_id in list(self._calls):
            await self._end_call(call_id, "shutdown")
        pending = [t for t in self._tasks if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.wait(pending, timeout=CANCEL_WAIT_S)

    # --- response tasks -------------------------------------------------------------------------

    async def _run_greeting(self, conv: CallConversation, turn_id: int) -> None:
        call_id = conv.call_id
        try:
            conv.phase = Phase.SYNTHESIZING
            conv.add("assistant", self._greeting_text)
            self._observe(call_id, self._greeting_text)
            outcome = await self._speak(conv, turn_id, self._greeting_text, kind="greeting")
            if not self._current(conv, turn_id):
                return
            conv.greeting = outcome  # "played" | "interrupted" | "failed"
            if outcome == "failed":
                self.metrics.greetings_failed += 1
                logger.warning("ai_greeting_failed", extra={"call_id": call_id})
            if conv.pending_reply:  # the caller already said something: answer it now
                conv.pending_reply = False
                conv.phase = Phase.IDLE
                self._launch(conv, "turn")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the call must survive any greeting problem
            self._fail(conv, turn_id, "greeting", exc)
        finally:
            self._finish_phase(conv, turn_id)

    async def _run_assess(self, conv: CallConversation, turn_id: int) -> None:
        """The call is eligible for escalation: ask the model for a structured decision (strict schema), then either
        start the transfer or carry on with a normal answer. The customer's sentence is already in the history."""
        call_id = conv.call_id
        started = False
        try:
            conv.phase = Phase.THINKING
            decision = await self._resolution.assess(self._llm, call_id, list(conv.messages))  # type: ignore[union-attr]
            if not self._current(conv, turn_id):
                return
            if decision.escalate:
                started = bool(self._unresolved_handoff and self._unresolved_handoff(call_id, f"ai_unresolved ({decision.reason})"))
                if started:
                    self._resolution.mark_escalated(call_id)  # type: ignore[union-attr]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(conv, turn_id, "assess", exc)
        if started:
            self._finish_phase(conv, turn_id)
            return  # the hand-off service announces and transfers; the AI says nothing more
        await self._run_turn(conv, turn_id)  # no escalation: answer the customer as usual

    async def _run_notice(self, conv: CallConversation, turn_id: int) -> None:
        """Speak one fixed sentence (for example after a failed hand-off). Same path as the greeting."""
        text, conv.pending_notice = conv.pending_notice or "", None
        try:
            if text:
                conv.phase = Phase.SYNTHESIZING
                conv.add("assistant", text)
                self._observe(conv.call_id, text)
                await self._speak(conv, turn_id, text, kind="notice")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(conv, turn_id, "notice", exc)
        finally:
            self._finish_phase(conv, turn_id)

    def _observe(self, call_id: str, text: str) -> None:
        if self._on_assistant_message is None:
            return
        try:
            self._on_assistant_message(call_id, text)
        except Exception as exc:  # reporting must never affect the call
            logger.warning("ai_message_observer_failed", extra={"call_id": call_id, "error": type(exc).__name__})

    async def suspend(self, call_id: str, reason: str) -> bool:
        """A human agent takes over: stop any AI speech now and ignore customer speech from here on.
        Returns False if the call has no AI conversation."""
        conv = self._calls.get(call_id)
        if conv is None or conv.closed:
            return False
        conv.suspended = True
        conv.pending_reply = False
        await self._interrupt(conv, reason)
        logger.info("ai_conversation_suspended", extra={"call_id": call_id, "reason": reason})
        return True

    async def say_and_suspend(self, call_id: str, text: str, timeout_s: float = 8.0) -> bool:
        """Hand-off announcement: stop whatever the AI is doing, say `text` once, wait (bounded) until it was
        played, and from then on stay silent. Customer speech is ignored from the first moment. Returns True if the
        announcement was played; False if it could not be (the call still proceeds with the hand-off)."""
        conv = self._calls.get(call_id)
        if conv is None or conv.closed:
            return False
        await self._interrupt(conv, "handoff")
        conv.suspended = True
        conv.pending_reply = False
        conv.pending_notice = text
        self._launch(conv, "notice")
        task = conv.task
        if task is not None:
            await asyncio.wait({task}, timeout=timeout_s)
            if not task.done():  # TTS or playback is slow: do not hold the customer up, cut it off
                await self._interrupt(conv, "handoff_announcement_timeout")
                return False
        return True

    async def speak_notice(self, call_id: str, text: str) -> bool:
        """Say one fixed sentence now (not suspended). Used when a hand-off is not possible."""
        conv = self._calls.get(call_id)
        if conv is None or conv.closed or conv.suspended:
            return False
        await self._interrupt(conv, "notice")
        conv.pending_notice = text
        self._launch(conv, "notice")
        return True

    def resume(self, call_id: str, notice: str | None = None, *, speak_now: bool = False) -> bool:
        """The hand-off did not happen: the AI takes the call back. `notice` is spoken when its stream returns,
        or right away with `speak_now` (the stream never went away, e.g. the transfer request failed)."""
        conv = self._calls.get(call_id)
        if conv is None or conv.closed:
            return False
        conv.suspended = False
        conv.pending_notice = notice
        if self._resolution is not None:
            self._resolution.reopen(call_id)  # the transfer did not happen: the AI may try again later
        if speak_now and notice:
            self._launch(conv, "notice")
        logger.info("ai_conversation_resumed", extra={"call_id": call_id})
        return True

    async def _run_turn(self, conv: CallConversation, turn_id: int) -> None:
        call_id = conv.call_id
        try:
            conv.phase = Phase.THINKING
            self.metrics.llm_requests += 1
            logger.info("ai_llm_request_started", extra={"call_id": call_id, "history_messages": len(conv.messages)})
            started = self._clock()
            try:
                system = SYSTEM_PROMPT
                if self._knowledge is not None:
                    question = next((m.content for m in reversed(conv.messages) if m.role == "user"), "")
                    extra = await self._knowledge.context_for(call_id, question)
                    if extra:
                        system = f"{SYSTEM_PROMPT}\n{extra}"
                result = await asyncio.wait_for(
                    self._llm.generate(system=system, messages=list(conv.messages)), self._llm_timeout_s
                )
                reply = clean_reply(result.text)
                if not reply:
                    raise LlmError("empty reply after clean-up")
                if contains_contact_details(reply):  # never read out a number the model may have invented
                    self.metrics.contact_details_blocked += 1
                    logger.warning("ai_reply_contact_details_blocked", extra={"call_id": call_id})
                    reply = CONTACT_DETAILS_BLOCKED_REPLY
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                self._llm_failed(call_id, "llm_timeout")
                return
            except Exception as exc:
                self._llm_failed(call_id, _error_code(exc))
                return
            latency_ms = (self._clock() - started) * 1000
            self.metrics.llm_latency.add(latency_ms)
            logger.info(
                "ai_llm_request_completed",
                extra={
                    "call_id": call_id, "latency_ms": round(latency_ms), "reply_chars": len(reply),
                    "input_tokens": getattr(result, "input_tokens", None),
                    "output_tokens": getattr(result, "output_tokens", None),
                    "stop_reason": getattr(result, "stop_reason", None),
                },
            )
            if not self._current(conv, turn_id):
                return
            conv.add("assistant", reply)
            self._observe(call_id, reply)
            conv.turns += 1
            await self._speak(conv, turn_id, reply, kind="turn")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(conv, turn_id, "turn", exc)
        finally:
            self._finish_phase(conv, turn_id)

    def _llm_failed(self, call_id: str, code: str) -> None:
        self.metrics.llm_failures += 1
        self.metrics.conversation_failures += 1
        logger.warning("ai_llm_request_failed", extra={"call_id": call_id, "error": code})

    def _fail(self, conv: CallConversation, turn_id: int, kind: str, exc: BaseException) -> None:
        self.metrics.conversation_failures += 1
        logger.error(
            "ai_conversation_error",
            extra={"call_id": conv.call_id, "kind": kind, "error": type(exc).__name__},
        )

    def _finish_phase(self, conv: CallConversation, turn_id: int) -> None:
        if conv.turn_id == turn_id:  # a newer turn owns the phase otherwise
            conv.phase = Phase.IDLE
            conv.playing_since = None

    async def _synthesize(self, conv: CallConversation, text: str, *, kind: str) -> Any | None:
        call_id = conv.call_id
        if kind == "greeting" and self._greeting_audio is not None:
            logger.info("ai_tts_cache_hit", extra={"call_id": call_id, "kind": kind})
            return self._greeting_audio
        self.metrics.tts_requests += 1
        logger.info("ai_tts_request_started", extra={"call_id": call_id, "kind": kind, "text_chars": len(text)})
        started = self._clock()
        try:
            audio = await asyncio.wait_for(self._tts.synthesize(text), self._tts_timeout_s)
            if not getattr(audio, "pcm", b""):
                raise ValueError("empty audio")
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self._tts_failed(call_id, kind, "tts_timeout")
            return None
        except Exception as exc:
            self._tts_failed(call_id, kind, _error_code(exc))
            return None
        latency_ms = (self._clock() - started) * 1000
        self.metrics.tts_latency.add(latency_ms)
        logger.info(
            "ai_tts_request_completed",
            extra={
                "call_id": call_id, "kind": kind, "latency_ms": round(latency_ms),
                "request_id": getattr(audio, "request_id", None),
            },
        )
        logger.info(
            "ai_audio_converted",
            extra={
                "call_id": call_id, "source_codec": getattr(audio, "source_codec", None),
                "source_sample_rate": getattr(audio, "source_sample_rate", None),
                "playback_format": audio.format.label, "resampled": getattr(audio, "resampled", None),
                "bytes": len(audio.pcm), "duration_ms": round(audio.duration_ms),
            },
        )
        if kind == "greeting":
            self._greeting_audio = audio
        return audio

    def _tts_failed(self, call_id: str, kind: str, code: str) -> None:
        self.metrics.tts_failures += 1
        self.metrics.conversation_failures += 1
        logger.warning("ai_tts_request_failed", extra={"call_id": call_id, "kind": kind, "error": code})

    async def _speak(self, conv: CallConversation, turn_id: int, text: str, *, kind: str) -> str:
        """TTS then playback on the call's current primary stream. Returns "played", "interrupted" or
        "failed" (only the outcome of this call; failures are logged and counted, never raised)."""
        call_id = conv.call_id
        conv.phase = Phase.SYNTHESIZING
        audio = await self._synthesize(conv, text, kind=kind)
        if audio is None:
            return "failed"
        if not self._current(conv, turn_id):
            return "interrupted"  # superseded while synthesizing: stale audio is never played
        session = self._get_stream(call_id)
        if session is None or session.closed:
            self.metrics.playback_failed += 1
            logger.warning("ai_playback_skipped", extra={"call_id": call_id, "kind": kind, "reason": "no_live_stream"})
            return "failed"
        conv.phase = Phase.PLAYING
        conv.playing_since = self._clock()
        try:
            # Two steps so "sent" and "acknowledged" stay distinct: play() returns once every chunk and the
            # checkpoint are written to the socket; wait_for() then waits for Vobiz's playedStream ack.
            result = await session.playback.play(audio.pcm, audio.format)
            extra = {
                "call_id": call_id, "stream_id": session.stream_id, "kind": kind,
                "chunks_sent": result.chunks_sent, "checkpoint": result.checkpoint,
            }
            if result.aborted or result.checkpoint is None:
                self.metrics.playback_cleared += 1
                logger.info("ai_playback_interrupted", extra={**extra, "outcome": CLEARED})
                return "interrupted"
            self.metrics.playback_sent += 1
            if kind == "greeting":
                self.metrics.greetings_sent += 1
            logger.info("ai_playback_sent", extra={**extra, "audio_format": audio.format.label})
            outcome = await session.playback.wait_for(result.checkpoint)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # StreamClosedError, CheckpointLimitError, AudioFormatError, ...
            self.metrics.playback_failed += 1
            logger.warning(
                "ai_playback_failed",
                extra={"call_id": call_id, "stream_id": session.stream_id, "kind": kind, "error": _error_code(exc)},
            )
            return "failed"
        extra = {**extra, "outcome": outcome}
        if outcome == CLEARED:
            self.metrics.playback_cleared += 1
            logger.info("ai_playback_interrupted", extra=extra)
            return "interrupted"
        if outcome == PLAYED:
            self.metrics.playback_acknowledged += 1
            if kind == "greeting":
                self.metrics.greetings_acknowledged += 1
            logger.info("ai_playback_acknowledged", extra=extra)
            return "played"
        if outcome == TIMEOUT:
            self.metrics.playback_timeouts += 1
            logger.warning("ai_playback_unacknowledged", extra=extra)  # sent, but Vobiz never confirmed it
        else:
            self.metrics.playback_failed += 1
            logger.warning("ai_playback_not_confirmed", extra=extra)
        return "played"
