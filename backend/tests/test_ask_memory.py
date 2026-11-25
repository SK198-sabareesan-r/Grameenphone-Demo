"""Ask Copilot chat memory: the recent turns rewrite a follow-up into a standalone question before the search."""
import pytest

from copilot.services.ask import MAX_ASSISTANT_CHARS, MAX_HISTORY_MESSAGES, NO_ANSWER, UNAVAILABLE, recent_turns
from copilot.services.kb_retriever import KnowledgeUnavailable
from tests.conftest import FAST_ARN

FIRST = "How do I activate a roaming data pack?"
FOLLOW_UP = "and what does it cost for Malaysia?"
ROAMING = "How do I activate roaming before I travel abroad?"
STANDALONE = "What does the roaming pack for Malaysia cost?"
HISTORY = [
    {"role": "user", "content": FIRST},
    {"role": "assistant", "content": "1. Check the roaming type.\n2. Dial `*121*1599#`."},
]


def reply_for(standalone=STANDALONE, answer="**BDT 1599** for 10 days.", sources=("roaming-malaysia-pack",)):
    """One fake model for both calls: the rewrite asks for "question", the answer asks for "answer"."""
    def respond(user: str) -> dict:
        if "standalone English question" in user:
            return {"question": standalone}
        return {"answer": answer, "sources": list(sources)}
    return respond


@pytest.fixture
def searches(container, monkeypatch):
    """Records what the knowledge search is asked, and still answers from the real local articles."""
    asked = []
    original = container.retriever.search

    async def spy(query, limit=4):
        asked.append(query)
        return await original(query, limit)

    monkeypatch.setattr(container.retriever, "search", spy)
    return asked


async def test_a_first_question_is_answered_without_a_rewrite_call(container, fake_bedrock, searches):
    fake_bedrock.responses[FAST_ARN] = reply_for()
    result = await container.ask.answer(FIRST)
    assert len(fake_bedrock.calls_for(FAST_ARN)) == 1 and searches == [FIRST]
    assert "searched_for" not in result
    assert "Conversation so far" in fake_bedrock.calls[0]["user"] and "(none)" in fake_bedrock.calls[0]["user"]


async def test_a_follow_up_is_rewritten_with_the_chat_and_the_rewritten_question_is_searched(container, fake_bedrock, searches):
    fake_bedrock.responses[FAST_ARN] = reply_for()
    result = await container.ask.answer(FOLLOW_UP, HISTORY)
    rewrite, answer = fake_bedrock.calls_for(FAST_ARN)
    assert FIRST in rewrite["user"] and "Dial `*121*1599#`" in rewrite["user"] and FOLLOW_UP in rewrite["user"]
    assert searches == [STANDALONE]
    assert FOLLOW_UP in answer["user"] and f"searched for as: {STANDALONE}" in answer["user"]
    assert "Agent: " + FIRST in answer["user"]  # the answer prompt also sees the chat, as context only
    assert result["searched_for"] == STANDALONE
    assert result["answer"] == "**BDT 1599** for 10 days." and [s["id"] for s in result["sources"]] == ["roaming-malaysia-pack"]


async def test_the_original_question_is_used_when_the_rewrite_fails(container, fake_bedrock, searches):
    calls = []

    def respond(user: str) -> dict:
        calls.append(user)
        if "standalone English question" in user:
            raise RuntimeError("throttled")
        return {"answer": "ok", "sources": []}

    fake_bedrock.responses[FAST_ARN] = respond
    result = await container.ask.answer(FOLLOW_UP, HISTORY)
    assert searches == [FOLLOW_UP] and "searched_for" not in result and result["answer"] == "ok"


@pytest.mark.parametrize("bad", [{}, {"question": ""}, {"question": "   "}, {"question": 5}, {"other": "x"}])
async def test_an_unusable_rewrite_falls_back_to_the_original_question(container, fake_bedrock, searches, bad):
    fake_bedrock.responses[FAST_ARN] = lambda user: bad if "standalone English question" in user else {"answer": "ok", "sources": []}
    result = await container.ask.answer(FOLLOW_UP, HISTORY)
    assert searches == [FOLLOW_UP] and "searched_for" not in result


async def test_a_rewrite_that_equals_the_question_is_not_reported(container, fake_bedrock, searches):
    fake_bedrock.responses[FAST_ARN] = reply_for(standalone=FIRST)
    assert "searched_for" not in await container.ask.answer(FIRST, HISTORY)


async def test_nothing_found_still_reports_what_was_searched(container, fake_bedrock, searches):
    fake_bedrock.responses[FAST_ARN] = reply_for(standalone="zzqx plorp vrumm")
    result = await container.ask.answer("and that one?", HISTORY)
    assert result == {"answer": NO_ANSWER, "sources": [], "searched_for": "zzqx plorp vrumm"}
    assert len(fake_bedrock.calls_for(FAST_ARN)) == 1  # only the rewrite: no answer call


async def test_only_the_last_turns_are_used_and_long_answers_are_cut_short(container, fake_bedrock):
    fake_bedrock.responses[FAST_ARN] = reply_for()
    long_history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i} " + "x" * 2000} for i in range(10)]
    await container.ask.answer(FOLLOW_UP, long_history)
    prompt = fake_bedrock.calls[0]["user"]
    assert "message 4 " in prompt and "message 3 " not in prompt  # the last six of ten (4 to 9)
    assert "x" * (MAX_ASSISTANT_CHARS + 50) not in prompt


def test_recent_turns_drops_empty_and_unknown_roles():
    turns = recent_turns([{"role": "system", "content": "x"}, {"role": "user", "content": "  "}, {"role": "user", "content": "hi"}, {"nope": 1}])
    assert turns == [{"role": "user", "content": "hi"}]
    assert recent_turns(None) == [] and len(recent_turns([{"role": "user", "content": str(i)} for i in range(20)])) == MAX_HISTORY_MESSAGES


# ---- the route ----
def test_the_route_accepts_the_chat_and_reports_the_rewritten_question(client, fake_bedrock):
    fake_bedrock.responses[FAST_ARN] = reply_for()
    body = client.post("/api/ask", json={"question": FOLLOW_UP, "history": HISTORY}).json()
    assert body["searched_for"] == STANDALONE and body["answer"].startswith("**BDT")


def test_the_route_without_a_chat_works_as_before(client, fake_bedrock):
    fake_bedrock.responses[FAST_ARN] = reply_for()
    body = client.post("/api/ask", json={"question": FIRST}).json()
    assert set(body) == {"answer", "sources"}


@pytest.mark.parametrize("history", [
    [{"role": "system", "content": "x"}],
    [{"role": "user", "content": ""}],
    [{"role": "user", "content": "x" * 4001}],
    [{"role": "user", "content": "x"}] * 13,
    [{"content": "no role"}],
])
def test_the_route_refuses_a_bad_chat(client, history):
    assert client.post("/api/ask", json={"question": FIRST, "history": history}).status_code == 422


def test_the_server_keeps_no_chat_between_requests(client, fake_bedrock):
    fake_bedrock.responses[FAST_ARN] = reply_for()
    client.post("/api/ask", json={"question": FOLLOW_UP, "history": HISTORY})
    fake_bedrock.calls.clear()
    client.post("/api/ask", json={"question": FIRST})  # a new request without a chat remembers nothing
    assert len(fake_bedrock.calls_for(FAST_ARN)) == 1 and "(none)" in fake_bedrock.calls[0]["user"]


async def test_when_the_knowledge_base_cannot_be_reached_the_reply_says_so_and_nothing_else_answers(container, fake_bedrock, monkeypatch):
    fake_bedrock.responses[FAST_ARN] = reply_for()

    async def down(query, limit=4):
        raise KnowledgeUnavailable('expired token')

    monkeypatch.setattr(container.retriever, 'search', down)
    result = await container.ask.answer(FIRST)
    assert result == {'answer': UNAVAILABLE, 'sources': [], 'unavailable': True}
    assert fake_bedrock.calls == []  # no model call: nothing to answer from


def test_the_route_reports_an_unreachable_knowledge_base(client, fake_bedrock, container, monkeypatch):
    async def down(query, limit=4):
        raise KnowledgeUnavailable('down')

    monkeypatch.setattr(client.app.state.container.retriever, 'search', down)
    body = client.post('/api/ask', json={'question': FIRST}).json()
    assert body['unavailable'] is True and body['sources'] == [] and body['answer'] == UNAVAILABLE


async def test_an_answer_that_used_no_document_shows_no_source(container, fake_bedrock):
    fake_bedrock.responses[FAST_ARN] = {'answer': 'This is not in the approved knowledge.', 'sources': []}
    result = await container.ask.answer(ROAMING)
    assert result['sources'] == []
