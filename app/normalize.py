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


NUMBER = re.compile(r"\d[\d.,]*\d|\d")

# Separators that sit between the digits of one number rather than between two
# numbers. Persian and Arabic writing use their own, and a check that does not
# fold them reads 5/12 as two numbers and then reports that the page never
# stated 5.12 — which is the page being right and the check being wrong.
NUMBER_SEPARATORS = {
    "\u066b": ".",  # Arabic decimal separator
    "\u066c": ",",  # Arabic thousands separator
}
# What a number may be written with, so that a figure and the word stuck to it
# are read as one token and can be judged together.
TOKEN = re.compile(r"[\w.,\-/]+", re.UNICODE)
LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
# A digit with letters on both sides, which is how a part number reads and how
# a measurement never does.
INTERLEAVED = re.compile(r"\d[^\W\d_]+\d|[^\W\d_]\d+[^\W\d_]", re.UNICODE)


def is_identifier(token: str) -> bool:
    """Whether this is a name that happens to contain digits.

    `SUN-6K-OG01LP1-EU-AM2` states no quantity: nothing on any page can confirm
    the 01 in it, so a claim that named a model used to be unverifiable for as
    long as it named one. Read narrowly — letters and digits joined by a hyphen,
    or digits with letters on both sides — because 5kWh and 12V are figures a
    page really can confirm, and dropping those would only move the blindness.
    """
    if not LETTER.search(token) or not any(c.isdigit() for c in token):
        return False
    return "-" in token or "_" in token or bool(INTERLEAVED.search(token))


def numbers_in(text: str) -> set[str]:
    """Every number in the text, in one script and without its grouping.

    Numbers are the part of a claim a reader acts on, and the part most likely
    to have been carried over wrongly, so they are compared separately from the
    words around them. Thousands separators go: a page writing 12,000 and a
    claim writing 12000 are stating the same figure.

    Identifiers are dropped whole, and the same rules run over the claim and
    over the page. A rule applied to one side only is how a passage that is
    really on the page gets reported as absent from it.
    """
    folded = (text or "").translate(str.maketrans({**DIGITS, **NUMBER_SEPARATORS}))
    found = set()
    for token in TOKEN.findall(folded):
        if is_identifier(token):
            continue
        # One slash between digits is a Persian decimal point; several are a
        # date, and a date's parts are three numbers rather than one.
        if token.count("/") == 1:
            token = token.replace("/", ".")
        for match in NUMBER.findall(token):
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
