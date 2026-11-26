"""Does the CUSTOMER ask to speak to a person? Deterministic rules on the customer's own words.

Why rules and not the LLM: the AI's reply is a guess generated per turn (it answered "call 121 or 158"), and a
second model call per sentence would add latency and a new failure mode to the live call. This module looks only at
what the customer said, never at anything the AI generated, and gives the same answer every time.

Rules (a match needs one of them, and no negation):

* direct   : an unmistakable phrase ("human agent", "real person", "live agent", "human please").
* strong   : a request verb (talk, speak, connect, transfer, put me through ...) shortly BEFORE a target
             (human, agent, representative, customer care, someone ...). Works for "I need to speak with a real person".
* medium   : contact / call / reach / want / need + a target, only if the sentence is not a question about
             a number, hours, price and does not start like an information question ("how do I call ...").
* bare     : the whole utterance is one or two words such as "agent" or "human".
* bangla / banglish: a target and a verb in the same sentence (for example "agent er sathe kotha bolbo").
* extra    : phrases from HUMAN_HANDOFF_PHRASES, matched as whole words in the normalised sentence.

Negation ("I don't want to talk to a human", "no need for an agent") blocks every rule. Information questions
("what is the customer care number") never match. Logs may contain the rule name, never the text.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class HumanRequest:
    matched: bool
    rule: str = ""  # which rule fired: safe to log


NO_MATCH = HumanRequest(False)

# --- English ---------------------------------------------------------------------------------------------
_TARGETS = (
    "human being", "human agent", "human", "real person", "live person", "live agent", "person", "agent",
    "representative", "operator", "executive", "officer", "someone", "somebody", "customer care", "customer support",
    "customer service", "care agent", "support agent", "support team", "care team", "staff", "advisor", "adviser",
    "supervisor", "manager",
)
_DIRECT = (
    "human agent", "human representative", "human being", "real person", "live agent", "live person", "human please",
    "a human", "the human", "speak to human", "talk to human", "real human", "actual person", "actual human",
)
_STRONG_VERBS = (
    "talk", "speak", "connect", "transfer", "put me through", "put me on", "get me", "give me", "switch me",
    "pass me", "hand me", "forward", "escalate", "route", "redirect", "chat", "patch me",
)
# These also mean "tell me": they only count when the sentence is not about a number, hours or price.
_AMBIGUOUS_STRONG = {"give me", "get me", "forward"}
_MEDIUM_VERBS = ("contact", "call", "reach", "want", "need", "like", "get")
_INFO_WORDS = {
    "number", "numbers", "helpline", "hotline", "code", "email", "address", "website", "hours", "timing", "timings",
    "open", "free", "cost", "charge", "charges", "fee", "price", "app", "link",
}
_INFO_STARTS = ("how do i", "how can i", "how to", "what is", "what s", "what are", "where is", "where can", "which", "when", "why", "is there")
_NEGATIONS = {"not", "dont", "don", "never", "without", "cancel", "stop"}
_NEAR_NEGATIONS = {"no", "nobody", "nothing"}  # only count right next to the verb or target ("no need", "no agent")
_MONEY = {"cost", "price", "charge", "charges", "fee", "free", "much", "rate", "rates"}
_NEGATION_PHRASES = ("do not", "don t", "no need", "not need", "don t need", "dont need")

# --- Bangla (script) and Banglish (Latin script) ----------------------------------------------------------
_BN_TARGETS = ("এজেন্ট", "এজেন্টের", "কাস্টমার কেয়ার", "কাস্টমার সার্ভিস", "কাস্টমার সাপোর্ট", "প্রতিনিধি", "মানুষ", "মানুষের", "অপারেটর", "কর্মকর্তা", "হিউম্যান")
_BN_VERBS = ("কথা", "বলতে", "সংযোগ", "কানেক্ট", "ট্রান্সফার", "দিন", "দেন", "চাই", "লাগবে")
_BN_NEG = ("চাই না", "দরকার নেই", "লাগবে না", "না")
_BL_TARGETS = ("agent", "manush", "manusher", "representative", "customer care", "customer support", "human", "operator")
_BL_VERBS = ("kotha", "bolte", "bolbo", "connect", "transfer", "chai", "lagbe", "dao", "den", "koren", "korun")
_BL_NEG = ("chai na", "lagbe na", "dorkar nei", "dorkar nai")


def normalise(text: str) -> str:
    """Lower case, keep letters (including Bangla) and digits, turn everything else into single spaces."""
    text = unicodedata.normalize("NFKC", text or "").lower().replace("'", " ").replace("\u2019", " ")
    text = re.sub(r"[^\w\u0980-\u09ff]+", " ", text, flags=re.UNICODE).replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()


def _find(words: list[str], phrase: str) -> list[int]:
    """Start indexes where `phrase` (one or more words) occurs in `words`."""
    parts = phrase.split()
    n = len(parts)
    return [i for i in range(len(words) - n + 1) if words[i:i + n] == parts]


def _negated(words: list[str], at: int, window: int = 7) -> bool:
    before = words[max(0, at - window):at]
    if any(w in _NEGATIONS for w in before) or any(w in _NEAR_NEGATIONS for w in words[max(0, at - 2):at]):
        return True
    joined = " ".join(before)
    return any(p in joined for p in _NEGATION_PHRASES)


_CLAUSE_SPLIT = re.compile(r"[.!?;:,\n\u0964]+|\b(?:so|but|because|then|however|anyway)\b", re.IGNORECASE)
# "support" and "care" alone are too broad for "I need support with my SIM"; they only count after a strong verb.
_WEAK_TARGETS = ("support", "care", "helpdesk", "help desk", "service desk")
_REPORTING = {"told", "said", "says", "tells", "asked", "mentioned", "called", "promised", "replied"}


def detect_human_request(text: str, extra_phrases: Sequence[str] = ()) -> HumanRequest:
    """Check each clause on its own, so a negation or a question about a number in ANOTHER clause cannot hide or
    fake a request: "it's not working, so can I contact a human agent?" is a request; "I don't want a human, I
    want to check my balance" is not."""
    for clause in _CLAUSE_SPLIT.split(text or ""):
        found = _detect_clause(clause, extra_phrases)
        if found.matched:
            return found
    return NO_MATCH


def _detect_clause(text: str, extra_phrases: Sequence[str]) -> HumanRequest:
    norm = normalise(text)
    if not norm:
        return NO_MATCH
    words = norm.split()
    padded = f" {norm} "

    # Bangla script and Banglish: a target and a verb in the same clause, no negation.
    if any(t in norm for t in _BN_TARGETS) and any(v in norm for v in _BN_VERBS):
        if not any(n in norm for n in _BN_NEG):
            return HumanRequest(True, "bangla")
    if any(f" {t} " in padded for t in _BL_TARGETS) and any(f" {v} " in padded for v in _BL_VERBS):
        if not any(f" {n} " in padded for n in _BL_NEG) and not _negated(words, len(words)):
            return HumanRequest(True, "banglish")

    info = bool(set(words) & _INFO_WORDS)
    starts_like_question = any(norm.startswith(s) for s in _INFO_STARTS)

    # Configured phrases (HUMAN_HANDOFF_PHRASES): whole-word match, same negation rule.
    for phrase in extra_phrases:
        p = normalise(phrase)
        for at in _find(words, p) if p else []:
            if not _negated(words, at):
                return HumanRequest(True, "configured_phrase")

    # direct
    for phrase in _DIRECT:
        for at in _find(words, phrase):
            if not _negated(words, at) and not info:
                return HumanRequest(True, "direct")

    # bare: "agent", "human", "representative please"
    if len(words) <= 3 and any(w in ("human", "agent", "representative", "operator") for w in words):
        if not (set(words) & (_NEGATIONS | _NEAR_NEGATIONS)) and not info:
            return HumanRequest(True, "bare")

    targets = [(i, t) for t in _TARGETS for i in _find(words, t)]
    weak = [(i, t) for t in _WEAK_TARGETS for i in _find(words, t)]
    if not targets and not weak:
        return NO_MATCH

    # "the agent told me to speak to my manager" reports what someone else said: not a request
    reported_at = [t_at for t_at, t in targets if t_at + len(t.split()) < len(words) and words[t_at + len(t.split())] in _REPORTING]

    # strong: request verb up to 8 words before a target ("connect me with support", "talk to customer care")
    for verb in _STRONG_VERBS:
        for v_at in _find(words, verb):
            if _negated(words, v_at) or (info and verb in _AMBIGUOUS_STRONG) or (set(words) & _MONEY):
                continue
            if any(r_at < v_at for r_at in reported_at):
                continue
            for t_at, _ in targets + weak:
                between = words[v_at + 1:t_at]
                if 0 < t_at - v_at <= 8 and "about" not in between and "regarding" not in between:
                    return HumanRequest(True, "strong")

    # reversed word order: the clause starts with the target ("customer care, I want to talk to them")
    for t_at, t in targets:
        if t_at <= 2 and t not in ("someone", "somebody", "person", "manager", "staff"):
            nxt = words[t_at + len(t.split())] if t_at + len(t.split()) < len(words) else ""
            if nxt in _REPORTING or info or set(words) & _MONEY:
                continue
            for verb in _STRONG_VERBS + _MEDIUM_VERBS:
                if any(0 < v_at - t_at <= 8 and not _negated(words, v_at) for v_at in _find(words, verb)):
                    return HumanRequest(True, "reversed")

    # medium: only for statements, not questions about numbers, hours or prices
    if not info and not starts_like_question:
        for verb in _MEDIUM_VERBS:
            for v_at in _find(words, verb):
                if _negated(words, v_at):
                    continue
                if any(0 < t_at - v_at <= 5 and t not in ("someone", "somebody", "person", "manager") for t_at, t in targets):
                    return HumanRequest(True, "medium")
    return NO_MATCH
