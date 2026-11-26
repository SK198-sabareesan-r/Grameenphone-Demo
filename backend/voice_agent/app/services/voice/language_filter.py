"""Keep only Bangla, Banglish (romanized Bangla) and English transcript text.

Bangla is written in the Bengali script; Banglish and English are written in Latin letters. Any
letter from another script (Malayalam, Hindi/Devanagari, Tamil, Arabic, ...) means the speech was
in another language, and the whole utterance is rejected. Digits, punctuation, symbols and
whitespace are neutral.

This is a script check, not language identification: romanized text in another language
cannot be told apart from Banglish here.
"""

from __future__ import annotations

import unicodedata

_ALLOWED_SCRIPT_PREFIXES = ("LATIN", "BENGALI")


def _script_ok(ch: str) -> bool:
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return False
    return name.startswith(_ALLOWED_SCRIPT_PREFIXES)


def is_allowed_language_text(text: str | None) -> bool:
    """True if every letter (and combining mark) in `text` is Latin or Bengali."""
    if not text:
        return True
    for ch in text:
        category = unicodedata.category(ch)
        if category[0] in ("L", "M") and not _script_ok(ch):
            return False
        # Zero-width joiners appear inside Bengali conjuncts; they are neutral.
    return True


def parse_allowed_languages(value: str) -> frozenset[str]:
    """"bn-IN, en-IN" -> {"bn-in", "en-in"} (compared case-insensitively). Empty = every language."""
    return frozenset(code.strip().lower() for code in (value or "").split(",") if code.strip())


def is_allowed_transcript(text: str | None, language: str | None, allowed: frozenset[str]) -> bool:
    """Keep a transcript only if Sarvam's detected language (sent with language_code=auto) is allowed and the text
    is in Bengali or Latin script. No detected language (a fixed language_code) = judged by the script alone."""
    if language and allowed and language.strip().lower() not in allowed:
        return False
    return is_allowed_language_text(text)
