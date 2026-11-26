"""The language of a call, and the instruction that makes generated text follow it.

Three languages, decided from what the CUSTOMER says (all lines when the customer has not spoken yet):
* bangla   : Bengali script, with at most a few English words ("আমার নেট কাজ করছে না").
* banglish : Bangla written in English letters ("amar net kaj korche na"), or Bengali script mixed with many English
             words ("আমার internet speed slow, data pack check করেন"), which is how mixed speech comes out of the
             speech recogniser.
* english  : English.

The summary, the ticket draft (filled from the summary), live tips, suggestions and assist answers are all written
in the call's language. Product names, pack names, codes, prices and numbers stay as written.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

BANGLA, BANGLISH, ENGLISH = "bangla", "banglish", "english"
LANGUAGES = (BANGLA, BANGLISH, ENGLISH)

# Words in Bengali script from this share on make the text Bengali-script speech (Bangla, or Banglish when mixed).
BENGALI_WORD_SHARE = 0.2
# Share of English words in Bengali-script speech from which the call counts as Banglish (mixed).
MIXED_ENGLISH_SHARE = 0.4
# Latin text: share of words (or count) that are common Bangla words written in English letters.
BANGLISH_WORD_SHARE = 0.2
BANGLISH_WORD_COUNT = 3

# Common Bangla words as they are typed in English letters. Words that are also English ("to", "a", "re") are left
# out so English sentences are not mistaken for Banglish.
BANGLISH_WORDS = frozenset("""
ami amar amake amader amra apni apnar apnake apnara tumi tomar tomake se tar tara oder ora uni unar
ki keno kemon kothay kobe koto kotokhon kivabe kibhabe kon kono keu kichu kisu
na nai nei naki hya haan ha ji jee accha acha thik thak thake theke
ache achhe achi achen chilo chhilo hobe hoy hoye hoyeche hoyche hoise hocche hochhe hochche holo hoini
kori korchi korche korchen korte korben koren korun kore korlam korsi korechi korbo korar korle
kaj kotha bolen bolchi bolte bolbo bolo bollam bolsi bujhi bujhte bujhchi parchi parbo pari parben parina
dao den din dite diye dilam disi dibo nite nibo nilam nisi nao nen chai chaichi lagbe lage laglo lagche
ekhon akhon ekhono kintu tai jonno shudhu sudhu onek khub beshi kom valo bhalo kharap sathe songe
ekta ekti ekdom ektu abar ar arekta tarpor pore age agee boro choto shob sob shobai
taka toka mash maash ghonta shomoy somoy din raat sokal bikal
jabe jai jacchi jaccha jaben jete ashe asche aste eshe gelo gese geche giyeche
dekhen dekhi dekhchi dekhun dekhte shunen shunun shuni janen jani janina janai jante
amr apnr plz vai bhai apu bhaiya
""".split())

_LATIN_LETTER = re.compile(r"[A-Za-z]")
_WORD = re.compile(r"[A-Za-z]+|[\u0980-\u09FF]+")

NAMES = {BANGLA: "Bangla", BANGLISH: "Banglish", ENGLISH: "English"}


def detect(texts: Iterable[str]) -> str:
    """The language of these lines: bangla, banglish or english (english when there is nothing to go on)."""
    words = _WORD.findall(" ".join(t for t in texts if t))
    if not words:
        return ENGLISH
    latin_words = [w.lower() for w in words if _LATIN_LETTER.match(w)]
    bengali_words = len(words) - len(latin_words)
    if bengali_words and bengali_words >= BENGALI_WORD_SHARE * len(words):
        # Bengali script: mostly Bangla, or mixed with many English words (Banglish).
        return BANGLISH if len(latin_words) / len(words) >= MIXED_ENGLISH_SHARE else BANGLA
    hits = sum(1 for w in latin_words if w in BANGLISH_WORDS)
    if hits >= BANGLISH_WORD_COUNT or (hits >= 2 and hits / len(latin_words) >= BANGLISH_WORD_SHARE):
        return BANGLISH
    return ENGLISH


_KEEP = "Keep product names, pack names, USSD codes, prices, dates and numbers exactly as written."
RULES = {
    ENGLISH: "Write it in English.",
    BANGLA: "Write it in Bangla, in Bengali script (বাংলা). Do not write English sentences. " + _KEEP,
    BANGLISH: (
        "Write it in Banglish: Bangla written in English (Latin) letters, the way Bangladeshi customers type it, "
        "for example \"Customer er internet kaj korche na, area te network er kaj cholche.\" "
        "Do not use Bengali script and do not write plain English sentences. " + _KEEP
    ),
}


def rule(language: str) -> str:
    """The instruction that goes into a prompt so the model writes in the call's language."""
    return RULES.get(language, RULES[ENGLISH])


def pick(texts: dict[str, str], language: str) -> str:
    """The text for this language from {"english": ..., "bangla": ..., "banglish": ...}; English when missing."""
    return texts.get(language) or texts[ENGLISH]
