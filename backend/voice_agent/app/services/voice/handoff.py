"""AI -> human hand-off, hold and resume for one phone call (M3).

How it works (Vobiz facts are from the Vobiz docs, see README "Human hand-off"):

1. `request()` suspends the AI conversation (it will not speak or answer any more), then calls the Vobiz
   Transfer API on the call's A-leg with our own `/handoff/{call}/dial` URL.
2. Vobiz fetches that URL and gets VobizXML that (a) forks BOTH audio tracks to the one-way monitor
   WebSocket, so Sarvam keeps transcribing and the Copilot keeps listening, and (b) `<Dial>`s the human
   agent's phone. Customer and human then talk directly over the phone network. The AI never plays audio.
3. Vobiz reports the B-leg through `callbackUrl` (answered, connected, digits, hangup) and the final result
   through `action`. These real events become the Copilot lifecycle events. If the human does not answer,
   `action` gets the XML that puts the caller back on the AI stream and the AI says so.

Hold and resume use only real signals, never silence:

* the human agent presses `HOLD_DTMF` / `RESUME_DTMF` on their own phone (Vobiz reports B-leg digits), or
* someone calls `POST /api/vobiz/calls/{id}/hold` / `/resume` (internal token).

Vobiz does not expose the handset's own hold button, so that cannot be detected (see README).
While on hold the configured hold audio (if any) is played to the caller with the Vobiz Play API.

Logs and Copilot events carry masked phone numbers only (last 4 digits).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from app.core.config import Settings
from app.integrations.vobiz.client import VobizClient
from app.integrations.vobiz.errors import VobizConfigError, VobizError
from app.integrations.vobiz.phone import mask_number, normalize_e164
from app.integrations.vobiz.stream_token import issue_stream_token, token_to_header_safe
from app.integrations.vobiz.urls import https_to_wss, join_public_url, validate_public_https_url
from app.services.conversation.handoff_intent import detect_human_request
from app.integrations.vobiz.xml_builder import (
    build_bidirectional_stream_xml,
    build_handoff_xml,
    build_hangup_xml,
)

logger = logging.getLogger(__name__)

REQUESTED, DIALING, CONNECTED, FAILED, COMPLETED = "requested", "dialing", "connected", "failed", "completed"
ACTIVE_STATES = (REQUESTED, DIALING, CONNECTED)
HANDOFF_FAILED_NOTICE = "Sorry, I could not reach a colleague right now. I am still here. How else can I help you?"
MAX_STATES = 1000
MAX_AGENT_NUMBERS = 5
TRIGGER_COOLDOWN_S = 5.0  # one spoken request can arrive as two final transcripts; one transfer is enough
UNAVAILABLE_NOTICE = (
    "I am sorry, I cannot connect you to a human agent right now. I can still help with general questions."
)


class HandoffError(Exception):
    def __init__(self, message: str, http_status: int = 409) -> None:
        super().__init__(message)
        self.http_status = http_status


@dataclass
class HandoffState:
    call_id: str
    status: str = REQUESTED
    reason: str = ""
    requested_by: str = ""
    requested_at: str = ""
    held: bool = False
    b_leg_uuid: str | None = None
    transfer_api_id: str | None = None  # Vobiz request id of the Transfer API call, for support and log search
    failure: str | None = None
    hangup_sent: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def view(self) -> dict[str, object]:
        return {
            "call_id": self.call_id, "status": self.status, "reason": self.reason, "requested_by": self.requested_by,
            "requested_at": self.requested_at, "held": self.held, "failure": self.failure,
            "transfer_api_id": self.transfer_api_id, "b_leg_uuid": self.b_leg_uuid,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class HandoffService:
    def __init__(
        self, settings: Settings, forwarder: Any, call_manager: Any, get_conversation, *, client_factory=None,
        clock=time.monotonic,
    ) -> None:
        self._s = settings
        self._forwarder = forwarder
        self._calls = call_manager
        self._get_conversation = get_conversation  # () -> ConversationManager | None
        self._client_factory = client_factory or (lambda: VobizClient(settings))
        self._states: dict[str, HandoffState] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._clock = clock
        self._last_trigger: dict[str, float] = {}
        self._hold_patterns = tuple(p for p in (settings.hold_dtmf, settings.resume_dtmf) if p)

    # ---- configuration ----------------------------------------------------------------------------

    def numbers(self) -> list[str]:
        """The configured destinations (HUMAN_AGENT_PHONE, comma separated, E.164). Raises HandoffError 503
        when none are usable; the message names the setting, never a number."""
        raw = [p.strip() for p in self._s.human_agent_phone.split(",") if p.strip()]
        if not raw:
            raise HandoffError("HUMAN_AGENT_PHONE is not set", 503)
        if len(raw) > MAX_AGENT_NUMBERS:
            raise HandoffError(f"HUMAN_AGENT_PHONE lists more than {MAX_AGENT_NUMBERS} numbers", 503)
        try:
            return [normalize_e164(p) for p in raw]
        except ValueError as exc:
            raise HandoffError("HUMAN_AGENT_PHONE contains a number that is not valid E.164", 503) from exc

    def unavailable_reason(self) -> str | None:
        """None when a transfer can be attempted, else a short machine-readable reason (safe to log)."""
        if not self._s.human_handoff_enabled:
            return "handoff_disabled"
        if not self._s.explicit_handoff_allowed:
            return "mode_disabled"  # HUMAN_HANDOFF_MODE=disabled
        try:
            self.numbers()
        except HandoffError as exc:
            return "no_agent_number" if "not set" in str(exc) else "invalid_agent_number"
        return None

    @property
    def enabled(self) -> bool:
        return self.unavailable_reason() is None

    def auto_available(self) -> bool:
        """May the AI start a transfer on its own (unresolved call)? Needs AI_AUTO_HANDOFF_ON_UNRESOLVED, a mode that
        allows it, and a usable agent number."""
        return self._s.auto_handoff_allowed and self.enabled

    def unresolved_trigger(self, call_id: str, reason: str) -> bool:
        """ConversationManager hook for AI-first escalation: the structured assessment said the AI cannot resolve this.
        Returns True if a transfer was started. Never blocks; refuses duplicates and anything not allowed by config."""
        if not self.auto_available():
            return False
        state = self._states.get(call_id)
        if state is not None and state.status in ACTIVE_STATES:
            return False
        now = self._clock()
        if now - self._last_trigger.get(call_id, -1e9) < TRIGGER_COOLDOWN_S:
            return False
        self._last_trigger[call_id] = now
        logger.info("handoff_requested_unresolved", extra={"call_id": call_id, "reason": reason})
        task = asyncio.create_task(self._handle_unresolved(call_id, reason), name=f"handoff-auto:{call_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _handle_unresolved(self, call_id: str, reason: str) -> None:
        try:
            await self.request(call_id, reason=reason, requested_by="ai_unresolved")
        except HandoffError as exc:
            logger.warning("handoff_unresolved_not_started", extra={"call_id": call_id, "status": exc.http_status})
            conversation = self._get_conversation()
            if conversation is not None and exc.http_status in (503, 404, 409):  # nothing was announced: AI keeps the call
                conversation.resume(call_id, None)
        except Exception as exc:
            logger.error("handoff_unresolved_failed", extra={"call_id": call_id, "error": type(exc).__name__})

    def describe(self) -> dict[str, object]:
        """For startup logs and diagnostics. Phone numbers appear only as their last 4 digits."""
        reason = self.unavailable_reason()
        masked: list[str] = []
        if reason is None:
            masked = [mask_number(n) for n in self.numbers()]
        return {
            "enabled": reason is None, "unavailable_reason": reason, "agent_numbers": masked,
            "phrases_configured": len(self._s.handoff_phrase_list),
            "announcement": bool(self._s.human_handoff_announcement), "hold_audio": bool(self._s.hold_audio_url),
            "mode": self._s.human_handoff_mode, "auto_on_unresolved": self.auto_available(),
            "max_resolution_attempts": self._s.ai_max_resolution_attempts,
            "background_assist": self._s.ai_background_assist_enabled,
        }

    def state(self, call_id: str) -> HandoffState | None:
        return self._states.get(call_id)

    # ---- trigger from customer speech -------------------------------------------------------------

    def phrase_trigger(self, call_id: str, text: str) -> bool:
        """ConversationManager hook, called for every final customer sentence BEFORE the LLM sees it.

        Returns True when the customer asked for a person (built-in intent rules plus HUMAN_HANDOFF_PHRASES). The
        AI then does not answer that sentence: a transfer is started, or, if none is possible, the customer is
        told so honestly. Only the customer's own words are examined. Never blocks.
        """
        found = detect_human_request(text, self._s.handoff_phrase_list)
        if not found.matched:
            return False
        state = self._states.get(call_id)
        now = self._clock()
        if (state is not None and state.status in ACTIVE_STATES) or now - self._last_trigger.get(call_id, -1e9) < TRIGGER_COOLDOWN_S:
            logger.info("handoff_request_ignored_duplicate", extra={"call_id": call_id, "rule": found.rule})
            return True  # swallowed: no second transfer, and the AI does not answer with a made-up number
        self._last_trigger[call_id] = now
        if len(self._last_trigger) > MAX_STATES:
            self._last_trigger.pop(next(iter(self._last_trigger)))
        logger.info("handoff_requested_by_customer", extra={"call_id": call_id, "rule": found.rule})
        task = asyncio.create_task(self._handle_customer_request(call_id, found.rule), name=f"handoff:{call_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _handle_customer_request(self, call_id: str, rule: str) -> None:
        reason = f"customer_asked_for_a_human ({rule})"
        try:
            await self.request(call_id, reason=reason, requested_by="customer")
        except HandoffError as exc:
            if exc.http_status == 503:  # not configured or not enabled: say so, record it, never invent a number
                await self._record_unavailable(call_id, reason, str(exc))
            else:  # 404 no live stream, 409 already running, 502 Vobiz refused (already recorded by request())
                logger.warning("handoff_trigger_not_started", extra={"call_id": call_id, "status": exc.http_status})
        except Exception as exc:
            logger.error("handoff_trigger_failed", extra={"call_id": call_id, "error": type(exc).__name__})

    async def _record_unavailable(self, call_id: str, reason: str, why: str) -> None:
        """The customer asked for a human but a transfer cannot be attempted. Record that, tell the customer."""
        state = self._states[call_id] = HandoffState(
            call_id=call_id, status=FAILED, reason=reason[:500], requested_by="customer", requested_at=_now(),
            failure=f"unavailable: {why}"[:200],
        )
        self._forwarder.emit_call_event(
            call_id, "human_handoff_requested", {"reason": state.reason, "requested_by": "customer", "pending_actions": []}
        )
        self._forwarder.emit_call_event(call_id, "human_handoff_failed", {"reason": state.failure})
        conversation = self._get_conversation()
        if conversation is not None:
            await conversation.speak_notice(call_id, UNAVAILABLE_NOTICE)

    # ---- request ----------------------------------------------------------------------------------

    async def request(
        self, call_id: str, *, reason: str, requested_by: str = "ai_agent", pending_actions: list[str] | None = None
    ) -> HandoffState:
        why = self.unavailable_reason()
        if why is not None:
            raise HandoffError(f"Human hand-off is not available ({why}). Set HUMAN_HANDOFF_ENABLED and HUMAN_AGENT_PHONE", 503)
        existing = self._states.get(call_id)
        if existing is not None and existing.status in ACTIVE_STATES:
            raise HandoffError("A hand-off is already in progress for this call", 409)
        if self._calls.current_stream(call_id) is None:
            raise HandoffError("No live AI stream for this call", 404)
        base = validate_public_https_url(self._s.public_base_url)  # fails before the AI is silenced

        if len(self._states) >= MAX_STATES:
            self._states.pop(next(iter(self._states)))
        state = self._states[call_id] = HandoffState(
            call_id=call_id, reason=reason[:500], requested_by=requested_by, requested_at=_now()
        )
        self._forwarder.emit_call_event(
            call_id, "human_handoff_requested",
            {"reason": state.reason, "requested_by": requested_by, "pending_actions": pending_actions or []},
        )
        conversation = self._get_conversation()
        if conversation is not None:
            # Tell the customer, wait (bounded) until it was said, then stay silent: the AI never talks over the human.
            announcement = self._s.human_handoff_announcement
            if announcement:
                await conversation.say_and_suspend(call_id, announcement, timeout_s=self._s.human_handoff_announcement_timeout_s)
            else:
                await conversation.suspend(call_id, "handoff")
        if self._calls.current_stream(call_id) is None:  # the customer hung up while we were speaking
            await self._fail(state, "customer_disconnected", announce=False)
            raise HandoffError("The customer disconnected before the transfer", 409)

        dial_url = join_public_url(base, f"/api/vobiz/handoff/{quote(call_id, safe='')}/dial")
        try:
            async with self._client_factory() as client:
                response = await client.request(
                    "POST",
                    client.account_path(f"Call/{quote(call_id, safe='')}/"),
                    operation="transfer_call",
                    json={"legs": "aleg", "aleg_url": dial_url, "aleg_method": "POST"},
                )
        except (VobizError, VobizConfigError) as exc:
            # The customer is still on the AI stream: apologise right away and give the call back to the AI.
            await self._fail(state, f"transfer_api_failed:{type(exc).__name__}", announce=True, speak_now=True)
            raise HandoffError(f"Vobiz refused the transfer: {type(exc).__name__}", 502) from exc
        state.transfer_api_id = str((response or {}).get("api_id") or "")[:64] or None
        logger.info(
            "handoff_transfer_accepted",  # Vobiz accepted the Transfer API request (it has not dialled yet)
            extra={"call_id": call_id, "requested_by": requested_by, "api_id": state.transfer_api_id,
                   "vobiz_message": str((response or {}).get("message") or "")[:100]},
        )
        return state
    # ---- XML served to Vobiz ----------------------------------------------------------------------

    def _stream_ws_url(self, base: str, call_id: str, path: str) -> tuple[str, dict[str, str] | None]:
        token = issue_stream_token(self._s.vobiz_auth_token.get_secret_value(), call_id)
        ws_base = https_to_wss(join_public_url(base, f"{path.rstrip('/')}/{quote(call_id, safe='')}"))
        if self._s.vobiz_ws_auth_mode == "start_event":
            return ws_base, {"token": token_to_header_safe(token)}
        return f"{ws_base}?token={token}", None

    def dial_xml(self, call_id: str) -> str:
        """The transfer document Vobiz fetches. Only served for a call whose hand-off we requested."""
        state = self._states.get(call_id)
        if state is None or state.status != REQUESTED:
            raise HandoffError("No hand-off is waiting for this call", 404)
        base = validate_public_https_url(self._s.public_base_url)
        if self._s.ai_background_assist_enabled:  # fork both audio tracks so the AI can keep transcribing and assisting
            monitor_url, headers = self._stream_ws_url(base, call_id, self._s.vobiz_monitor_ws_path)
        else:
            monitor_url, headers = None, None
        numbers = self.numbers()
        prefix = f"/api/vobiz/handoff/{quote(call_id, safe='')}"
        xml = build_handoff_xml(
            number=numbers,
            monitor_ws_url=monitor_url,
            callback_url=join_public_url(base, f"{prefix}/dial-events"),
            action_url=join_public_url(base, f"{prefix}/dial-result"),
            content_type=self._s.vobiz_inbound_content_type,
            timeout_s=self._s.human_handoff_dial_timeout_s,
            caller_id=self._s.vobiz_caller_id or None,
            digits_match_bleg=self._hold_patterns,
            stream_extra_headers=headers,
        )
        state.status = DIALING
        self._forwarder.emit_call_event(
            call_id, "human_agent_dialing",
            {"agent_ref": mask_number(numbers[0]), "agent_count": len(numbers), "transfer_api_id": state.transfer_api_id},
        )
        logger.info("handoff_dial_document_served", extra={"call_id": call_id, "agents": len(numbers)})
        return xml

    def _return_to_ai_xml(self, call_id: str) -> str:
        base = validate_public_https_url(self._s.public_base_url)
        ws_url, headers = self._stream_ws_url(base, call_id, self._s.vobiz_stream_ws_path)
        return build_bidirectional_stream_xml(ws_url, content_type=self._s.vobiz_inbound_content_type, extra_headers=headers)

    # ---- Vobiz callbacks --------------------------------------------------------------------------

    async def on_dial_event(self, call_id: str, payload: dict[str, str]) -> str:
        """`callbackUrl`: B-leg lifecycle. Returns what was done (for logs and tests)."""
        state = self._states.get(call_id)
        if state is None:
            return "ignored_unknown_call"
        action = (payload.get("DialAction") or payload.get("Event") or "").lower()
        if payload.get("DialBLegUUID"):
            state.b_leg_uuid = payload["DialBLegUUID"]
        if action in ("connected", "dialconnected"):
            if state.status == CONNECTED:
                return "duplicate"
            state.status = CONNECTED
            self._forwarder.emit_call_event(
                call_id, "human_agent_connected", {"agent_ref": mask_number(self.numbers()[0]), "b_leg_uuid": state.b_leg_uuid}
            )
            logger.info("handoff_human_connected", extra={"call_id": call_id, "b_leg_uuid": state.b_leg_uuid})
            return "connected"
        if action in ("digits", "dialdigitsmatch"):
            if (payload.get("DialDigitsPressedBy") or "").lower() != "bleg":
                return "ignored_customer_digits"  # only the human agent's keypad controls hold
            digits = payload.get("DialDigitsMatch", "")
            if digits and digits == self._s.hold_dtmf:
                return "held" if await self.hold(call_id, source="human_agent_dtmf") else "ignored"
            if digits and digits == self._s.resume_dtmf:
                return "resumed" if await self.resume(call_id, source="human_agent_dtmf") else "ignored"
            return "ignored_unknown_digits"
        if action in ("hangup", "dialhangup"):
            if state.hangup_sent:
                return "duplicate"
            state.hangup_sent = True
            if state.held:
                await self.resume(call_id, source="human_agent_hangup", emit=False)
            self._forwarder.emit_call_event(
                call_id, "human_agent_disconnected",
                {"duration_s": payload.get("DialBLegDuration"), "hangup_cause": payload.get("DialBLegHangupCauseName") or payload.get("DialHangupCause")},
            )
            return "disconnected"
        return "ignored"

    async def on_dial_result(self, call_id: str, payload: dict[str, str]) -> str:
        """`action`: the final result. Returns the XML Vobiz runs next."""
        state = self._states.get(call_id)
        status = (payload.get("DialStatus") or "").lower()
        if state is None:
            return build_hangup_xml()
        if status == "completed":
            state.status = COMPLETED
            return build_hangup_xml()  # the bridged conversation ended normally
        # busy / no-answer / timeout / failed / cancel: the human never took the call.
        await self._fail(state, status or "unknown", announce=True)
        try:
            return self._return_to_ai_xml(call_id)
        except Exception as exc:
            logger.error("handoff_return_to_ai_failed", extra={"call_id": call_id, "error": type(exc).__name__})
            return build_hangup_xml()

    async def _fail(self, state: HandoffState, why: str, *, announce: bool, speak_now: bool = False) -> None:
        state.status = FAILED
        state.failure = why
        self._forwarder.emit_call_event(state.call_id, "human_handoff_failed", {"reason": why})
        conversation = self._get_conversation()
        if conversation is not None:
            conversation.resume(state.call_id, HANDOFF_FAILED_NOTICE if announce else None, speak_now=speak_now)

    # ---- hold / resume ----------------------------------------------------------------------------

    async def hold(self, call_id: str, *, source: str) -> bool:
        state = self._states.get(call_id)
        if state is None or state.status != CONNECTED or state.held:
            return False
        state.held = True
        audio = await self._hold_audio(call_id, play=True)
        self._forwarder.emit_call_event(call_id, "customer_put_on_hold", {"source": source, "hold_audio": audio})
        return True

    async def resume(self, call_id: str, *, source: str, emit: bool = True) -> bool:
        state = self._states.get(call_id)
        if state is None or not state.held:
            return False
        state.held = False
        audio = await self._hold_audio(call_id, play=False)
        if emit:
            self._forwarder.emit_call_event(call_id, "customer_resumed", {"source": source, "hold_audio": audio})
        return True

    async def _hold_audio(self, call_id: str, *, play: bool) -> str:
        """Play (or stop) the hold audio for the caller. Returns what happened, honestly."""
        url = self._s.hold_audio_url
        if not url:
            return "not_configured"
        try:
            async with self._client_factory() as client:
                path = client.account_path(f"Call/{quote(call_id, safe='')}/Play/")
                if play:  # mix=false: the caller hears the audio, not the agent's side conversation
                    await client.request("POST", path, operation="hold_audio_play",
                                         json={"urls": url, "legs": "aleg", "loop": True, "mix": False})
                else:
                    await client.request("DELETE", path, operation="hold_audio_stop")
            return "playing" if play else "stopped"
        except Exception as exc:
            logger.warning("handoff_hold_audio_failed", extra={"call_id": call_id, "error": type(exc).__name__})
            return "failed"

    # ---- end of call ------------------------------------------------------------------------------

    async def on_call_ended(self, call_id: str, reason: str) -> None:
        state = self._states.get(call_id)
        if state is not None and state.status in (CONNECTED, DIALING) and not state.hangup_sent:
            state.hangup_sent = True
            self._forwarder.emit_call_event(call_id, "human_agent_disconnected", {"hangup_cause": "call_ended"})
        # keep the final state briefly for GET /handoff; it is bounded by MAX_STATES

    # ---- listener plumbing (CallStateManager) -----------------------------------------------------

    async def on_stream_start(self, session: Any) -> None:
        return None

    async def on_stream_end(self, session: Any, reason: str) -> None:
        return None

    async def on_dtmf(self, session: Any, event: Any) -> None:
        return None
