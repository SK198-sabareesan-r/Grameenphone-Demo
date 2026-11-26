"""AI-first call routing (M3.3): the AI answers every call; a human only on request or when the AI cannot resolve it.

Everything external is faked: Vobiz (transfer / dial), the LLM (Bedrock) and TTS (Sarvam). No paid call is made.
"""

from __future__ import annotations

import asyncio
import json
import xml.etree.ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import RoutingConfigError, Settings, _ai_enabled
from app.main import create_app
from app.services.conversation.manager import ConversationManager
from app.services.conversation.prompts import GREETING_TEXT
from app.services.conversation.resolution import (
    ASSESSMENT_SYSTEM_PROMPT,
    CAPABILITY_GAP,
    UNRESOLVED,
    ResolutionPolicy,
    classify_customer_sentence,
    parse_decision,
)
from app.services.llm.base import LlmResult
from app.services.voice.handoff import HANDOFF_FAILED_NOTICE, HandoffService
from tests.ai_fakes import FakeLlm, FakeTts, final, make_stream, settle
from tests.test_handoff import CALL, FakeCalls, FakeVobiz, HUMAN, Rec, hs
from tests.test_ai_conversation_app import app_with_fakes, null_factory
from tests.test_vobiz_websocket import HEADERS, make_settings, open_stream, wait_until

ESCALATE = '{"escalate": true, "reason": "troubleshooting_exhausted", "next_step_available": false}'
CONTINUE = '{"escalate": false, "reason": "other_step_available", "next_step_available": true}'


class ScriptedLlm(FakeLlm):
    """Answers the escalation assessment with `assessment`, and every customer turn with a normal reply."""

    def __init__(self, assessment: str = ESCALATE) -> None:
        super().__init__()
        self.assessment = assessment
        self.assessments = 0
        self.turn_replies = 0

    async def generate(self, *, system, messages):
        if system == ASSESSMENT_SYSTEM_PROMPT:
            self.assessments += 1
            return LlmResult(text=self.assessment, latency_ms=1.0, input_tokens=1, output_tokens=1)
        self.turn_replies += 1
        return await super().generate(system=system, messages=messages)


class Call:
    """One AI-first call: the real conversation manager, resolution policy and hand-off service."""

    def __init__(self, settings: Settings | None = None, llm: ScriptedLlm | None = None, vobiz: FakeVobiz | None = None) -> None:
        self.settings = settings or hs()
        self.rec, self.vobiz, self.llm, self.tts = Rec(), vobiz or FakeVobiz(), llm or ScriptedLlm(), FakeTts()
        self.streams: dict = {}
        self.policy = ResolutionPolicy(
            max_attempts=self.settings.ai_max_resolution_attempts, auto_handoff=self.settings.auto_handoff_allowed)
        self.service = HandoffService(self.settings, self.rec, FakeCalls(), lambda: self.mgr, client_factory=self.vobiz)
        self.mgr = ConversationManager(
            llm=self.llm, tts=self.tts, get_stream=self.streams.get, barge_in_grace_ms=0, llm_timeout_s=2.0,
            tts_timeout_s=2.0, handoff_trigger=self.service.phrase_trigger, resolution=self.policy,
            unresolved_handoff=self.service.unresolved_trigger, on_resolution_event=self.rec.emit_call_event)
        self.seq = 0

    async def start(self) -> None:
        self.session, self.wire = make_stream(CALL)
        self.streams[CALL] = self.session
        await self.mgr.on_stream_start(self.session)
        await self.ack()  # the greeting
        await settle(lambda: self.mgr.conversation(CALL).phase.value == "idle")

    async def ack(self) -> None:
        await settle(lambda: any(m["name"] not in self.wire.acked for m in self.wire.of("checkpoint")))
        name = next(m["name"] for m in self.wire.of("checkpoint") if m["name"] not in self.wire.acked)
        self.wire.acked.add(name)
        self.session.playback.handle_played_stream(name)

    async def customer_says(self, text: str, *, wait_reply: bool = True) -> None:
        self.seq += 1
        before = self.llm.turn_replies
        await self.mgr.on_event(final(CALL, text, self.seq))
        if wait_reply:
            await settle(lambda: self.llm.turn_replies > before)
            await self.ack()
            await settle(lambda: self.mgr.conversation(CALL).phase.value == "idle")

    def transfers(self) -> int:
        return sum(1 for r in self.vobiz.requests if r["operation"] == "transfer_call")


# ---- 1-2. a new call starts with the AI and never dials the human ------------------------------------------------


def test_an_incoming_call_is_answered_by_the_ai_and_the_human_number_is_not_dialled() -> None:
    app, llm, tts = app_with_fakes(human_handoff_enabled=True, human_agent_phone=HUMAN, vobiz_caller_id="+8801700000001")
    vobiz = FakeVobiz()
    with TestClient(app) as client:
        app.state.handoff._client_factory = vobiz
        answer = client.post("/api/vobiz/answer", data={
            "CallUUID": CALL, "From": "+8801700123456", "To": "+8809600000000", "Direction": "inbound",
            "Event": "StartApp", "CallStatus": "in-progress"})
        assert answer.status_code == 200
        root = ET.fromstring(answer.text.split("\n", 1)[1])
        assert [el.tag for el in root] == ["Stream"]  # one bidirectional AI stream: no <Dial> anywhere
        assert root.find("Stream").attrib["bidirectional"] == "true"
        assert HUMAN not in answer.text and "Dial" not in answer.text
        with open_stream(client):
            wait_until(lambda: tts.texts)
            assert tts.texts[0] == GREETING_TEXT  # the AI greeting is never bypassed
            assert app.state.handoff.state(CALL) is None  # no hand-off state exists at the start of a call
    assert vobiz.requests == []  # the Vobiz API (transfer, dial) was never called


@pytest.mark.parametrize("mode", ["ai_first"])
def test_every_new_call_is_reported_to_the_copilot_in_ai_agent_mode(mode) -> None:
    sent: list[dict] = []

    async def copilot(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(201)

    settings = make_settings(
        copilot_transcript_webhook_url="http://copilot.test/api/v1/internal/transcripts/events", call_routing_mode=mode,
        ai_conversation_enabled=True, sarvam_api_key=SecretStr("k"))
    app = create_app(settings, copilot_http_client=httpx.AsyncClient(transport=httpx.MockTransport(copilot)),
                     conversation_llm=FakeLlm(), conversation_tts=FakeTts(), audio_sink_factory=null_factory)
    with TestClient(app) as client:
        client.post("/api/vobiz/answer", data={"CallUUID": CALL, "From": "+8801700123456", "To": "+8809600000000",
                                               "Direction": "inbound", "Event": "StartApp", "CallStatus": "in-progress"})
        with open_stream(client):
            wait_until(lambda: any(b.get("type") == "ai_agent_connected" for b in sent))
    kinds = [b["type"] for b in sent if "type" in b]
    assert kinds[0] == "call_started" and "ai_agent_connected" in kinds
    assert not {"human_handoff_requested", "human_agent_dialing", "human_agent_connected"} & set(kinds)


# ---- 3-4. ordinary problems are handled by the AI ----------------------------------------------------------------


async def test_the_ai_answers_an_ordinary_network_problem_without_a_transfer() -> None:
    call = Call()
    await call.start()
    await call.customer_says("my network is too slow")
    assert call.llm.turn_replies == 1 and call.transfers() == 0 and call.rec.events == []  # reporting a problem is not a signal


async def test_a_solved_problem_is_not_transferred_and_is_recorded_as_resolved() -> None:
    call = Call()
    await call.start()
    await call.customer_says("my network is too slow")
    await call.customer_says("thank you, it worked")
    assert call.transfers() == 0 and call.rec.events == []
    assert call.policy.outcome(CALL, None) == "ai_resolved"


async def test_one_failed_tip_does_not_send_the_customer_to_a_human() -> None:
    call = Call()
    await call.start()
    await call.customer_says("my network is too slow")
    await call.customer_says("I tried that, it is still not working")
    assert call.llm.assessments == 0 and call.transfers() == 0  # attempt 1 of 2: the AI gets another go
    assert call.rec.data("ai_resolution_attempt") == {"signal": UNRESOLVED, "attempt": 1, "max_attempts": 2}
    assert call.policy.outcome(CALL, None) == "unresolved"  # if the customer hung up now, it was not resolved


# ---- 5. explicit request: immediate transfer ---------------------------------------------------------------------


async def test_an_explicit_request_for_a_human_transfers_immediately_even_with_auto_escalation_off() -> None:
    call = Call(hs(ai_auto_handoff_on_unresolved=False))
    await call.start()
    await call.customer_says("my network is too slow")
    await call.mgr.on_event(final(CALL, "Can I talk to a human agent?", 99))
    await call.ack()
    await settle(lambda: call.transfers() == 1)
    assert call.rec.data("human_handoff_requested")["requested_by"] == "customer" and call.llm.assessments == 0


# ---- 6-7. unresolved calls: transfer only when configured --------------------------------------------------------


async def unresolved_conversation(call: Call) -> None:
    await call.start()
    await call.customer_says("my network is too slow")
    await call.customer_says("I tried that, it is still not working")
    await call.customer_says("same problem, nothing changed", wait_reply=False)


async def test_an_unresolved_call_is_transferred_after_the_configured_attempts_when_enabled() -> None:
    call = Call()
    await unresolved_conversation(call)
    await call.ack()  # the announcement
    await settle(lambda: call.transfers() == 1)
    assert call.llm.assessments == 1  # the structured decision was asked for once, with the strict schema
    requested = call.rec.data("human_handoff_requested")
    assert requested["requested_by"] == "ai_unresolved" and requested["reason"].startswith("ai_unresolved (troubleshooting_exhausted")
    assert [d["attempt"] for _, k, d in call.rec.events if k == "ai_resolution_attempt"] == [1, 2]
    assert call.tts.texts[-1] == "Please hold on. I am connecting you to customer support."
    assert call.mgr.conversation(CALL).suspended


@pytest.mark.parametrize("overrides", [
    {"ai_auto_handoff_on_unresolved": False},
    {"human_handoff_mode": "on_request"},
    {"human_handoff_enabled": False},
])
async def test_a_disabled_auto_escalation_never_transfers_an_unresolved_call(overrides) -> None:
    call = Call(hs(**overrides))
    await unresolved_conversation(call)
    await settle(lambda: call.llm.turn_replies == 3)  # the AI keeps answering
    await call.ack()
    assert call.transfers() == 0 and call.llm.assessments == 0 and "human_handoff_requested" not in call.rec.kinds()
    assert not call.mgr.conversation(CALL).suspended


async def test_the_model_saying_another_step_exists_keeps_the_call_with_the_ai_once() -> None:
    call = Call(llm=ScriptedLlm(CONTINUE))
    await unresolved_conversation(call)
    await settle(lambda: call.llm.turn_replies == 3)
    await call.ack()
    assert call.llm.assessments == 1 and call.transfers() == 0
    await call.customer_says("still not working, same issue", wait_reply=False)  # eligible again
    await call.ack()
    await settle(lambda: call.transfers() == 1)  # no second question to the model: it cannot loop forever


@pytest.mark.parametrize("answer", ["", "sure, transfer them", '{"escalate": true}', '{"escalate": true, "reason": "needs_action", "next_step_available": true}', "not json {"])
async def test_an_invalid_model_decision_is_never_trusted_the_eligible_call_escalates(answer) -> None:
    assert parse_decision(answer) is None  # strict schema: free text and half-valid JSON are rejected
    call = Call(llm=ScriptedLlm(answer))
    await unresolved_conversation(call)
    await call.ack()
    await settle(lambda: call.transfers() == 1)


async def test_asking_for_something_the_ai_cannot_do_escalates_after_the_assessment() -> None:
    call = Call()
    await call.start()
    await call.customer_says("My network is too slow, so I want to raise a ticket for it.", wait_reply=False)
    await call.ack()
    await settle(lambda: call.transfers() == 1)
    assert call.rec.data("ai_resolution_attempt")["signal"] == CAPABILITY_GAP
    assert call.rec.data("human_handoff_requested")["requested_by"] == "ai_unresolved"


# ---- 8-9. no answer returns to the AI; a connected human is never talked over -----------------------------------


async def test_when_the_human_does_not_answer_the_ai_takes_the_call_back_and_can_escalate_again() -> None:
    call = Call()
    await unresolved_conversation(call)
    await call.ack()
    await settle(lambda: call.transfers() == 1)
    call.service.dial_xml(CALL)
    xml = await call.service.on_dial_result(CALL, {"DialStatus": "no-answer"})
    assert 'bidirectional="true"' in xml  # the caller is put back on the AI stream
    assert call.rec.data("human_handoff_failed") == {"reason": "no-answer"}
    conv = call.mgr.conversation(CALL)
    assert not conv.suspended and conv.pending_notice == HANDOFF_FAILED_NOTICE
    await call.customer_says("hello?")  # and the AI answers again
    assert call.llm.turn_replies == 3


async def test_a_connected_human_is_never_talked_over() -> None:
    call = Call()
    await unresolved_conversation(call)
    await call.ack()
    await settle(lambda: call.transfers() == 1)
    call.service.dial_xml(CALL)
    await call.service.on_dial_event(CALL, {"DialAction": "connected", "DialBLegUUID": "b1"})
    sent, replies = len(call.wire.messages), call.llm.turn_replies
    for i, sentence in enumerate(["hello ma'am", "my network is still not working", "can you check my bill"]):
        await call.mgr.on_event(final(CALL, sentence, 200 + i))
    await asyncio.sleep(0.05)
    assert call.llm.turn_replies == replies and len(call.wire.messages) == sent and call.llm.assessments == 1
    assert call.policy.outcome(CALL, call.service.state(CALL).status) == "human_handled"


# ---- background assist switch ------------------------------------------------------------------------------------


async def test_the_transfer_document_forks_the_audio_only_when_background_assist_is_on() -> None:
    for assist, expected in ((True, ["Stream", "Dial"]), (False, ["Dial"])):
        call = Call(hs(ai_background_assist_enabled=assist))
        await call.service.request(CALL, reason="x")
        root = ET.fromstring(call.service.dial_xml(CALL).split("\n", 1)[1])
        assert [el.tag for el in root] == expected


# ---- classification: only the customer's words, only real signals ------------------------------------------------


@pytest.mark.parametrize("text,replies,expected", [
    ("my network is too slow", 0, None),
    ("my network is too slow", 1, None),
    ("I tried that but it is still not working", 1, UNRESOLVED),
    ("I tried that but it is still not working", 0, None),  # nothing was suggested yet
    ("same problem", 2, UNRESOLVED),
    ("are you still there", 1, None),
    ("what is my balance", 0, CAPABILITY_GAP),
    ("please check my bill", 1, CAPABILITY_GAP),
    ("I want to raise a ticket", 0, CAPABILITY_GAP),
    ("what is a data pack", 1, None),
    ("thanks, that worked, it is not slow now", 1, None),
])
def test_only_unresolved_reports_and_capability_gaps_count(text, replies, expected) -> None:
    assert classify_customer_sentence(text, replies) == expected


# ---- startup validation ------------------------------------------------------------------------------------------


def test_the_example_configuration_is_valid_and_ai_first() -> None:
    s = Settings(call_routing_mode="ai_first", ai_conversation_enabled=True, ai_agent_auto_answer=True,
                 human_handoff_enabled=True, human_handoff_mode="on_request_or_unresolved", human_agent_phone=HUMAN,
                 ai_auto_handoff_on_unresolved=True, ai_max_resolution_attempts=2, ai_background_assist_enabled=True)
    assert s.routing_problems() == ([], [])
    assert s.explicit_handoff_allowed and s.auto_handoff_allowed


@pytest.mark.parametrize("overrides,fragment", [
    ({"call_routing_mode": "human_first"}, "CALL_ROUTING_MODE must be one of"),
    ({"human_handoff_mode": "always"}, "HUMAN_HANDOFF_MODE must be one of"),
    ({"ai_max_resolution_attempts": 0}, "AI_MAX_RESOLUTION_ATTEMPTS"),
    ({"ai_max_resolution_attempts": 9}, "AI_MAX_RESOLUTION_ATTEMPTS"),
    ({"ai_agent_auto_answer": False}, "AI_AGENT_AUTO_ANSWER=true"),
    ({"call_routing_mode_explicit": True, "ai_conversation_enabled": False}, "AI_AGENT_ENABLED=true"),
])
def test_contradictory_or_unknown_routing_settings_stop_startup(overrides, fragment) -> None:
    settings = Settings(**{"ai_conversation_enabled": True, **overrides})
    errors, _ = settings.routing_problems()
    assert any(fragment in e for e in errors), errors
    with pytest.raises(RoutingConfigError):
        create_app(settings)


def test_risky_but_workable_settings_only_warn() -> None:
    _, warnings = Settings(
        ai_conversation_enabled=True, human_handoff_enabled=True, human_agent_phone="", ai_background_assist_enabled=False,
        human_handoff_mode="on_request", ai_auto_handoff_on_unresolved=True).routing_problems()
    text = " | ".join(warnings)
    assert "HUMAN_AGENT_PHONE is empty" in text and "AI_BACKGROUND_ASSIST_ENABLED=false" in text and "ignored because HUMAN_HANDOFF_MODE=on_request" in text
    assert HUMAN not in text


def test_ai_agent_enabled_is_the_same_setting_as_ai_conversation_enabled() -> None:
    assert _ai_enabled({"AI_AGENT_ENABLED": "true"}) is True
    assert _ai_enabled({"AI_CONVERSATION_ENABLED": "true"}) is True
    assert _ai_enabled({"AI_AGENT_ENABLED": "true", "AI_CONVERSATION_ENABLED": "true"}) is True
    assert _ai_enabled({}) is False
    with pytest.raises(RoutingConfigError):
        _ai_enabled({"AI_AGENT_ENABLED": "true", "AI_CONVERSATION_ENABLED": "false"})


def test_handoff_mode_disabled_means_even_an_explicit_request_is_not_transferred() -> None:
    service = HandoffService(hs(human_handoff_mode="disabled"), Rec(), FakeCalls(), lambda: None, client_factory=FakeVobiz())
    assert service.unavailable_reason() == "mode_disabled" and not service.enabled
