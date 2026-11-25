"""Local JSON store for live voice calls: atomic writes, ordering, de-duplication, concurrency, recovery."""

import asyncio
import json

import pytest

from config.settings import BASE_DIR
from copilot.services import conversation_store as cs
from copilot.services.conversation_store import ConversationStore


def tev(n, call="CALL_1", session="sess-CALL_1", speaker="customer", kind="final", text=None, **extra):
    return {
        "event_id": f"{call}-t{n}", "call_id": call, "session_id": session, "customer_id": None,
        "sequence_number": n, "speaker": speaker, "transcript_type": kind, "text": text or f"line {n}",
        "timestamp": f"2026-11-24T10:30:{n:02d}.000Z", "agent_mode": "ai_agent", **extra,
    }


def cev(name, ts, call="CALL_1", session="sess-CALL_1", n=None):
    return {"event_id": f"{call}-{name}{n or ''}", "call_id": call, "session_id": session, "type": name,
            "timestamp": ts, "source": "voice_agent", "data": {}}


@pytest.fixture
def store(tmp_path):
    return ConversationStore(tmp_path / "conversations")


def files(store, call="CALL_1"):
    return sorted(p.name for p in store.call_dir(call).iterdir())


async def test_transcript_is_written_to_transcript_json_with_the_documented_shape(store):
    assert await store.add_transcript_event(tev(1, customer_id="CUSTOMER_001")) == "stored"
    doc = json.loads((store.call_dir("CALL_1") / "transcript.json").read_text(encoding="utf-8"))
    assert doc["call_id"] == "CALL_1" and doc["session_id"] == "sess-CALL_1" and doc["customer_id"] == "CUSTOMER_001"
    event = doc["events"][0]
    assert {"event_id", "sequence_number", "speaker", "transcript_type", "text", "timestamp", "agent_mode"} <= set(event)
    assert "call_id" not in event  # carried once at the top, not repeated


async def test_partial_and_final_events_are_both_stored(store):
    await store.add_transcript_event(tev(1, kind="partial", text="I need"))
    await store.add_transcript_event(tev(2, kind="final", text="I need help"))
    doc = await store.read("CALL_1", cs.TRANSCRIPT)
    assert [e["transcript_type"] for e in doc["events"]] == ["partial", "final"]


async def test_same_event_id_is_stored_once(store):
    assert await store.add_transcript_event(tev(1)) == "stored"
    assert await store.add_transcript_event(tev(1)) == "duplicate"
    assert len((await store.read("CALL_1", cs.TRANSCRIPT))["events"]) == 1


async def test_out_of_order_arrival_is_stored_in_sequence_order(store):
    for n in (3, 1, 2, 5, 4):
        await store.add_transcript_event(tev(n))
    seqs = [e["sequence_number"] for e in (await store.read("CALL_1", cs.TRANSCRIPT))["events"]]
    assert seqs == [1, 2, 3, 4, 5]


async def test_a_different_event_reusing_a_sequence_number_is_a_conflict(store):
    await store.add_transcript_event(tev(1))
    clash = tev(1)
    clash["event_id"] = "CALL_1-other"
    with pytest.raises(cs.SequenceConflict):
        await store.add_transcript_event(clash)


async def test_another_session_for_the_same_call_is_refused(store):
    await store.add_transcript_event(tev(1))
    with pytest.raises(cs.SessionMismatch):
        await store.add_transcript_event(tev(2, session="sess-OTHER"))


@pytest.mark.parametrize("bad", ["../evil", "a/b", "a\\b", "", ".hidden", "x" * 200, "with space", "..", "C:evil"])
async def test_unsafe_call_ids_never_reach_the_file_system(store, bad):
    with pytest.raises(cs.InvalidIdentifier):
        await store.add_transcript_event(tev(1, call=bad))
    assert not (store.call_dir("CALL_1").parent.parent / "evil").exists()


async def test_call_events_are_kept_in_chronological_order_even_if_they_arrive_late(store):
    await store.add_call_event(cev("call_started", "2026-11-24T10:00:00.000Z"))
    await store.add_call_event(cev("call_ended", "2026-11-24T10:05:00.000Z"))
    await store.add_call_event(cev("human_agent_connected", "2026-11-24T10:02:00.000Z"))
    doc = await store.read("CALL_1", cs.CALL_EVENTS)
    assert [e["type"] for e in doc["events"]] == ["call_started", "human_agent_connected", "call_ended"]


async def test_duplicate_call_event_is_ignored(store):
    event = cev("call_started", "2026-11-24T10:00:00.000Z")
    assert await store.add_call_event(event) == "stored"
    assert await store.add_call_event(event) == "duplicate"
    assert len((await store.read("CALL_1", cs.CALL_EVENTS))["events"]) == 1


async def test_concurrent_writers_do_not_lose_events(store):
    await asyncio.gather(*(store.add_transcript_event(tev(n)) for n in range(1, 61)))
    await asyncio.gather(*(store.update("CALL_1", cs.CUSTOMER, lambda d, i=i: d["conversation_history_refs"].append(i))
                           for i in range(40)))
    on_disk = json.loads((store.call_dir("CALL_1") / "transcript.json").read_text(encoding="utf-8"))
    assert [e["sequence_number"] for e in on_disk["events"]] == list(range(1, 61))
    refs = json.loads((store.call_dir("CALL_1") / "customer_context.json").read_text(encoding="utf-8"))
    assert sorted(refs["conversation_history_refs"]) == list(range(40))


async def test_concurrent_duplicates_are_stored_once(store):
    results = await asyncio.gather(*(store.add_transcript_event(tev(1)) for _ in range(20)))
    assert results.count("stored") == 1 and results.count("duplicate") == 19


async def test_writes_are_atomic_no_temp_files_are_left_behind(store):
    await store.add_transcript_event(tev(1))
    assert files(store) == ["transcript.json"]


def test_a_failed_write_leaves_the_previous_file_intact(tmp_path, monkeypatch):
    target = tmp_path / "doc.json"
    cs.atomic_write_json(target, {"v": 1})

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(cs.os, "replace", boom)
    with pytest.raises(OSError):
        cs.atomic_write_json(target, {"v": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["doc.json"]  # the temp file was cleaned up


async def test_write_failure_is_reported_as_storage_unavailable(store, monkeypatch):
    monkeypatch.setattr(cs, "atomic_write_json", lambda *_a, **_k: (_ for _ in ()).throw(OSError("read-only")))
    with pytest.raises(cs.StorageUnavailable):
        await store.add_transcript_event(tev(1))


async def test_a_corrupted_file_is_moved_aside_and_the_call_continues(store):
    folder = store.call_dir("CALL_1")
    folder.mkdir(parents=True)
    (folder / "transcript.json").write_text("{ this is not json", encoding="utf-8")
    assert await store.add_transcript_event(tev(1)) == "stored"
    assert any(name.startswith("transcript.json.corrupt-") for name in files(store))
    assert len((await store.read("CALL_1", cs.TRANSCRIPT))["events"]) == 1


async def test_existing_files_are_loaded_and_extended_after_a_restart(store, tmp_path):
    await store.add_transcript_event(tev(1))
    fresh = ConversationStore(tmp_path / "conversations")  # new process, same folder
    assert await fresh.add_transcript_event(tev(1)) == "duplicate"
    assert await fresh.add_transcript_event(tev(2)) == "stored"
    assert len((await fresh.read("CALL_1", cs.TRANSCRIPT))["events"]) == 2


async def test_missing_files_read_as_empty_documents(store):
    for name in (cs.CUSTOMER, cs.AI_AGENT, cs.HUMAN_AGENT):
        doc = await store.read("NEW_CALL", name)
        assert doc["call_id"] == "NEW_CALL"
    assert not await store.exists("NEW_CALL")  # reading never creates files


async def test_contexts_share_call_session_and_customer_ids(store):
    kw = {"session_id": "sess-CALL_1", "customer_id": "rahim"}
    await store.update("CALL_1", cs.CUSTOMER, lambda d: None, **kw)
    await store.update("CALL_1", cs.AI_AGENT, lambda d: None, **kw)
    await store.update("CALL_1", cs.HUMAN_AGENT, lambda d: None, **kw)
    for name in (cs.CUSTOMER, cs.AI_AGENT, cs.HUMAN_AGENT):
        doc = json.loads((store.call_dir("CALL_1") / f"{name}.json").read_text(encoding="utf-8"))
        assert (doc["call_id"], doc["session_id"], doc["customer_id"]) == ("CALL_1", "sess-CALL_1", "rahim")


async def test_update_only_accepts_context_documents(store):
    with pytest.raises(ValueError):
        await store.update("CALL_1", cs.TRANSCRIPT, lambda d: None)


async def test_handoff_package_separates_ai_and_human_conversation(store):
    await store.add_transcript_event(tev(1, speaker="customer", text="recharge problem"))
    await store.add_transcript_event(tev(2, speaker="ai_agent", text="Let me check"))
    await store.add_transcript_event(tev(3, speaker="human_agent", text="Hello, I am Tania", agent_mode="human_agent"))
    await store.update("CALL_1", cs.CUSTOMER, lambda d: d.update(initial_intent="recharge problem"))
    await store.update("CALL_1", cs.AI_AGENT, lambda d: d.update(handoff={"reason": "needs a human", "pending_actions": ["refund"]}))
    package = await store.handoff_package("CALL_1")
    assert [e["text"] for e in package["ai_conversation"]] == ["recharge problem", "Let me check"]
    assert [e["text"] for e in package["live_conversation"]] == ["Hello, I am Tania"]
    assert package["handoff_reason"] == "needs a human" and package["pending_actions"] == ["refund"]
    assert package["summary"]["human_agent_turns"] == 1 and package["initial_intent"] == "recharge problem"


SAMPLE = BASE_DIR / "tests" / "fixtures" / "conversations" / "SAMPLE_CALL_001"


async def test_the_sample_fixture_is_a_valid_call_folder_the_store_can_read(tmp_path):
    """The sample is a recorded run of the M3 scenario (see test_m3_integration.py). It documents the file
    layout and proves the store loads real output: copy it, read it, and build the hand-off package."""
    import shutil

    root = tmp_path / "conversations"
    shutil.copytree(SAMPLE, root / SAMPLE.name)
    store = ConversationStore(root)
    transcript = await store.read(SAMPLE.name, cs.TRANSCRIPT)
    assert transcript["events"]
    for name in (cs.TRANSCRIPT, cs.CUSTOMER, cs.AI_AGENT, cs.HUMAN_AGENT, cs.CALL_EVENTS):
        doc = json.loads((SAMPLE / f"{name}.json").read_text(encoding="utf-8"))
        assert doc["schema_version"] == cs.SCHEMA_VERSION and doc["session_id"]
    assert await store.add_transcript_event(tev(99, call=SAMPLE.name, session=transcript["session_id"])) == "stored"
    package = await store.handoff_package(SAMPLE.name)
    assert package["handoff_reason"] and package["ai_conversation"] and package["live_conversation"]
