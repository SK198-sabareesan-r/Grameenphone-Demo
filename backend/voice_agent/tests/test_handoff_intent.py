"""Does the customer ask for a person? Wording rules, including negatives that must NOT start a transfer."""

from __future__ import annotations

import pytest

from app.services.conversation.handoff_intent import detect_human_request as detect

REQUESTS = [
    # the examples from the task
    "I want to speak to a human.", "Can I talk to a human agent?", "Connect me to customer support.",
    "Transfer my call to an agent.", "I need to speak with a real person.", "Please connect me to your representative.",
    "I want to talk to customer care.",
    # the sentence that failed on the first real call
    "Can I contact human agent?", "Can I contact human agent", "Can I contact a human agent?", "can i contact a human agent",
    "Can I talk to human agent?", "Can I talk to a human agent?", "Can I speak with a human agent?",
    "I want to speak with customer care.", "Connect me to a human representative.", "Please transfer me to an agent.",
    "I need to talk to a real person.", "Can you connect me with support?", "I want to talk to customer service.",
    "To a human agent can I speak", "Can I, uh, talk to a human agent",
    # the exact sentence the first real call missed: a negation in the previous clause used to hide the request
    "I tried too many times, it's not working. So can I contact a human agent?",
    "it is slow and not working but can I speak to an agent", "my internet is not working so I want to talk to customer care",
    # other natural wordings, speech-recognition style (no punctuation, odd casing)
    "agent please", "human", "i need an agent", "put me through to someone", "give me a live agent", "get me a real person",
    "can i speak to somebody", "No, connect me to an agent", "I would like to talk with your customer service",
    "Hello can you transfer me to the support team", "escalate this to a supervisor please",
    # Banglish and Bangla
    "agent er sathe kotha bolbo", "amake customer care er sathe connect koren", "manush er sathe kotha bolte chai",
    "\u098f\u099c\u09c7\u09a8\u09cd\u099f\u09c7\u09b0 \u09b8\u09be\u09a5\u09c7 \u0995\u09a5\u09be \u09ac\u09b2\u09a4\u09c7 \u099a\u09be\u0987",
]

NOT_REQUESTS = [
    "What is the customer care number?", "my internet is not working", "I called customer care yesterday but nobody replied",
    "I don't want to talk to a human", "I do not need an agent", "no need for an agent", "the agent told me to recharge",
    "How do I call customer care from abroad?", "give me the customer care number", "I want to recharge my number",
    "what are your customer care hours", "can you check my balance", "which person is this", "thank you",
    "how much does it cost to talk to an agent", "is customer care free", "my sim is not working",
    "agent er sathe kotha bolte chai na",
    "I need support with my SIM", "the agent told me to speak to my manager", "talk about customer support",
    "I don't want a human, I want to check my balance", "i want to know about customer service hours",
    "it is not working, what is the customer care number", "please help my network is not working",
    "\u098f\u099c\u09c7\u09a8\u09cd\u099f\u09c7\u09b0 \u09b8\u09be\u09a5\u09c7 \u0995\u09a5\u09be \u09ac\u09b2\u09a4\u09c7 \u099a\u09be\u0987 \u09a8\u09be",
    "", "   ",
]


@pytest.mark.parametrize("sentence", REQUESTS)
def test_requests_for_a_person_are_recognised(sentence) -> None:
    result = detect(sentence)
    assert result.matched, sentence
    assert result.rule  # the rule name is what gets logged, never the text


@pytest.mark.parametrize("sentence", NOT_REQUESTS)
def test_unrelated_or_negated_sentences_are_not_requests(sentence) -> None:
    assert not detect(sentence).matched, sentence


def test_configured_phrases_extend_the_builtin_rules_and_respect_negation() -> None:
    assert not detect("tell me about the gp wizard").matched
    assert detect("tell me about the gp wizard", ["GP Wizard"]).rule == "configured_phrase"
    assert not detect("I do not want the gp wizard", ["gp wizard"]).matched
    assert not detect("wizardry", ["wizard"]).matched  # whole words only


def test_the_result_never_contains_the_customers_words() -> None:
    assert "refund" not in repr(detect("I want to talk to an agent about my refund"))

def test_the_missed_real_sentence_is_matched_by_the_direct_rule() -> None:
    result = detect("I tried too many times, it's not working. So can I contact a human agent?")
    assert result.matched and result.rule == "direct"


def test_a_request_in_one_clause_is_not_cancelled_by_a_negation_in_another() -> None:
    assert detect("I don't want to wait, connect me to an agent").matched


def test_a_negated_request_stays_negated_even_with_other_clauses() -> None:
    assert not detect("my sim is slow, I do not want to talk to a human").matched
