"""Comparing text that was written twice.

Two problems in this pipeline reduce to the same question — is this the same
sentence as that one? Checking that a quoted passage really appears on the page
it was cited from, and recognising that a claim in today's research is one the
registry already verified last month.

Neither can be answered by comparing strings. The same fact is written with
different spacing, different quotation marks, Persian digits one day and ASCII
the next, and — because Persian and Arabic share a script but not a keyboard —
with two different characters that look identical on screen. So everything is
flattened to one form first, and compared after.

Nothing here is language-specific in the sense that matters: the folding rules
are about writing systems, not about any particular site or subject.
"""

from __future__ import annotations

import re
import unicodedata

# Digits that mean the same number in a different script. A specification
# quoted with Persian digits and the page that states it in ASCII are the same
# claim, and a check that says otherwise is worse than no check.
DIGITS = {
    **{chr(0x06F0 + i): str(i) for i in range(10)},  # Persian
    **{chr(0x0660 + i): str(i) for i in range(10)},  # Arabic-Indic
    **{chr(0x0966 + i): str(i) for i in range(10)},  # Devanagari
}

# Letters that are one letter in practice and two in Unicode. Arabic keyboards
# produce the first of each pair, Persian ones the second, and readers cannot
# tell them apart.
LETTERS = {
    "ي": "ی",  # Arabic yeh  -> Farsi yeh
    "ى": "ی",  # alef maksura
    "ك": "ک",  # Arabic kaf  -> Keheh
    "ة": "ه",  # noqa: RUF001 - teh marbuta -> heh; look-alikes are the point here
    "‌": " ",  # zero-width non-joiner: a word break that prints as none
    "‏": " ",
    "‎": " ",
    "ـ": "",  # tatweel: decoration, never meaning
}

# Vowel marks and other combining decoration, which almost nobody types
# consistently.
HARAKAT = re.compile(r"[ً-ّْٰ]")
PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
WHITESPACE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """One canonical form: same script, same digits, no decoration."""
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    folded = folded.translate(str.maketrans({**DIGITS, **LETTERS}))
    folded = HARAKAT.sub("", folded)
    return WHITESPACE.sub(" ", PUNCTUATION.sub(" ", folded)).strip()


def tokens(text: str) -> list[str]:
    return normalize_text(text).split()


def shingles(words: list[str], size: int = 5) -> set[str]:
    """Overlapping runs of words.

    A passage that was tidied — a comma moved, a word dropped — still shares
    most of its runs with the original. Comparing runs rather than whole
    sentences is what makes the difference between quoting and paraphrasing
    visible without asking a model to judge it.
    """
    if len(words) <= size:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + size]) for i in range(len(words) - size + 1)}


def overlap(passage: str, haystack: str, size: int = 5) -> float:
    """What fraction of the passage's word-runs appear in the larger text."""
    wanted = shingles(tokens(passage), size)
    if not wanted:
        return 0.0
    found = shingles(tokens(haystack), size)
    return len(wanted & found) / len(wanted)


def word_coverage(passage: str, haystack: str) -> float:
    """What fraction of the passage's words appear in the larger text at all.

    Word runs catch quotation; this catches the same sentence rebuilt in a
    different order, which is what a careful writer produces when asked for a
    passage and working from a page they have already read. On its own it
    proves little — vocabulary repeats — which is why the caller pairs it with
    the numbers, where agreement is not a coincidence.
    """
    wanted = set(tokens(passage))
    if not wanted:
        return 0.0
    return len(wanted & set(tokens(haystack))) / len(wanted)


NUMBER = re.compile(r"\d[\d.,]*\d|\d")


def numbers_in(text: str) -> set[str]:
    """Every number in the text, in one script and without its grouping.

    Numbers are the part of a claim a reader acts on, and the part most likely
    to have been carried over wrongly, so they are compared separately from the
    words around them. Thousands separators go: a page writing 12,000 and a
    claim writing 12000 are stating the same figure.
    """
    digits_only = (text or "").translate(str.maketrans(DIGITS))
    found = set()
    for match in NUMBER.findall(digits_only):
        cleaned = match.replace(",", "").rstrip(".")
        if cleaned:
            found.add(cleaned)
    return found


def claim_key(claim: str) -> str:
    """The identity of a claim, for a registry that must not hold it twice.

    Deliberately blunt: two claims with the same words in the same order are
    the same claim, whatever their punctuation. Two that differ by a number are
    not — which is the important case, because that is what happens when a
    specification changes.
    """
    return normalize_text(claim)[:500]
