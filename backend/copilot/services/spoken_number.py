"""Find a Bangladesh mobile number in what people say on a call.

Speech-to-text writes a number in many ways: "01712345678", "০১৭১২৩৪৫৬৭৮", "0171 234 5678",
"zero one seven ...", "shunno ek sat ..." or "+880 1712 345678". Digits and digit words that follow each
other are joined, and the joined run is searched for 01 followed by 9 more digits. A number needs 11
digits in a row, so ordinary words such as "ek" or "char" do not cause a false match.
"""
import re

_BANGLA_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
_SPLIT = re.compile(r"[\s,.;:!?\"'()\[\]{}\-/+।]+")
_NUMBER = re.compile(r"(?<!\d)(?:88)?(01\d{9})(?!\d)")

_WORDS = {
    "0": ("zero", "shunno", "shuno", "sunno", "শূন্য"),
    "1": ("one", "ek", "এক"),
    "2": ("two", "dui", "doi", "দুই"),
    "3": ("three", "tin", "তিন"),
    "4": ("four", "char", "chaar", "চার"),
    "5": ("five", "pach", "panch", "pnach", "পাঁচ"),
    "6": ("six", "choy", "chhoy", "ছয়"),
    "7": ("seven", "sat", "shat", "সাত"),
    "8": ("eight", "aat", "at", "আট"),
    "9": ("nine", "noy", "নয়"),
}
_WORD_TO_DIGIT = {word: digit for digit, words in _WORDS.items() for word in words}


def _digits_of(token: str) -> str | None:
    """The digits a token stands for, or None when it is not a number word."""
    token = token.translate(_BANGLA_DIGITS).lower()
    if token.isascii() and token.isdigit():
        return token
    return _WORD_TO_DIGIT.get(token)


def mobile_numbers(text: str) -> list[str]:
    """Every mobile number in the text, as 01XXXXXXXXX, in the order they were said."""
    runs: list[str] = []
    current: list[str] = []
    for token in _SPLIT.split(text or ""):
        if not token:
            continue
        digits = _digits_of(token)
        if digits is None:
            if current:
                runs.append("".join(current))
                current = []
        else:
            current.append(digits)
    if current:
        runs.append("".join(current))
    return [m.group(1) for run in runs for m in _NUMBER.finditer(run)]
