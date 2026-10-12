"""Small, pure text helpers for turning slide wording into spoken narration - no I/O (rule 1).

Slide text is written to be *read*: it is made of fragments, symbols and abbreviations. These
helpers make it speakable without changing what it says. They never add a fact, a figure or a
name: the only words they introduce are connectives (``and``, ``percent``, ``for example``), which
is what lets the lesson generator hold its narration to the same grounding rule as everything else
(``domain.script_provenance``).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

_ORDINALS: Final = (
    "first",
    "second",
    "third",
    "fourth",
    "fifth",
    "sixth",
    "seventh",
    "eighth",
    "ninth",
    "tenth",
)

_NUMBER_WORDS: Final = (
    "no",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
)

# Words that begin a sentence only because a sentence begins there, so they can be lower-cased when
# a connective is put in front of them.
_ABBREVIATIONS: Final = (
    ("e.g.", "for example"),
    ("i.e.", "that is"),
    ("etc.", "and so on"),
    ("vs.", "versus"),
    ("vs ", "versus "),
    ("approx.", "approximately"),
    ("&", " and "),
    ("→", " to "),
    ("->", " to "),
    ("=>", " leads to "),
    ("≥", " at least "),
    ("≤", " at most "),
    ("•", " "),
    ("▪", " "),
    ("●", " "),
    ("✓", " "),
    ("✔", " "),
    ("★", " "),
    ("…", "."),
)

_DASHES = re.compile(r"\s+[-–—]\s+|\s*[–—]\s*")
_BRACKETS = re.compile(r"\s*[()\[\]]\s*")
_SPACES = re.compile(r"\s+")
_REPEATED_PUNCTUATION = re.compile(r"([,.;:!?])(?:\s*[,.;:])+")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_TRAILING_JUNK = re.compile(r"[\s,;:\-–—]+$")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[\"“(\[]?[A-Z0-9])")
_ENDS_SENTENCE = re.compile(r"[.!?][\"”')\]]*$")
_KEPT_ABBREVIATIONS = (
    "e.g.",
    "i.e.",
    "etc.",
    "vs.",
    "dr.",
    "mr.",
    "mrs.",
    "ms.",
    "no.",
    "approx.",
    "fig.",
    "inc.",
    "ltd.",
)


def split_sentences(text: str) -> list[str]:
    """Split running prose into sentences, without breaking after a common abbreviation."""
    pieces = [piece.strip() for piece in _SENTENCE_BREAK.split(text) if piece.strip()]
    merged: list[str] = []
    for piece in pieces:
        if merged and merged[-1].casefold().endswith(_KEPT_ABBREVIATIONS):
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)
    return merged


def word_count(text: str) -> int:
    return len(text.split())


def ordinal_word(position: int) -> str:
    """``first`` for 0, ``second`` for 1 ... then ``next`` once the list outgrows the words."""
    return _ORDINALS[position] if 0 <= position < len(_ORDINALS) else "next"


def number_word(count: int) -> str:
    return _NUMBER_WORDS[count] if 0 <= count < len(_NUMBER_WORDS) else "several"


def speakable(text: str) -> str:
    """Slide wording made fit to be read aloud: symbols and abbreviations spelt out, dashes and
    brackets turned into pauses, stray punctuation tidied. Case and every number are kept."""
    spoken = text
    for written, said in _ABBREVIATIONS:
        spoken = spoken.replace(written, said)
    spoken = _DASHES.sub(", ", spoken)
    spoken = _BRACKETS.sub(", ", spoken)
    spoken = _SPACES.sub(" ", spoken)
    spoken = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", spoken)
    spoken = _REPEATED_PUNCTUATION.sub(r"\1", spoken)
    return _TRAILING_JUNK.sub("", spoken).strip()


def capitalise_first(text: str) -> str:
    for position, character in enumerate(text):
        if character.isalpha():
            return text[:position] + character.upper() + text[position + 1 :]
        if character.isdigit():
            return text
    return text


def lower_first(text: str) -> str:
    """Lower-case the first word so it can follow a connective - unless it is an acronym, a
    mixed-case name or the pronoun *I*, which a lower-case first letter would damage."""
    head, _, tail = text.partition(" ")
    letters = [character for character in head if character.isalpha()]
    if not letters:
        return text
    if (
        (len(letters) > 1 and head.isupper())
        or any(c.isupper() for c in head[1:])
        or head
        in {
            "I",
            "I'm",
            "I'll",
            "I've",
            "I'd",
        }
    ):
        return text
    lowered = head[0].lower() + head[1:]
    return f"{lowered} {tail}".strip()


def ensure_sentence(text: str) -> str:
    """``text`` as one sentence: capitalised, with a full stop unless it already ends one."""
    cleaned = _TRAILING_JUNK.sub("", text.strip())
    if not cleaned:
        return ""
    cleaned = capitalise_first(cleaned)
    return cleaned if _ENDS_SENTENCE.search(cleaned) else cleaned + "."


def join_list(items: Sequence[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


_CLAUSE_BREAKS = (": ", " - ", " – ", " — ", "; ", ", ", " (", " and ", " or ")


def fit_text(text: str, max_chars: int) -> str:
    """``text`` cut to at most ``max_chars``: whole if it fits, otherwise at the last natural
    break (a colon, comma, bracket) that leaves a useful amount, otherwise at a word with an
    ellipsis. Always a prefix of the original, so every word in it is the slide's own."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    window = text[: max_chars + 1]
    best = -1
    for separator in _CLAUSE_BREAKS:
        position = window.rfind(separator)
        if position >= max_chars * 0.45:
            best = max(best, position)
    if best >= 0:
        return text[:best].rstrip(" ,;:-–—(")
    cut = window[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:-–—(")
    return (cut or window[: max_chars - 1]).rstrip() + "…"
