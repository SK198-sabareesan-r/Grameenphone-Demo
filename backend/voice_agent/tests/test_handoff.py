"""AI -> human hand-off, background assist and hold/resume (M3). No Vobiz or Copilot call is ever made."""

from __future__ import annotations

import asyncio
import json
import xml.etree.ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.integrations.vobiz.errors import VobizServerError
from app.integrations.vobiz.xml_builder import build_handoff_xml
from app.main import create_app
from app.services.conversation.manager import ConversationManager
from app.services.voice.handoff import HANDOFF_FAILED_NOTICE, UNAVAILABLE_NOTICE, HandoffError, HandoffService
from tests.ai_fakes import FakeLlm, FakeTts, final, make_stream, settle
from tests.test_vobiz_websocket import CALL, HEADERS, make_settings as base_settings, open_stream, wait_until

HUMAN = "+8801911000999"
BASE = "https://abc.ngrok.app"


def hs(**kw) -> Settings:
    values = dict(
        human_handoff_enabled=True, human_agent_phone=HUMAN, hold_audio_url="https://cdn.example/hold.mp3",
        vobiz_caller_id="+8801700000001",
    )
    values.update(kw)
    return base_settings(**values)


class Rec:
    """Records emitted call events (stands in for the CopilotEventForwarder)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def emit_call_event(self, call_id, kind, data=None) -> None:
        self.events.append((call_id, kind, data or {}))

    def kinds(self) -> list[str]:
        return [k for _, k, _ in self.events]

    def data(self, kind: str) -> dict:
        return next(d for _, k, d in self.events if k == kind)


class FakeVobiz:
    def __init__(self, fail: Exception | None = None) -> None:
        self.requests: list[dict] = []
        self.fail = fail

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def account_path(self, suffix: str) -> str:
        return f"Account/MA_TEST/{suffix}"

    async def request(self, method, path, *, operation, **kw):
        self.requests.append({"method": method, "path": path, "operation": operation, **kw})
        if self.fail is not None and operation == "transfer_call":
            raise self.fail
        return {"api_id": "api-123", "message": "ok"}


class FakeCalls:
    def __init__(self, live=True) -> None:
        self.live = live

    def current_stream(self, call_id):
        return object() if self.live else None


class FakeConversation:
    def __init__(self) -> None:
        self.suspended: list[str] = []
        self.resumed: list[tuple[str, str | None]] = []
        self.said: list[tuple[str, str]] = []  # (call_id, text) spoken to the customer

    async def suspend(self, call_id, reason):
        self.suspended.append(call_id)
        return True

    async def say_and_suspend(self, call_id, text, timeout_s=8.0):
        self.said.append((call_id, text))  # announced first, then silent
        self.suspended.append(call_id)
        return True

    async def speak_notice(self, call_id, text):
        self.said.append((call_id, text))
        return True

    def resume(self, call_id, notice=None, *, speak_now=False):
        self.resumed.append((call_id, notice))
        if speak_now and notice:
            self.said.append((call_id, notice))
        return True


def make_service(settings=None, *, vobiz=None, live=True):
    rec, conv, vobiz = Rec(), FakeConversation(), vobiz or FakeVobiz()
    service = HandoffService(settings or hs(), rec, FakeCalls(live), lambda: conv, client_factory=vobiz)
    return service, rec, conv, vobiz


# ---- request ------------------------------------------------------------------------------------


async def test_request_silences_the_ai_then_asks_vobiz_to_transfer_the_call_leg() -> None:
    service, rec, conv, vobiz = make_service()
    state = await service.request(CALL, reason="refund needs a human", requested_by="ai_agent", pending_actions=["refund"])
    assert state.status == "requested" and conv.suspended == [CALL]
    assert rec.data("human_handoff_requested") == {
        "reason": "refund needs a human", "requested_by": "ai_agent", "pending_actions": ["refund"]}
    transfer = vobiz.requests[0]
    assert (transfer["method"], transfer["path"]) == ("POST", f"Account/MA_TEST/Call/{CALL}/")
    assert transfer["json"] == {"legs": "aleg", "aleg_url": f"{BASE}/api/vobiz/handoff/{CALL}/dial", "aleg_method": "POST"}


@pytest.mark.parametrize("settings,live,status", [
    (hs(human_handoff_enabled=False), True, 503),
    (hs(human_agent_phone=""), True, 503),
    (hs(human_agent_phone="not-a-number"), True, 503),
    (hs(), False, 404),
    (hs(public_base_url=""), True, None),  # a configuration error, raised before the AI is silenced
])
async def test_request_is_refused_before_anything_changes(settings, live, status) -> None:
    service, rec, conv, vobiz = make_service(settings, live=live)
    with pytest.raises(Exception) as caught:
        await service.request(CALL, reason="x")
    if status:
        assert isinstance(caught.value, HandoffError) and caught.value.http_status == status
    assert rec.events == [] and conv.suspended == [] and vobiz.requests == []


async def test_a_second_request_while_one_is_in_progress_is_refused() -> None:
    service, *_ = make_service()
    await service.request(CALL, reason="x")
    with pytest.raises(HandoffError) as caught:
        await service.request(CALL, reason="again")
    assert caught.value.http_status == 409


async def test_a_vobiz_error_reports_the_failure_and_gives_the_call_back_to_the_ai() -> None:
    service, rec, conv, vobiz = make_service(vobiz=FakeVobiz(fail=VobizServerError("boom")))
    with pytest.raises(HandoffError) as caught:
        await service.request(CALL, reason="x")
    assert caught.value.http_status == 502
    assert rec.kinds() == ["human_handoff_requested", "human_handoff_failed"]
    assert conv.resumed == [(CALL, HANDOFF_FAILED_NOTICE)] and conv.said[-1] == (CALL, HANDOFF_FAILED_NOTICE)  # apologises at once: the AI stream never went away
    assert service.state(CALL).status == "failed"
    vobiz.fail = None
    await service.request(CALL, reason="try again")  # a failed hand-off can be retried


# ---- the VobizXML ------------------------------------------------------------------------------


async def test_the_transfer_document_streams_both_tracks_then_dials_the_human() -> None:
    service, rec, *_ = make_service()
    await service.request(CALL, reason="x")
    xml = service.dial_xml(CALL)
    root = ET.fromstring(xml.split("\n", 1)[1])
    stream, dial = list(root)
    assert stream.tag == "Stream" and dial.tag == "Dial"  # the stream comes first and does not hold the call
    assert stream.attrib["audioTrack"] == "both" and stream.attrib["bidirectional"] == "false"
    assert stream.attrib["keepCallAlive"] == "false"
    assert stream.text.startswith("wss://abc.ngrok.app/ws/vobiz/monitor/") and "token=" in stream.text
    assert dial.find("Number").text == HUMAN
    assert dial.attrib["callbackUrl"] == f"{BASE}/api/vobiz/handoff/{CALL}/dial-events"
    assert dial.attrib["action"] == f"{BASE}/api/vobiz/handoff/{CALL}/dial-result"
    assert dial.attrib["digitsMatchBLeg"] == "*1,*2" and dial.attrib["callerId"] == "+8801700000001"
    assert dial.attrib["timeout"] == "30" and dial.attrib["redirect"] == "true"
    assert rec.data("human_agent_dialing") == {"agent_ref": "***0999", "agent_count": 1, "transfer_api_id": "api-123"}  # masked: the number is never reported
    assert service.state(CALL).status == "dialing"


async def test_the_transfer_document_is_only_served_for_a_pending_handoff() -> None:
    service, *_ = make_service()
    with pytest.raises(HandoffError) as caught:
        service.dial_xml(CALL)
    assert caught.value.http_status == 404
    await service.request(CALL, reason="x")
    service.dial_xml(CALL)
    with pytest.raises(HandoffError):
        service.dial_xml(CALL)  # a replay does not dial again


def test_the_xml_builder_rejects_bad_input() -> None:
    kw = dict(number=HUMAN, monitor_ws_url="wss://h/ws", callback_url="https://h/cb", action_url="https://h/act")
    assert "Dial" in build_handoff_xml(**kw)
    with pytest.raises(ValueError):
        build_handoff_xml(**{**kw, "monitor_ws_url": "http://h/ws"})
    with pytest.raises(ValueError):
        build_handoff_xml(**kw, digits_match_bleg=("1;DROP",))
    with pytest.raises(ValueError):
        build_handoff_xml(**kw, content_type="audio/mp3")


# ---- Dial callbacks: connected, digits, hang-up -----------------------------------------------------


async def connected_service(**kw):
    service, rec, conv, vobiz = make_service(**kw)
    await service.request(CALL, reason="x")
    service.dial_xml(CALL)
    assert await service.on_dial_event(CALL, {"DialAction": "connected", "DialBLegUUID": "bleg-1"}) == "connected"
    return service, rec, conv, vobiz


async def test_connected_is_reported_once_and_the_ai_stays_silent() -> None:
    service, rec, conv, _ = await connected_service()
    assert await service.on_dial_event(CALL, {"DialAction": "connected"}) == "duplicate"
    assert rec.kinds().count("human_agent_connected") == 1
    assert conv.resumed == []  # the AI is not given the call back
    assert service.state(CALL).b_leg_uuid == "bleg-1"


async def test_the_human_agent_pressing_the_hold_digits_puts_the_customer_on_hold_with_hold_audio() -> None:
    service, rec, _, vobiz = await connected_service()
    digits = {"DialAction": "digits", "DialDigitsMatch": "*1", "DialDigitsPressedBy": "bleg"}
    assert await service.on_dial_event(CALL, digits) == "held"
    assert rec.data("customer_put_on_hold") == {"source": "human_agent_dtmf", "hold_audio": "playing"}
    play = vobiz.requests[-1]
    assert (play["method"], play["path"]) == ("POST", f"Account/MA_TEST/Call/{CALL}/Play/")
    assert play["json"] == {"urls": "https://cdn.example/hold.mp3", "legs": "aleg", "loop": True, "mix": False}
    assert await service.on_dial_event(CALL, digits) == "ignored"  # pressing hold twice does nothing
    assert service.state(CALL).held


async def test_resume_stops_the_hold_audio_and_restores_the_conversation() -> None:
    service, rec, _, vobiz = await connected_service()
    await service.on_dial_event(CALL, {"DialAction": "digits", "DialDigitsMatch": "*1", "DialDigitsPressedBy": "bleg"})
    resumed = await service.on_dial_event(CALL, {"DialAction": "digits", "DialDigitsMatch": "*2", "DialDigitsPressedBy": "bleg"})
    assert resumed == "resumed" and rec.data("customer_resumed")["hold_audio"] == "stopped"
    assert vobiz.requests[-1]["method"] == "DELETE" and vobiz.requests[-1]["path"].endswith("/Play/")
    assert not service.state(CALL).held


async def test_digits_pressed_by_the_customer_or_unknown_digits_never_change_the_hold_state() -> None:
    service, rec, *_ = await connected_service()
    by_customer = {"DialAction": "digits", "DialDigitsMatch": "*1", "DialDigitsPressedBy": "aleg"}
    assert await service.on_dial_event(CALL, by_customer) == "ignored_customer_digits"
    odd = {"DialAction": "digits", "DialDigitsMatch": "9", "DialDigitsPressedBy": "bleg"}
    assert await service.on_dial_event(CALL, odd) == "ignored_unknown_digits"
    assert "customer_put_on_hold" not in rec.kinds()


async def test_hold_is_not_inferred_before_the_human_is_connected_and_resume_needs_a_hold() -> None:
    service, rec, *_ = make_service()
    await service.request(CALL, reason="x")
    assert not await service.hold(CALL, source="api")  # still ringing
    assert not await service.resume(CALL, source="api")
    assert "customer_put_on_hold" not in rec.kinds()


async def test_hold_without_hold_audio_is_still_recorded_and_says_so() -> None:
    service, rec, *_ = await connected_service(settings=hs(hold_audio_url=""))
    assert await service.hold(CALL, source="api")
    assert rec.data("customer_put_on_hold")["hold_audio"] == "not_configured"


async def test_a_hold_audio_failure_is_reported_honestly_and_does_not_undo_the_hold() -> None:
    service, rec, _, vobiz = await connected_service()

    async def failing(method, path, *, operation, **kw):
        raise VobizServerError("play failed")

    vobiz.request = failing
    assert await service.hold(CALL, source="api")
    assert rec.data("customer_put_on_hold")["hold_audio"] == "failed"


async def test_the_human_hanging_up_is_reported_once_and_ends_any_hold() -> None:
    service, rec, *_ = await connected_service()
    await service.hold(CALL, source="api")
    hangup = {"DialAction": "hangup", "DialBLegDuration": "42"}
    assert await service.on_dial_event(CALL, hangup) == "disconnected"
    assert await service.on_dial_event(CALL, hangup) == "duplicate"
    assert rec.kinds().count("human_agent_disconnected") == 1 and "customer_resumed" not in rec.kinds()
    assert not service.state(CALL).held


async def test_events_for_an_unknown_call_are_ignored() -> None:
    service, rec, *_ = make_service()
    assert await service.on_dial_event("nope", {"DialAction": "connected"}) == "ignored_unknown_call"
    assert rec.events == []


# ---- the human does not answer -------------------------------------------------------------------


@pytest.mark.parametrize("status", ["no-answer", "busy", "failed", "timeout", "cancel"])
async def test_if_the_human_does_not_answer_the_caller_goes_back_to_the_ai(status) -> None:
    service, rec, conv, _ = make_service()
    await service.request(CALL, reason="x")
    service.dial_xml(CALL)
    xml = await service.on_dial_result(CALL, {"DialStatus": status})
    stream = ET.fromstring(xml.split("\n", 1)[1]).find("Stream")
    assert stream is not None and stream.attrib["bidirectional"] == "true" and stream.attrib["audioTrack"] == "inbound"
    assert rec.data("human_handoff_failed") == {"reason": status}
    assert conv.resumed == [(CALL, HANDOFF_FAILED_NOTICE)]
    assert service.state(CALL).status == "failed"


async def test_a_completed_dial_hangs_up_and_an_unknown_call_hangs_up() -> None:
    service, *_ = await connected_service()
    assert "<Hangup" in await service.on_dial_result(CALL, {"DialStatus": "completed"})
    assert "<Hangup" in await service.on_dial_result("nope", {"DialStatus": "no-answer"})


async def test_the_call_ending_during_the_transfer_reports_the_human_leg_as_gone() -> None:
    service, rec, *_ = await connected_service()
    await service.on_call_ended(CALL, "hangup")
    await service.on_call_ended(CALL, "hangup")
    assert rec.kinds().count("human_agent_disconnected") == 1


# ---- customer asks for a human (see test_handoff_intent.py for the wording rules) --------------------------


async def test_asking_for_a_human_announces_then_transfers_and_the_ai_does_not_answer() -> None:
    service, rec, conv, vobiz = make_service()
    assert service.phrase_trigger(CALL, "Can I contact human agent?") is True  # swallowed: no LLM answer
    await settle(lambda: vobiz.requests)
    assert conv.said == [(CALL, "Please hold on. I am connecting you to a human agent.")]  # announced before the transfer
    assert conv.suspended == [CALL]
    assert vobiz.requests[0]["operation"] == "transfer_call"  # the Vobiz transfer really was invoked
    started = rec.data("human_handoff_requested")
    assert started["requested_by"] == "customer" and started["reason"].startswith("customer_asked_for_a_human")


async def test_the_announcement_is_spoken_before_the_transfer_is_requested() -> None:
    order: list[str] = []
    service, rec, conv, vobiz = make_service()
    original = conv.say_and_suspend

    async def say(call_id, text, timeout_s=8.0):
        order.append("announce")
        return await original(call_id, text, timeout_s)

    async def transfer(method, path, *, operation, **kw):
        order.append(operation)
        return {}

    conv.say_and_suspend, vobiz.request = say, transfer
    await service.request(CALL, reason="x")
    assert order == ["announce", "transfer_call"]


async def test_the_customer_hanging_up_during_the_announcement_cancels_the_transfer() -> None:
    service, rec, conv, vobiz = make_service()
    calls = service._calls

    async def say(call_id, text, timeout_s=8.0):
        calls.live = False  # the customer hung up while the announcement was playing
        return True

    conv.say_and_suspend = say
    with pytest.raises(HandoffError):
        await service.request(CALL, reason="x")
    assert vobiz.requests == [] and rec.data("human_handoff_failed") == {"reason": "customer_disconnected"}


async def test_configured_extra_phrases_work_in_addition_to_the_builtin_rules() -> None:
    service, rec, conv, vobiz = make_service(hs(human_handoff_phrases="gp wizard"))
    assert service.phrase_trigger(CALL, "get me the GP wizard") is True
    await settle(lambda: vobiz.requests)


@pytest.mark.parametrize("sentence", [
    "what is the customer care number", "my internet is not working", "I do not want to talk to a human",
    "I called customer care yesterday", "how much does it cost to talk to an agent",
])
async def test_unrelated_sentences_never_start_a_transfer(sentence) -> None:
    service, rec, conv, vobiz = make_service()
    assert service.phrase_trigger(CALL, sentence) is False
    await asyncio.sleep(0.02)
    assert rec.events == [] and vobiz.requests == [] and conv.said == []


async def test_repeating_the_request_does_not_start_a_second_transfer() -> None:
    service, rec, conv, vobiz = make_service()
    assert service.phrase_trigger(CALL, "I want to talk to a human") is True
    assert service.phrase_trigger(CALL, "I want to talk to a human") is True  # same sentence delivered twice
    await settle(lambda: vobiz.requests)
    service.dial_xml(CALL)
    assert service.phrase_trigger(CALL, "connect me to an agent") is True  # asked again while dialing
    await asyncio.sleep(0.02)
    assert len(vobiz.requests) == 1 and rec.kinds().count("human_handoff_requested") == 1


async def test_asking_again_after_a_failed_transfer_tries_again() -> None:
    clock = {"t": 0.0}
    service, rec, conv, vobiz = make_service()
    service._clock = lambda: clock["t"]
    assert service.phrase_trigger(CALL, "transfer me to an agent")
    await settle(lambda: vobiz.requests)
    service.dial_xml(CALL)
    await service.on_dial_result(CALL, {"DialStatus": "no-answer"})  # the human did not answer
    clock["t"] = 100.0
    assert service.phrase_trigger(CALL, "transfer me to an agent")
    await settle(lambda: len(vobiz.requests) == 2)


@pytest.mark.parametrize("settings,why", [
    (hs(human_handoff_enabled=False), "handoff_disabled"),
    (hs(human_agent_phone=""), "no_agent_number"),
    (hs(human_agent_phone="12345"), "invalid_agent_number"),
    (hs(human_agent_phone=",".join(["+8801911000990"] * 6)), "invalid_agent_number"),
])
async def test_when_no_transfer_is_possible_the_customer_is_told_honestly_and_it_is_recorded(settings, why) -> None:
    service, rec, conv, vobiz = make_service(settings)
    assert service.phrase_trigger(CALL, "I want to speak to a human agent") is True  # still intercepted: no invented numbers
    await settle(lambda: conv.said)
    assert conv.said == [(CALL, UNAVAILABLE_NOTICE)] and vobiz.requests == []
    assert rec.kinds() == ["human_handoff_requested", "human_handoff_failed"]
    assert rec.data("human_handoff_failed")["reason"].startswith("unavailable:")
    assert service.state(CALL).status == "failed" and service.unavailable_reason() == why


async def test_several_agent_numbers_all_ring_and_only_the_first_is_reported_masked() -> None:
    service, rec, *_ = make_service(hs(human_agent_phone="+8801911000999, +8801811000123"))
    await service.request(CALL, reason="x")
    root = ET.fromstring(service.dial_xml(CALL).split("\n", 1)[1])
    assert [n.text for n in root.find("Dial").findall("Number")] == ["+8801911000999", "+8801811000123"]
    assert rec.data("human_agent_dialing") == {"agent_ref": "***0999", "agent_count": 2, "transfer_api_id": "api-123"}
    assert "+8801811000123" not in json.dumps(rec.events)


def test_describe_never_exposes_a_full_number() -> None:
    service, *_ = make_service()
    info = service.describe()
    assert info["enabled"] is True and info["agent_numbers"] == ["***0999"] and HUMAN not in json.dumps(info)
    assert make_service(hs(human_agent_phone=""))[0].describe()["unavailable_reason"] == "no_agent_number"


# ---- the AI conversation obeys the hand-off -----------------------------------------------------------


class Harness:
    def __init__(self) -> None:
        self.llm, self.tts, self.said = FakeLlm(), FakeTts(), []
        self.streams: dict = {}
        self.mgr = ConversationManager(
            llm=self.llm, tts=self.tts, get_stream=self.streams.get, barge_in_grace_ms=0,
            llm_timeout_s=2.0, tts_timeout_s=2.0, on_assistant_message=lambda c, t: self.said.append((c, t)))


async def test_a_suspended_conversation_ignores_customer_speech_and_never_speaks() -> None:
    h = Harness()
    session, wire = make_stream(CALL)
    h.streams[CALL] = session
    await h.mgr.on_stream_start(session)
    await settle(lambda: wire.of("checkpoint"))  # the greeting is on the wire
    assert [c for c, _ in h.said] == [CALL]  # the greeting was reported as an AI message
    assert await h.mgr.suspend(CALL, "handoff")
    before = len(wire.messages)
    await h.mgr.on_event(final(CALL, "hello? are you there", 1))
    await asyncio.sleep(0.05)
    assert h.llm.calls == [] and len(wire.messages) == before  # nothing was asked, nothing was said
    assert h.mgr.conversation(CALL).suspended


async def test_suspending_interrupts_a_reply_that_is_being_prepared() -> None:
    h = Harness()
    session, wire = make_stream(CALL)
    h.streams[CALL] = session
    h.llm.gate = asyncio.Event()  # the LLM is "thinking"
    await h.mgr.on_stream_start(session)
    await settle(lambda: wire.of("checkpoint"))
    session.playback.handle_played_stream(wire.of("checkpoint")[0]["name"])  # the greeting finished playing
    await settle(lambda: h.mgr.conversation(CALL).phase.value == "idle")
    await h.mgr.on_event(final(CALL, "my balance please", 1))
    await settle(lambda: h.llm.calls)
    await h.mgr.suspend(CALL, "handoff")
    h.llm.gate.set()
    await asyncio.sleep(0.05)
    assert h.llm.cancelled == 1
    assert all(t != "Reply to my balance please" for _, t in h.said)  # the stale reply never reached the caller


async def test_a_failed_handoff_lets_the_ai_say_so_when_its_stream_returns() -> None:
    h = Harness()
    first, _ = make_stream(CALL, "s1")
    h.streams[CALL] = first
    await h.mgr.on_stream_start(first)
    await h.mgr.suspend(CALL, "handoff")
    assert h.mgr.resume(CALL, HANDOFF_FAILED_NOTICE)
    second, wire2 = make_stream(CALL, "s2")  # Vobiz re-attached the AI stream
    h.streams[CALL] = second
    await h.mgr.on_stream_start(second)
    await settle(lambda: wire2.of("checkpoint"))
    assert h.tts.texts[-1] == HANDOFF_FAILED_NOTICE and (CALL, HANDOFF_FAILED_NOTICE) in h.said
    assert h.tts.texts.count(h.tts.texts[0]) == 1  # no second greeting


async def test_the_handoff_trigger_swallows_the_sentence_but_a_broken_trigger_does_not_break_the_call() -> None:
    h = Harness()
    session, _ = make_stream(CALL)
    h.streams[CALL] = session
    h.mgr._handoff_trigger = lambda call, text: True
    await h.mgr.on_stream_start(session)
    await h.mgr.on_event(final(CALL, "give me a human", 1))
    await asyncio.sleep(0.05)
    assert h.llm.calls == []
    h.mgr._handoff_trigger = lambda call, text: (_ for _ in ()).throw(RuntimeError("boom"))
    await h.mgr.on_event(final(CALL, "my balance", 2))
    await settle(lambda: h.llm.calls)


# ---- HTTP routes (real app, fake Vobiz and fake Copilot) --------------------------------------------


def app_with(settings, copilot_requests: list):
    async def copilot(request: httpx.Request) -> httpx.Response:
        copilot_requests.append(json.loads(request.content))
        return httpx.Response(201, json={"status": "stored"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(copilot))
    settings = settings.model_copy(update={
        "copilot_transcript_webhook_url": "http://copilot.test/api/v1/internal/transcripts/events"})
    app = create_app(settings, copilot_http_client=client, conversation_llm=FakeLlm(), conversation_tts=FakeTts(),
                     audio_sink_factory=None)
    return app


def test_control_routes_need_the_internal_token_and_hand_off_is_off_by_default() -> None:
    app = app_with(base_settings(), [])
    with TestClient(app) as client:
        for method, path in [("post", f"/api/vobiz/calls/{CALL}/handoff"), ("get", f"/api/vobiz/calls/{CALL}/handoff"),
                             ("post", f"/api/vobiz/calls/{CALL}/hold"), ("post", f"/api/vobiz/calls/{CALL}/resume")]:
            body = {"json": {"reason": "x"}} if method == "post" else {}
            assert getattr(client, method)(path, **body).status_code == 401
        assert client.post(f"/api/vobiz/calls/{CALL}/handoff", json={"reason": "x"}, headers=HEADERS).status_code == 503
        assert client.get(f"/api/vobiz/calls/{CALL}/handoff", headers=HEADERS).status_code == 404
        assert client.post(f"/api/vobiz/calls/{CALL}/hold", headers=HEADERS).status_code == 409


def test_vobiz_facing_handoff_routes_enforce_the_webhook_signature() -> None:
    app = app_with(hs(vobiz_validate_signature=True), [])
    with TestClient(app) as client:
        for tail in ("dial", "dial-events", "dial-result"):
            assert client.post(f"/api/vobiz/handoff/{CALL}/{tail}").status_code == 403


def test_full_handoff_over_http_reaches_the_copilot_with_ordered_events() -> None:
    sent: list[dict] = []
    app = app_with(hs(), sent)
    vobiz = FakeVobiz()
    with TestClient(app) as client:
        app.state.handoff._client_factory = vobiz
        with open_stream(client):
            r = client.post(f"/api/vobiz/calls/{CALL}/handoff", json={"reason": "refund", "pending_actions": ["refund"]}, headers=HEADERS)
            assert r.status_code == 202 and r.json()["status"] == "requested"
            assert client.post(f"/api/vobiz/calls/{CALL}/handoff", json={"reason": "again"}, headers=HEADERS).status_code == 409
            xml = client.post(f"/api/vobiz/handoff/{CALL}/dial")
            assert xml.status_code == 200 and "<Dial" in xml.text and "audioTrack=\"both\"" in xml.text
            post = lambda d: client.post(f"/api/vobiz/handoff/{CALL}/dial-events", data=d)
            assert post({"DialAction": "connected", "DialBLegUUID": "b1"}).json()["outcome"] == "connected"
            assert post({"DialAction": "digits", "DialDigitsMatch": "*1", "DialDigitsPressedBy": "bleg"}).json()["outcome"] == "held"
            assert post({"DialAction": "digits", "DialDigitsMatch": "*2", "DialDigitsPressedBy": "bleg"}).json()["outcome"] == "resumed"
            assert client.get(f"/api/vobiz/calls/{CALL}/handoff", headers=HEADERS).json()["status"] == "connected"
        wait_until(lambda: any(b.get("type") == "customer_resumed" for b in sent))
    kinds = [b["type"] for b in sent if "type" in b]
    expected = ["human_handoff_requested", "human_agent_dialing", "human_agent_connected", "customer_put_on_hold", "customer_resumed"]
    assert [k for k in kinds if k in expected] == expected  # the Copilot receives them in the order they happened
    assert HUMAN not in json.dumps(sent)  # the human agent's number never leaves this service


# ---- the real conversation manager + the real hand-off service (fake LLM, TTS, Vobiz) ------------------------


class Live:
    """The failing scenario from the first real call: the customer asks for a human agent."""

    def __init__(self, settings=None, vobiz=None) -> None:
        self.rec, self.vobiz = Rec(), vobiz or FakeVobiz()
        self.llm, self.tts = FakeLlm(), FakeTts()
        self.streams: dict = {}
        self.service = HandoffService(settings or hs(), self.rec, FakeCalls(), lambda: self.mgr, client_factory=self.vobiz)
        self.mgr = ConversationManager(
            llm=self.llm, tts=self.tts, get_stream=self.streams.get, barge_in_grace_ms=0, llm_timeout_s=2.0,
            tts_timeout_s=2.0, handoff_trigger=self.service.phrase_trigger)

    async def start(self):
        self.session, self.wire = make_stream(CALL)
        self.streams[CALL] = self.session
        await self.mgr.on_stream_start(self.session)
        await self.ack()  # the greeting finishes playing
        await settle(lambda: self.mgr.conversation(CALL).phase.value == "idle")

    async def ack(self) -> None:
        await settle(lambda: any(m["name"] not in self.wire.acked for m in self.wire.of("checkpoint")))
        name = next(m["name"] for m in self.wire.of("checkpoint") if m["name"] not in self.wire.acked)
        self.wire.acked.add(name)
        self.session.playback.handle_played_stream(name)


async def test_a_customer_asking_for_a_human_gets_the_announcement_then_the_transfer_not_an_llm_answer() -> None:
    live = Live()
    await live.start()
    await live.mgr.on_event(final(CALL, "Can I contact human agent?", 1))
    await live.ack()  # the announcement is played to the customer
    await settle(lambda: live.vobiz.requests)
    assert live.llm.calls == []  # the model never saw the sentence, so it cannot answer "call 121"
    assert live.tts.texts[-1] == "Please hold on. I am connecting you to a human agent."
    assert live.vobiz.requests[0]["operation"] == "transfer_call"
    assert live.rec.kinds() == ["human_handoff_requested"]
    assert live.mgr.conversation(CALL).suspended
    # the human is dialled; from now on the AI is silent whatever the customer says
    sent = len(live.wire.messages)
    await live.mgr.on_event(final(CALL, "hello? hello?", 2))
    await asyncio.sleep(0.05)
    assert live.llm.calls == [] and len(live.wire.messages) == sent


async def test_the_customer_is_not_left_hanging_if_the_transfer_request_is_refused() -> None:
    live = Live(vobiz=FakeVobiz(fail=VobizServerError("boom")))
    await live.start()
    await live.mgr.on_event(final(CALL, "transfer me to an agent", 1))
    await live.ack()  # announcement
    await settle(lambda: live.rec.kinds().count("human_handoff_failed") == 1)
    await live.ack()  # the apology, spoken at once on the same stream
    assert live.tts.texts[-1] == HANDOFF_FAILED_NOTICE
    assert not live.mgr.conversation(CALL).suspended  # the AI has the call again
    await live.mgr.on_event(final(CALL, "what is a data pack", 2))
    await settle(lambda: live.llm.calls)  # and answers normally


async def test_without_a_configured_agent_number_the_customer_is_told_the_truth_not_a_made_up_number() -> None:
    live = Live(hs(human_agent_phone=""))
    await live.start()
    await live.mgr.on_event(final(CALL, "I want to speak to a human", 1))
    await live.ack()
    assert live.llm.calls == []
    assert live.tts.texts[-1] == UNAVAILABLE_NOTICE
    assert live.vobiz.requests == []
    assert live.rec.kinds() == ["human_handoff_requested", "human_handoff_failed"]
    await live.mgr.on_event(final(CALL, "ok what is a data pack", 2))
    await settle(lambda: live.llm.calls)  # the AI continues to help with everything else


async def test_a_normal_question_still_goes_to_the_llm() -> None:
    live = Live()
    await live.start()
    await live.mgr.on_event(final(CALL, "what is the customer care number", 1))
    await settle(lambda: live.llm.calls)
    assert live.vobiz.requests == [] and live.rec.events == []

async def test_the_transfer_and_the_dial_are_traceable_by_vobiz_identifiers() -> None:
    service, rec, *_ = make_service()
    state = await service.request(CALL, reason="x")
    assert state.transfer_api_id == "api-123"  # the Transfer API's own request id
    service.dial_xml(CALL)
    await service.on_dial_event(CALL, {"DialAction": "connected", "DialBLegUUID": "bleg-77"})
    assert rec.data("human_agent_dialing")["transfer_api_id"] == "api-123"
    assert rec.data("human_agent_connected")["b_leg_uuid"] == "bleg-77"
    assert service.state(CALL).view()["b_leg_uuid"] == "bleg-77"


async def test_the_real_missed_sentence_starts_a_transfer_end_to_end() -> None:
    """From the first real call: a negation in the PREVIOUS sentence hid the request, the LLM answered "call 121"."""
    live = Live()
    await live.start()
    await live.mgr.on_event(final(CALL, "I tried too many times, it's not working. So can I contact a human agent?", 1))
    await live.ack()
    await settle(lambda: live.vobiz.requests)
    assert live.llm.calls == [] and live.vobiz.requests[0]["operation"] == "transfer_call"


async def test_the_model_cannot_read_out_a_phone_number_even_if_it_tries() -> None:
    from app.services.conversation.prompts import CONTACT_DETAILS_BLOCKED_REPLY

    live = Live()
    live.llm.reply_fn = lambda messages: "You can call 121 or 01711594594 for help."
    await live.start()
    await live.mgr.on_event(final(CALL, "my network is slow", 1))
    await live.ack()
    assert live.tts.texts[-1] == CONTACT_DETAILS_BLOCKED_REPLY  # the numbers never reached text-to-speech
    assert live.mgr.metrics.contact_details_blocked == 1 and "121" not in " ".join(live.tts.texts)
