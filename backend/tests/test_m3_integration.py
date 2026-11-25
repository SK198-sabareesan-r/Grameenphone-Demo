"""M3 end to end, in one process: the REAL voice-agent forwarder and hand-off service talk to the REAL Copilot
app (over an in-memory ASGI transport), then the JSON files and the panel events are checked.

Only the outside world is faked: Vobiz (transfer / hold-audio calls) and the phone network. The voice agent's
code is imported from backend/voice_agent; nothing here calls Vobiz, Sarvam or AWS.
"""

import json
import sys
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from config.settings import BASE_DIR
from copilot.app_factory import create_app
from tests.conftest import INTERNAL_TOKEN

sys.path.append(str(BASE_DIR / "voice_agent"))  # makes the voice agent's `app` package importable

from app.core.config import Settings as VoiceSettings  # noqa: E402
from app.publishers.copilot_forwarder import CopilotCallListener, CopilotEventForwarder  # noqa: E402
from app.publishers.transcripts import TRANSCRIPT_FINAL, TRANSCRIPT_PARTIAL, TranscriptEvent  # noqa: E402
from app.services.voice.handoff import HandoffService  # noqa: E402

CALL = "5401fd2e-6344-40df-a22c-c8ffea7a92e7"
HUMAN_PHONE = "+8801911000999"
CALLER = "+8801200000001"


class FakeVobiz:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    def account_path(self, suffix):
        return f"Account/MA_TEST/{suffix}"

    async def request(self, method, path, *, operation, **kw):
        self.requests.append((method, path))
        return {}


class FakeConversation:
    def __init__(self) -> None:
        self.suspended = False

    async def suspend(self, call_id, reason):
        self.suspended = True
        return True

    async def say_and_suspend(self, call_id, text, timeout_s=8.0):
        self.suspended = True
        return True

    def resume(self, call_id, notice=None, *, speak_now=False):
        self.suspended = False
        return True


def stt(kind, seq, text, *, track=None, stream="s1", utter=None):
    return TranscriptEvent(
        call_id=CALL, stream_id=stream, sequence=seq, type=kind, received_at="2026-11-24T10:30:00Z", text=text,
        track=track, utterance_idx=utter if utter is not None else seq, is_final=kind == TRANSCRIPT_FINAL,
    )


@pytest.fixture
def wired(settings, container, fake_bedrock, events):
    """A Copilot app (container injected, no lifespan) and a voice-agent side wired to it."""
    app = create_app(settings, bedrock=fake_bedrock, serve_frontend=False)
    app.state.container = container
    transport = httpx.ASGITransport(app=app)

    def build(**over):
        vs = VoiceSettings(
            copilot_transcript_webhook_url="http://copilot.test/api/v1/internal/transcripts/events",
            internal_api_token=SecretStr(INTERNAL_TOKEN), copilot_forward_backoff_s=0.01,
            human_handoff_enabled=True, human_agent_phone=HUMAN_PHONE, public_base_url="https://voice.example.com",
            vobiz_auth_id="MA_TEST", vobiz_auth_token=SecretStr("x"), hold_audio_url="https://cdn.example/hold.mp3", **over,
        )
        client = httpx.AsyncClient(transport=transport, base_url="http://copilot.test")
        forwarder = CopilotEventForwarder(vs, client=client)
        conversation = FakeConversation()
        vobiz = FakeVobiz()
        handoff = HandoffService(vs, forwarder, SimpleNamespace(current_stream=lambda c: object()), lambda: conversation,
                                 client_factory=vobiz)
        return SimpleNamespace(forwarder=forwarder, handoff=handoff, conversation=conversation, vobiz=vobiz,
                               listener=CopilotCallListener(forwarder, conversation_enabled=True))

    return SimpleNamespace(app=app, build=build, container=container, events=events, settings=settings)


def files(settings, call=CALL):
    folder = settings.conversations_dir / call
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in folder.glob("*.json")}


async def run_call(v, *, outbound_is_human: bool):
    """The whole scenario from the task: AI call, customer speaks, AI answers, hand-off, human talks, hold, resume, end."""
    f, h = v.forwarder, v.handoff
    f.emit_call_event(CALL, "call_started", {"from_number": CALLER, "direction": "inbound"})
    await v.listener.on_stream_start(SimpleNamespace(kind="primary", call_id=CALL))
    await f.on_event(stt(TRANSCRIPT_PARTIAL, 1, "I need help"))
    await f.on_event(stt(TRANSCRIPT_FINAL, 2, "I need help with my mobile recharge.", utter=0))
    f.emit_ai_message(CALL, "I can help with that. Which pack did you buy?")
    await f.on_event(stt(TRANSCRIPT_FINAL, 3, "I want a refund, let me talk to a person.", utter=1))
    await h.request(CALL, reason="Customer asks for a refund the AI cannot approve", pending_actions=["Approve refund"])
    h.dial_xml(CALL)  # Vobiz fetched the transfer document
    await h.on_dial_event(CALL, {"DialAction": "connected", "DialBLegUUID": "bleg-1"})
    # After the transfer, both tracks of the monitored stream are transcribed.
    await f.on_event(stt(TRANSCRIPT_FINAL, 1, "Hello, this is Tania from Grameenphone.", track="track_outbound", stream="mon", utter=0))
    await f.on_event(stt(TRANSCRIPT_PARTIAL, 2, "Hi, about my", track="track_inbound", stream="mon"))
    await f.on_event(stt(TRANSCRIPT_FINAL, 3, "Hi, about my refund.", track="track_inbound", stream="mon", utter=1))
    await h.on_dial_event(CALL, {"DialAction": "digits", "DialDigitsMatch": "*1", "DialDigitsPressedBy": "bleg"})
    await h.on_dial_event(CALL, {"DialAction": "digits", "DialDigitsMatch": "*2", "DialDigitsPressedBy": "bleg"})
    await h.on_dial_event(CALL, {"DialAction": "hangup", "DialBLegDuration": "30"})
    await v.listener.on_call_ended(CALL, "hangup")  # also drains the forwarder


async def test_the_whole_call_is_stored_in_json_and_shown_on_the_panel(wired):
    v = wired.build(vobiz_outbound_track_speaker="human_agent")  # as if verified on a live call
    await run_call(v, outbound_is_human=True)
    stored = files(wired.settings)
    assert set(stored) == {"transcript", "customer_context", "ai_agent_context", "human_agent_context", "call_events"}

    # transcript.json: partial and final events, in order, with the right speaker and mode
    events = stored["transcript"]["events"]
    assert [e["sequence_number"] for e in events] == list(range(1, len(events) + 1))
    got = [(e["speaker"], e["transcript_type"], e["agent_mode"], e["text"]) for e in events]
    assert got == [
        ("customer", "partial", "ai_agent", "I need help"),
        ("customer", "final", "ai_agent", "I need help with my mobile recharge."),
        ("ai_agent", "final", "ai_agent", "I can help with that. Which pack did you buy?"),
        ("customer", "final", "ai_agent", "I want a refund, let me talk to a person."),
        ("human_agent", "final", "human_agent", "Hello, this is Tania from Grameenphone."),
        ("customer", "partial", "human_agent", "Hi, about my"),
        ("customer", "final", "human_agent", "Hi, about my refund."),
    ]
    assert len({e["event_id"] for e in events}) == len(events)
    assert stored["transcript"]["session_id"] == f"sess-{CALL}" and stored["transcript"]["customer_id"] == "rahim"

    # the three contexts hold their own side only, tied together by call, session and customer
    customer, ai, human = (stored[n] for n in ("customer_context", "ai_agent_context", "human_agent_context"))
    for doc in (customer, ai, human, stored["call_events"]):
        assert (doc["call_id"], doc["session_id"], doc["customer_id"]) == (CALL, f"sess-{CALL}", "rahim")
    assert customer["initial_intent"] == "I need help with my mobile recharge." and customer["caller_ref"] == "***0001"
    assert len(customer["conversation_history_refs"]) == 3  # three customer finals
    assert [m["text"] for m in ai["messages"]] == ["I can help with that. Which pack did you buy?"]
    assert ai["handoff"]["reason"].startswith("Customer asks for a refund") and ai["handoff"]["requested_at"]
    assert ai["handoff"]["pending_actions"] == ["Approve refund"]
    assert human["transcript_refs"] == [events[4]["event_id"]]
    assert human["agent_ref"] == "***0999" and human["handoff"]["status"] == "completed"
    assert human["handoff"]["accepted_at"] and human["handoff"]["completed_at"] and human["assist_activated_at"]
    assert [h["type"] for h in human["hold_events"]] == ["customer_put_on_hold", "customer_resumed"]
    assert all(h["source"] == "human_agent_dtmf" for h in human["hold_events"])

    # call_events.json: the lifecycle in order
    assert [e["type"] for e in stored["call_events"]["events"]] == [
        "call_started", "ai_agent_connected", "human_handoff_requested", "human_agent_dialing",
        "human_agent_connected", "ai_agent_assist_activated", "customer_put_on_hold", "customer_resumed",
        "human_agent_disconnected", "call_ended",
    ]

    # the customer's own number is kept as it was (in customer_context only). No token or agent phone is stored.
    everything = json.dumps(stored)
    for secret in (HUMAN_PHONE, INTERNAL_TOKEN):
        assert secret not in everything
    assert customer['caller_number'] == CALLER
    assert CALLER not in json.dumps({k: v for k, v in stored.items() if k != 'customer_context'})

    # the panel saw the same call, live
    statuses = [d["status"] for d in wired.events.of("call.status")]
    assert statuses == ["in_progress", "handoff_requested", "dialing_human", "with_human",
                        "on_hold", "with_human", "human_disconnected"]
    assert [d["agent_mode"] for d in wired.events.of("call.status")][-1] == "human_agent"
    turns = wired.container.registry.current.turns
    assert [t.speaker for t in turns] == ["customer", "ai_agent", "customer", "human_agent", "customer"]
    assert wired.container.registry.current.ended_at is not None
    assert v.conversation.suspended  # the AI never got the call back and never spoke after the hand-off


async def test_without_a_verified_outbound_track_the_human_line_is_stored_as_unknown_not_guessed(wired):
    v = wired.build()  # default: outbound track speaker is "unknown"
    await run_call(v, outbound_is_human=False)
    events = files(wired.settings)["transcript"]["events"]
    mystery = [e for e in events if e["text"].startswith("Hello, this is Tania")][0]
    assert (mystery["speaker"], mystery["speaker_basis"]) == ("unknown", "outbound_track_unverified")
    assert files(wired.settings)["human_agent_context"]["transcript_refs"] == []  # not credited to the human agent
    assert [t.speaker for t in wired.container.registry.current.turns][3] == "unknown"


async def test_a_copilot_outage_during_the_call_loses_nothing_that_fits_in_the_retry_budget(wired, monkeypatch):
    v = wired.build()
    real = httpx.ASGITransport.handle_async_request
    state = {"down": True, "calls": 0}

    async def flaky(self, request):
        state["calls"] += 1
        if state["down"] and state["calls"] <= 3:
            return httpx.Response(503)
        return await real(self, request)

    monkeypatch.setattr(httpx.ASGITransport, "handle_async_request", flaky)
    await v.forwarder.on_event(stt(TRANSCRIPT_FINAL, 1, "first line", utter=0))
    await v.forwarder.on_event(stt(TRANSCRIPT_FINAL, 2, "second line", utter=1))
    await v.forwarder.finish_call(CALL)
    texts = [e["text"] for e in files(wired.settings)["transcript"]["events"]]
    assert texts == ["first line", "second line"]  # retried, in order, stored once each


async def test_the_voice_agent_token_is_required_by_the_copilot(wired):
    v = wired.build()
    v.forwarder._token = "wrong-token"
    await v.forwarder.on_event(stt(TRANSCRIPT_FINAL, 1, "never stored", utter=0))
    await v.forwarder.finish_call(CALL)
    assert not (wired.settings.conversations_dir / CALL).exists()
    assert wired.container.registry.current is None
