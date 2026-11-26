"""Tests for the automatic lookup. No network: Bedrock and the panel hub are replaced by fakes."""

import asyncio
import tempfile
from pathlib import Path

from config.settings import Settings
from copilot.repo.base import DataUnavailable
from copilot.repo.files import FileRepo
from copilot.services.assist import NOT_FOUND_TEXTS, LiveAssist
from copilot.services.kb_retriever import KnowledgeRetriever, KnowledgeUnavailable
from copilot.services.kb_retriever import KnowledgeRetriever, KnowledgeUnavailable
from copilot.services.knowledge import KnowledgeBase
from copilot.services.sessions import CallSession

DATA = Path(__file__).resolve().parents[1] / "data"
RUNTIME = Path(tempfile.mkdtemp())  # tests never touch the real data/runtime folder
CUSTOMER = {"id": "rahim", "name": "Rahim Uddin", "area": "Mirpur"}


class BrokenReferenceRepo(FileRepo):
    """Packs and GP Centres cannot be read, as when the data store is down."""

    async def list_packs(self):
        raise DataUnavailable("ExpiredTokenException")


class FakeHub:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def broadcast(self, event: str, data: dict) -> None:
        self.events.append((event, data))

    def of(self, name: str) -> list[dict]:
        return [d for e, d in self.events if e == name]


class FakeBedrock:
    """Returns a canned reply (or raises), and remembers every prompt it was given."""

    def __init__(self, reply: dict | None = None, error: Exception | None = None) -> None:
        self.reply, self.error, self.prompts = reply, error, []
        self.gate: asyncio.Event | None = None  # when set, the call waits until the test opens it

    async def complete_json(self, model_id, system, user, max_tokens=500, temperature=0.1) -> dict:
        self.prompts.append(user)
        if self.gate is not None:
            await self.gate.wait()
        if self.error:
            raise self.error
        return self.reply


def make(bedrock: FakeBedrock, repo_class=FileRepo):
    # _env_file=None: never read config/.env in tests
    settings = Settings(_env_file=None, bedrock_inference_profile_arn_fast="arn:aws:bedrock:ap-south-1:1:test/x")
    hub = FakeHub()
    kb = KnowledgeBase(DATA / "knowledge" / "articles")
    retriever = KnowledgeRetriever(settings, bedrock, kb)
    return LiveAssist(settings, bedrock, retriever, hub, repo_class(settings, runtime_dir=RUNTIME)), hub


def session_with(*lines: tuple[str, str]) -> CallSession:
    session = CallSession(
        call_sid="CA1", customer=CUSTOMER, compliance={"verify_identity": "idle"}
    )
    for speaker, text in lines:
        session.add_turn(speaker, text)
    return session


def test_greetings_and_acknowledgements_are_small_talk():
    assist, _ = make(FakeBedrock())
    for text in ["Hello", "Assalamu alaikum", "thik ache", "ঠিক আছে", "ok ok", "Thank you!", "জি"]:
        assert assist.is_small_talk(text), text


def test_questions_and_account_sentences_are_not_small_talk():
    assist, _ = make(FakeBedrock())
    for text in [
        "Bhai, amar net 2 din dhore kaj korche na.",
        "thik ache chalu kore den",
        "Amar bill e 100 taka extra keno?",
        "Roaming e data pack kivabe chalu hoy?",
    ]:
        assert not assist.is_small_talk(text), text


def test_a_question_gets_one_model_call_and_only_real_sources_are_kept():
    reply = {
        "needs_lookup": True,
        "answer": "Dial *121*1*4# to check the remaining internet.",
        "sources": ["internet-packs-general", "made-up-article"],
        "used_customer_data": True,
        "confidence": "high",
        "clarifying_question": None,
    }
    bedrock = FakeBedrock(reply)
    assist, hub = make(bedrock)
    session = session_with(("customer", "Amar internet balance koto ache, kivabe check korbo?"))

    entry = asyncio.run(assist.run_now(session))

    assert len(bedrock.prompts) == 1
    assert [s["id"] for s in entry["sources"]] == ["internet-packs-general"]
    assert entry["confidence"] == "high" and entry["used_customer_data"] is True
    assert entry["mode"] == "ai"
    assert session.answers == [entry]
    assert [e for e, _ in hub.events] == ["answer.pending", "answer"]


def test_model_can_decide_nothing_needs_looking_up():
    assist, hub = make(FakeBedrock({"needs_lookup": False}))
    session = session_with(("customer", "Amar ekta kotha chilo"))

    entry = asyncio.run(assist.run_now(session))

    assert entry["needs_lookup"] is False
    assert session.answers == []  # nothing stored for the panel history
    assert hub.of("answer")[0]["needs_lookup"] is False  # but the panel is told, so it stops showing "looking up"


def test_missing_answer_becomes_a_clear_not_found_message():
    assist, _ = make(FakeBedrock({"needs_lookup": True, "sources": [], "confidence": "high"}))
    session = session_with(("customer", "Amader elakay notun tower kobe boshbe?"))

    entry = asyncio.run(assist.run_now(session))

    assert entry["answer"] == NOT_FOUND_TEXTS["banglish"]  # the customer speaks Banglish
    assert entry["confidence"] == "high"  # taken from the model, not invented here


def test_when_bedrock_fails_the_matching_articles_are_still_shown():
    assist, _ = make(FakeBedrock(error=RuntimeError("expired token")))
    session = session_with(("customer", "SIM hariye gese, ki korbo?"))

    entry = asyncio.run(assist.run_now(session))

    assert entry["mode"] == "search_only" and entry["answer"] is None
    assert "sim-replacement" in [s["id"] for s in entry["sources"]]


def test_a_short_follow_up_is_searched_together_with_the_previous_sentence():
    bedrock = FakeBedrock({"needs_lookup": False})
    assist, _ = make(bedrock)
    session = session_with(
        ("customer", "Amar SIM hariye gese"),
        ("agent", "Ji bolun"),
        ("customer", "Fee koto?"),
    )

    asyncio.run(assist.run_now(session))

    assert "[sim-replacement]" in bedrock.prompts[0]  # found through the earlier sentence


def test_identity_check_status_is_passed_to_the_model():
    bedrock = FakeBedrock({"needs_lookup": False})
    assist, _ = make(bedrock)
    session = session_with(("customer", "Amar bill koto?"))

    asyncio.run(assist.run_now(session))
    session.compliance["verify_identity"] = "green"
    asyncio.run(assist.run_now(session))

    assert "Identity check done: no" in bedrock.prompts[0]
    assert "Identity check done: yes" in bedrock.prompts[1]


def test_small_talk_is_not_sent_to_the_model_at_all():
    bedrock = FakeBedrock({"needs_lookup": False})
    assist, hub = make(bedrock)

    async def scenario():
        session = session_with(("customer", "Hello"))
        assist.schedule(session, session.turns[0])
        await asyncio.sleep(0.05)
        return session

    asyncio.run(scenario())

    assert bedrock.prompts == [] and hub.events == []


def test_lookups_run_one_at_a_time_and_finished_ones_are_kept():
    reply = {"needs_lookup": True, "answer": "ok", "sources": [], "confidence": "low"}
    bedrock = FakeBedrock(reply)
    assist, hub = make(bedrock)

    async def scenario():
        bedrock.gate = asyncio.Event()
        session = session_with(("customer", "Amar net kaj korche na 2 din dhore"))
        assist.schedule(session, session.turns[0])
        await asyncio.sleep(0.05)  # the first lookup is now waiting on the model
        middle = session.add_turn("customer", "Ar amar bill e 100 taka extra keno ase?")
        assist.schedule(session, middle)  # waits behind the first one
        newest = session.add_turn("customer", "Roaming pack kon ta nibo?")
        assist.schedule(session, newest)  # replaces the waiting one: the model also sees the transcript
        bedrock.gate.set()
        await asyncio.sleep(0.3)
        return session

    session = asyncio.run(scenario())

    # the finished first lookup is kept, the overtaken middle sentence is skipped, the newest is answered
    assert [a["turn_id"] for a in session.answers] == [1, 3]
    assert [d["turn_id"] for d in hub.of("answer")] == [1, 3]
    assert len(bedrock.prompts) == 2


def test_pack_catalog_and_gp_centres_reach_the_model():
    bedrock = FakeBedrock({"needs_lookup": False})
    assist, _ = make(bedrock)
    session = session_with(("customer", "Amar SIM hariye gese, ar boro pack ki ache?"))
    session.current_location = {"area": "Gulshan", "status": "no_notice"}

    asyncio.run(assist.run_now(session))

    prompt = bedrock.prompts[0]
    assert "30 GB + 300 minutes: Tk 598, 30 days" in prompt  # from the packs table
    assert "activation code: none published" in prompt  # codes are never invented
    assert "Grameenphone Center Mirpur-1 (Mirpur)" in prompt  # the customer's registered area
    assert "Grameenphone Experience, Gulshan (Gulshan)" in prompt  # where the customer is now


def test_the_lookup_still_runs_when_packs_and_centres_cannot_be_read():
    bedrock = FakeBedrock({"needs_lookup": False})
    assist, _ = make(bedrock, BrokenReferenceRepo)
    session = session_with(("customer", "Amar boro pack ki ache?"))

    entry = asyncio.run(assist.run_now(session))

    assert entry["needs_lookup"] is False and len(bedrock.prompts) == 1
    assert "(none)" in bedrock.prompts[0]


def test_the_lookup_carries_on_with_the_card_when_the_knowledge_base_cannot_be_reached():
    bedrock = FakeBedrock({"needs_lookup": True, "answer": "The knowledge is not available right now.", "sources": [], "used_customer_data": True,
                           "confidence": "low", "clarifying_question": None})
    assist, _ = make(bedrock)

    async def down(query, limit=4):
        raise KnowledgeUnavailable("expired token")

    assist._kb.search = down
    entry = asyncio.run(assist.run_now(session_with(("customer", "Amar internet balance koto ache?"))))
    assert entry["needs_lookup"] is True and entry["sources"] == [] and entry["mode"] == "ai"
    assert "knowledge base could not be reached" in bedrock.prompts[0]
    assert "Do not answer from memory" in bedrock.prompts[0]


def reply(**extra) -> dict:
    return {"needs_lookup": True, "answer": "Check the area.", "sources": [], "confidence": "medium", **extra}


def test_a_translation_in_brackets_is_removed_from_the_ask_line():
    asked = "Apni ekhon kothay achen? (Where are you right now?)"
    assist, _ = make(FakeBedrock(reply(clarifying_question=asked)))
    entry = asyncio.run(assist.run_now(session_with(("customer", "Amar network kaj korche na"))))
    assert entry["clarifying_question"] == "Apni ekhon kothay achen?"


def test_the_same_ask_line_is_not_shown_twice_on_a_call():
    assist, _ = make(FakeBedrock(reply(clarifying_question="Apni ekhon kothay achen?")))
    session = session_with(("customer", "Amar network kaj korche na"))
    first = asyncio.run(assist.run_now(session))
    session.add_turn("customer", "Amar net slow, kichu hocche na")
    second = asyncio.run(assist.run_now(session))
    assert first["clarifying_question"] == "Apni ekhon kothay achen?"
    assert second["clarifying_question"] is None and second["answer"]  # the answer stays, only the repeat goes
    again = asyncio.run(assist.run_now(session))  # the same sentence looked up again keeps its own ask line
    assert again["clarifying_question"] is None  # already shown for an earlier sentence


def test_a_regenerated_answer_for_the_same_sentence_keeps_its_ask_line():
    assist, _ = make(FakeBedrock(reply(clarifying_question="Which country are you travelling to?")))
    session = session_with(("customer", "Roaming kivabe chalu hoy?"))
    asyncio.run(assist.run_now(session))
    again = asyncio.run(assist.run_now(session))
    assert again["clarifying_question"] == "Which country are you travelling to?"


def test_blank_or_non_text_ask_lines_become_none():
    for value in ["", "   ", None, 5, ["x"], "(only a bracket)"]:
        assist, _ = make(FakeBedrock(reply(clarifying_question=value)))
        entry = asyncio.run(assist.run_now(session_with(("customer", "Amar network kaj korche na"))))
        assert entry["clarifying_question"] is None, value


def test_a_long_answer_is_cut_at_the_end_of_a_sentence():
    long_answer = ("Dial *121*1*4# to see the data left. " * 20).strip()
    assist, _ = make(FakeBedrock(reply(answer=long_answer)))
    entry = asyncio.run(assist.run_now(session_with(("customer", "Amar internet koto ache?"))))
    assert len(entry["answer"]) <= 600 and entry["answer"].endswith("left.")


def test_the_prompt_tells_the_model_how_to_write_the_ask_line():
    bedrock = FakeBedrock({"needs_lookup": False})
    assist, _ = make(bedrock)
    asyncio.run(assist.run_now(session_with(("customer", "Amar bill koto?"))))
    prompt = bedrock.prompts[0]
    assert "language and script of the customer's latest sentence" in prompt
    assert "Do not ask for the mobile number when" in prompt
