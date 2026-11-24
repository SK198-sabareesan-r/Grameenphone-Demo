"""Voice calls: turns events from the voice agent into stored JSON and live panel updates.

Two entry points, called by copilot/api/internal_routes.py after authentication and validation:

* handle_transcript_event: store the event, then show it on the panel (partial lines update the line
  in progress, final lines become transcript turns and go through the normal Copilot analysis).
* handle_call_event: store the lifecycle event, update the customer / AI-agent / human-agent contexts,
  and update the call status shown on the panel.

Storage failures raise StorageUnavailable and the panel is NOT updated for that event, so a retry of the
same event (same event_id) is safe: nothing was half-applied.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timezone

from copilot.container import Container
from copilot.models.internal_events import CallEventIn, TranscriptEventIn
from copilot.services import conversation_store as store
from copilot.services.pipeline import CallPipeline
from copilot.services.conversation_store import now_iso

log = logging.getLogger(__name__)
MAX_INTENT_CHARS = 500


def mask_number(value: str | None) -> str | None:
    """Keep the last 4 digits only. Full phone numbers are never written to the JSON files."""
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    return f"***{digits[-4:]}" if digits else None


def _iso(value) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---- panel session lookup ------------------------------------------------------------------------


def _live_pipeline(c: Container, call_id: str) -> CallPipeline | None:
    session = c.registry.get(call_id)
    pipeline = c.registry.pipeline
    if session is None or session.ended_at is not None or pipeline is None or pipeline.session is not session:
        return None
    return pipeline


async def _pipeline_for(c: Container, call_id: str, caller: str = "") -> CallPipeline | None:
    """The panel pipeline of this call, started on first use. None when the call already ended."""
    existing = c.registry.get(call_id)
    if existing is not None:
        return _live_pipeline(c, call_id)
    pipeline = CallPipeline(c)
    log.info("voice_call_registered call_id=%s", call_id)  # the panel now has this call on screen
    await pipeline.begin(call_sid=call_id, caller=caller, source="voice", agent_mode="ai_agent")
    return pipeline


# ---- transcript events ---------------------------------------------------------------------------


async def handle_transcript_event(c: Container, ev: TranscriptEventIn) -> str:
    """Returns "stored" or "duplicate". Raises store.StoreError subclasses for the route to map."""
    record = {
        "event_id": ev.event_id,
        "call_id": ev.call_id,
        "session_id": ev.session_id,
        "customer_id": ev.customer_id,
        "sequence_number": ev.sequence_number,
        "speaker": ev.speaker,
        "speaker_basis": ev.speaker_basis,
        "transcript_type": ev.transcript_type,
        "text": ev.transcript_text,
        "timestamp": _iso(ev.timestamp),
        "call_status": ev.call_status,
        "agent_mode": ev.agent_mode,
        "language": ev.language,
    }
    customer_id = ev.customer_id or await c.conversations.customer_id_of(ev.call_id)  # set by call_started
    record["customer_id"] = customer_id
    result = await c.conversations.add_transcript_event(record)
    if result == "duplicate":
        return result

    if ev.transcript_type == "final":
        await _record_final_in_contexts(c, ev, customer_id)

    pipeline = await _pipeline_for(c, ev.call_id)
    if pipeline is not None:
        try:
            await pipeline.ingest(ev.speaker, ev.transcript_type, ev.transcript_text, ev.timestamp.timestamp())
        except Exception:  # the panel must never make the voice agent retry an already stored event
            log.exception("voice_panel_update_failed call_id=%s", ev.call_id)
    return result


async def _record_final_in_contexts(c: Container, ev: TranscriptEventIn, customer_id: str | None) -> None:
    ref = ev.event_id
    kw = {"session_id": ev.session_id, "customer_id": customer_id}
    if ev.speaker == "customer":

        def mutate(doc: dict) -> None:
            doc["conversation_history_refs"].append(ref)
            if not doc.get("initial_intent"):
                doc["initial_intent"] = ev.transcript_text[:MAX_INTENT_CHARS]

        await c.conversations.update(ev.call_id, store.CUSTOMER, mutate, **kw)
    elif ev.speaker == "ai_agent":

        def mutate(doc: dict) -> None:
            doc["messages"].append({"event_id": ref, "text": ev.transcript_text, "timestamp": _iso(ev.timestamp)})

        await c.conversations.update(ev.call_id, store.AI_AGENT, mutate, **kw)
    elif ev.speaker == "human_agent":

        def mutate(doc: dict) -> None:
            doc["transcript_refs"].append(ref)

        await c.conversations.update(ev.call_id, store.HUMAN_AGENT, mutate, **kw)
    # speaker "unknown": kept in transcript.json only, in no participant's context.


# ---- lifecycle events ----------------------------------------------------------------------------


async def handle_call_event(c: Container, ev: CallEventIn) -> str:
    data = dict(ev.data)
    caller_number = data.pop("from_number", None)  # used once to find the customer, never stored
    customer = None
    if ev.type == "call_started":
        # The caller ID only SUGGESTS who is calling: the panel still waits for the agent to confirm the mobile number
        # (identity_routes). The stored files get the matching customer id so all five files can be joined.
        customer = c.customers.find_by_number(caller_number) if caller_number else None
        if customer:
            data["customer_name"] = customer.get("name")
        data["caller_ref"] = mask_number(caller_number)
    if "agent_ref" in data:
        data["agent_ref"] = mask_number(str(data["agent_ref"])) or str(data["agent_ref"])[:32]

    customer_id = ev.customer_id or (customer or {}).get("id") or await c.conversations.customer_id_of(ev.call_id)
    record = {
        "event_id": ev.event_id,
        "call_id": ev.call_id,
        "session_id": ev.session_id,
        "customer_id": customer_id,
        "type": ev.type,
        "timestamp": _iso(ev.timestamp),
        "source": ev.source,
        "data": data,
    }
    if await c.conversations.add_call_event(record) == "duplicate":
        return "duplicate"

    kw = {"session_id": ev.session_id, "customer_id": customer_id}
    at = _iso(ev.timestamp)
    cid = ev.call_id

    if ev.type == "call_started":

        def mutate_customer(doc: dict) -> None:
            # Only id and name, and marked as matched by caller ID, not confirmed by the agent.
            doc["customer"] = {"id": customer["id"], "name": customer.get("name"), "matched_by": "caller_id"} if customer else None
            doc["caller_ref"] = data.get("caller_ref")

        await c.conversations.update(cid, store.CUSTOMER, mutate_customer, **kw)
        await c.conversations.bind_customer(cid, ev.session_id, customer_id)
        await _pipeline_for(c, cid, caller_number or "")

    elif ev.type == "ai_agent_connected":
        await c.conversations.update(cid, store.AI_AGENT, lambda d: d["actions"].append({"type": ev.type, "at": at}), **kw)
        pipeline = await _pipeline_for(c, cid)
        if pipeline:
            await pipeline.set_status("in_progress", "ai_agent")

    elif ev.type == "ai_resolution_attempt":
        # The customer said the AI's help did not work (or asked for something it cannot do). Recorded, not a transfer.
        entry = {"type": ev.type, "at": at, "signal": str(data.get("signal"))[:40], "attempt": data.get("attempt")}
        await c.conversations.update(cid, store.AI_AGENT, lambda d: d["actions"].append(entry), **kw)
        await _set_status(c, cid, "ai_resolution_attempt", "ai_agent")

    elif ev.type == "human_handoff_requested":
        reason = str(data.get("reason") or "not given")[:500]

        def mutate_ai(doc: dict) -> None:
            doc["handoff"] = {
                "reason": reason,
                "requested_at": at,
                "requested_by": str(data.get("requested_by") or "ai_agent")[:32],
                "pending_actions": [str(a)[:200] for a in (data.get("pending_actions") or [])][:20],
            }
            doc["actions"].append({"type": "handoff_requested", "at": at, "reason": reason})

        def mutate_human(doc: dict) -> None:
            doc["handoff"] = {"status": "requested", "requested_at": at}

        await c.conversations.update(cid, store.AI_AGENT, mutate_ai, **kw)
        await c.conversations.update(cid, store.HUMAN_AGENT, mutate_human, **kw)
        await _set_status(c, cid, "handoff_requested")

    elif ev.type == "human_agent_dialing":

        def mutate(doc: dict) -> None:
            doc["handoff"] = {**(doc.get("handoff") or {}), "status": "dialing", "dialing_at": at}
            doc["agent_ref"] = data.get("agent_ref") or doc.get("agent_ref")

        await c.conversations.update(cid, store.HUMAN_AGENT, mutate, **kw)
        await _set_status(c, cid, "dialing_human")

    elif ev.type == "human_agent_connected":

        def mutate(doc: dict) -> None:
            doc["handoff"] = {**(doc.get("handoff") or {}), "status": "connected", "accepted_at": at}
            doc["agent_ref"] = data.get("agent_ref") or doc.get("agent_ref")

        await c.conversations.update(cid, store.HUMAN_AGENT, mutate, **kw)
        await _set_status(c, cid, "with_human", "human_agent")
        await _activate_assist(c, ev, at, kw)

    elif ev.type == "human_handoff_failed":
        why = str(data.get("reason") or "unknown")[:200]

        def mutate_human(doc: dict) -> None:
            doc["handoff"] = {**(doc.get("handoff") or {}), "status": "failed", "failed_at": at, "failure": why}

        await c.conversations.update(cid, store.HUMAN_AGENT, mutate_human, **kw)
        await c.conversations.update(
            cid, store.AI_AGENT, lambda d: d["actions"].append({"type": "handoff_failed", "at": at, "reason": why}), **kw
        )
        await _set_status(c, cid, "handoff_failed", "ai_agent")  # the AI has the call again; the panel still shows the failure

    elif ev.type in ("customer_put_on_hold", "customer_resumed"):

        def mutate(doc: dict) -> None:
            doc["hold_events"].append(
                {"event_id": ev.event_id, "type": ev.type, "at": at, "source": str(data.get("source") or "unknown")[:64]}
            )

        await c.conversations.update(cid, store.HUMAN_AGENT, mutate, **kw)
        await _set_status(c, cid, "on_hold" if ev.type == "customer_put_on_hold" else "with_human")

    elif ev.type == "human_agent_disconnected":

        def mutate(doc: dict) -> None:
            doc["handoff"] = {**(doc.get("handoff") or {}), "status": "completed", "completed_at": at}

        await c.conversations.update(cid, store.HUMAN_AGENT, mutate, **kw)
        await _set_status(c, cid, "human_disconnected")

    elif ev.type == "call_ended":
        if data.get("resolution"):  # how the call ended: ai_resolved | human_handled | unresolved | no_interaction
            outcome = {"outcome": str(data["resolution"])[:40], "at": at}
            await c.conversations.update(cid, store.AI_AGENT, lambda d: d.update(resolution=outcome), **kw)
        pipeline = _live_pipeline(c, cid)
        if pipeline is not None:
            await pipeline.finish()
        await c.conversations.release(cid)

    return "stored"


async def _set_status(c: Container, call_id: str, status: str, agent_mode: str | None = None) -> None:
    pipeline = await _pipeline_for(c, call_id)
    if pipeline is not None:
        await pipeline.set_status(status, agent_mode)


async def _activate_assist(c: Container, ev: CallEventIn, at: str, kw: dict) -> None:
    """Once a human agent is connected, the Copilot (the AI) keeps listening and assisting in the
    background. Record that as its own lifecycle event, derived from the connect event's id so a retried
    connect event cannot create it twice."""
    record = {
        "event_id": f"{ev.event_id}-assist",
        "call_id": ev.call_id,
        "session_id": ev.session_id,
        "customer_id": kw["customer_id"],
        "type": "ai_agent_assist_activated",
        "timestamp": at,
        "source": "copilot",
        "data": {"mode": "background_assist", "ai_speaks_to_customer": False},
    }
    if await c.conversations.add_call_event(record) == "stored":
        await c.conversations.update(
            ev.call_id, store.HUMAN_AGENT, lambda d: d.update(assist_activated_at=at), **kw
        )


# ---- notes ---------------------------------------------------------------------------------------


async def add_note(c: Container, call_id: str, text: str, author: str) -> dict:
    note = {"id": uuid.uuid4().hex[:12], "text": text, "at": now_iso(), "author": author[:64]}
    await c.conversations.update(call_id, store.HUMAN_AGENT, lambda d: d["notes"].append(note))
    return note
