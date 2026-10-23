"""Arabic sentence splitting (spec 4.5), Franco normalization (10) and box validation (7)."""

import re
import unicodedata

# Harakat (fatha, damma, kasra, shadda, sukun, tanween...), superscript alef, and tatweel.
_ARABIC_MARKS = re.compile(r"[ً-ْٰـ]")
_ARABIC_LETTERS = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
# Phones often turn ' into a curly apostrophe; treat them all as one.
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "`": "'"})
_ALLOWED = re.compile(r"^[A-Za-z0-9'\-\s]+$")

MAX_BOX_LENGTH = 100


def split_words(text: str) -> list[str]:
    """Strip punctuation (، ؟ ! . , « » etc.), then split on whitespace."""
    no_punct = "".join(" " if unicodedata.category(c).startswith("P") else c for c in text)
    return no_punct.split()


def non_arabic_words(words: list[str]) -> list[str]:
    """Words with no Arabic letters (e.g. English pasted into the Arabic column)."""
    return [w for w in words if not _ARABIC_LETTERS.search(w)]


def normalize_sentence(text: str) -> str:
    """Key for the duplicate check: same words, ignoring punctuation, spacing and harakat."""
    return " ".join(split_words(_ARABIC_MARKS.sub("", text)))


def clean(text: str) -> str:
    """The `franco` column: trimmed, inner whitespace collapsed, lowercase, straight apostrophes."""
    return " ".join(text.translate(_APOSTROPHES).split()).lower()


# Five or more neighbouring keys in a row, e.g. "asdfg", "qwerty", "hjkl;" (without the ;).
_KEYBOARD_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")
_MASH_LENGTH = 5


def _has_keyboard_run(text: str) -> bool:
    text = text.lower()
    for row in _KEYBOARD_ROWS:
        for line in (row, row[::-1]):
            for i in range(len(line) - _MASH_LENGTH + 1):
                if line[i : i + _MASH_LENGTH] in text:
                    return True
    return False


def flag_reason(raw: str, arabic: str, length_ratio: float, length_slack: int, repeat_run: int) -> str | None:
    """Why an accepted answer looks like junk and should be flagged for review (spec 7), or None.

    Flagged answers are still saved: stretched words like "ya3niiii" are real typing.
    """
    franco = clean(raw).replace(" ", "")
    arabic_len = len(_ARABIC_MARKS.sub("", arabic).replace(" ", ""))
    if len(franco) > length_ratio * arabic_len + length_slack:
        return f"Much longer than the Arabic ({len(franco)} vs {arabic_len} letters)"
    run = re.search(r"(.)\1{%d,}" % (repeat_run - 1), franco)
    if run:
        return f'Repeated character ("{run.group(0)}")'
    if _has_keyboard_run(franco):
        return "Looks like keyboard mashing"
    return None


def franco_error(raw: str, arabic: str = "") -> str | None:
    """Why a Franco box for `arabic` must be rejected, or None if it's acceptable."""
    if not raw.strip():
        return "Fill in this box"
    if _ARABIC_LETTERS.search(raw):
        return "Write it in Franco, not Arabic"
    if len(raw) > MAX_BOX_LENGTH:
        return f"Too long (max {MAX_BOX_LENGTH} characters)"
    if not re.search(r"[A-Za-z0-9]", raw):
        return "Use English letters or numbers"
    if not _ALLOWED.match(raw.translate(_APOSTROPHES)):
        return "Only English letters, numbers, ' and - are allowed"
    # Numbers alone can be a real abbreviation ("72", "45"), but one digit repeated
    # ("55", "22222") isn't, unless the Arabic word is itself a number.
    digits = re.sub(r"[\s'\-]", "", raw)
    if (digits.isdigit() and len(digits) > 1 and len(set(digits)) == 1
            and not re.search(r"\d", arabic)):
        return "Repeated numbers aren't a spelling"
    return None
