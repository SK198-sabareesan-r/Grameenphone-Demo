"""Find a mobile number in what people say on a call.

Speech-to-text writes a number in many ways: "01712345678", "9500799295", "০১৭১২৩৪৫৬৭৮", "9500 799 295",
"zero one seven ...", "shunno ek sat ...", "nine five double zero ..." or "+91 95007 99295". Digits and digit
words that follow each other are joined, and the joined run is searched for a number:
* Bangladesh: 01 followed by 9 more digits (11 digits), with an optional 88 in front.
* India (the demo caller): 6, 7, 8 or 9 followed by 9 more digits (10 digits), with an optional 91 or 0 in front.
A run must hold a whole number, so ordinary words such as "ek" or "char" do not cause a false match.
"""
import re

_BANGLA_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
_SPLIT = re.compile(r"[\s,.;:!?\"'()\[\]{}\-/+।]+")
_NUMBER = re.compile(r"(?<!\d)(?:(?:88)?(01\d{9})|(?:91|0)?([6-9]\d{9}))(?!\d)")

_WORDS = {
    "0": ("zero", "shunno", "shuno", "sunno", "শূন্য", "জিরো"),
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
# "double nine" is 99 and "triple one" is 111, as people read out numbers.
_REPEATERS = {"double": 2, "dabal": 2, "ডাবল": 2, "triple": 3, "tripal": 3, "ট্রিপল": 3}


def _digits_of(token: str) -> str | None:
    """The digits a token stands for, or None when it is not a number word."""
    token = token.translate(_BANGLA_DIGITS).lower()
    if token.isascii() and token.isdigit():
        return token
    return _WORD_TO_DIGIT.get(token)


def _digit_runs(text: str) -> list[str]:
    """Runs of digits and digit words that follow each other, joined into one string each."""
    runs: list[str] = []
    current: list[str] = []
    repeat = 1
    for token in _SPLIT.split(text or ""):
        if not token:
            continue
        if token.lower() in _REPEATERS:
            repeat = _REPEATERS[token.lower()]
            continue
        digits = _digits_of(token)
        if digits is None:
            if current:
                runs.append("".join(current))
                current = []
        else:
            current.append(digits * repeat if len(digits) == 1 else digits)
        repeat = 1
    if current:
        runs.append("".join(current))
    return runs


def mobile_numbers(text: str) -> list[str]:
    """Every mobile number in the text, in the order they were said.
    Bangladesh numbers are returned as 01XXXXXXXXX and Indian ones as the 10 digits."""
    return [m.group(1) or m.group(2) for run in _digit_runs(text) for m in _NUMBER.finditer(run)]
