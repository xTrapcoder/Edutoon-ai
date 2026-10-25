"""Pasted text as source material - pure domain logic (rule 1: no I/O, no configuration reads).

A project may be built from text its owner pasted in instead of an uploaded file. The text is
cleaned, checked against fixed bounds and cut into chunks of about the size of a PDF page, which
then flow through exactly the same indexing, claim verification and script generation as a PDF's
pages do. A chunk has no page number: its position in the text is its ``chunk_index``.

This module only decides *what the chunks are*. It never judges whether the text is true - that is
the evidence pipeline's job, against these very chunks.
"""

from __future__ import annotations

import re
from typing import Final

MIN_CHARS: Final = 300
"""The least text worth building a video from, counted after cleaning."""

MAX_CHARS: Final = 200_000
"""The most text accepted, counted after cleaning (about 90 printed pages)."""

CHUNK_CHARS: Final = 2_500
"""The most characters in one chunk; a PDF page averages about 2,200 and rarely exceeds 3,000."""

DEFAULT_TITLE: Final = "Pasted text"
"""The name recorded for a source whose owner gave none."""


class PastedTextError(ValueError):
    """The pasted text cannot be used. The message says why, in words for its owner."""


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BLANK_RUNS = re.compile(r"\n{3,}")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def clean(text: str) -> str:
    """Normalise ``text``: one newline convention, no control characters, no trailing spaces on a
    line, at most one blank line in a row, no leading or trailing whitespace. Idempotent."""
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    without_controls = _CONTROL.sub("", unified)
    lines = [line.rstrip() for line in without_controls.split("\n")]
    return _BLANK_RUNS.sub("\n\n", "\n".join(lines)).strip()


def validate(cleaned: str) -> None:
    """Raise :class:`PastedTextError` unless ``cleaned`` (the result of :func:`clean`) is long
    enough, short enough and contains words."""
    if not any(character.isalpha() for character in cleaned):
        raise PastedTextError("The pasted text has no words in it. Paste the material to explain.")
    if len(cleaned) < MIN_CHARS:
        raise PastedTextError(
            f"The pasted text is too short: {len(cleaned)} characters, and at least "
            f"{MIN_CHARS} are needed to check a video against."
        )
    if len(cleaned) > MAX_CHARS:
        raise PastedTextError(
            f"The pasted text is too long: {len(cleaned)} characters, and the limit is "
            f"{MAX_CHARS}. Paste the parts you need."
        )


def split_chunks(cleaned: str, *, limit: int = CHUNK_CHARS) -> list[str]:
    """Cut ``cleaned`` into non-empty chunks of at most ``limit`` characters.

    Whole paragraphs are packed together while they fit. A paragraph longer than ``limit`` is cut
    at sentence ends, and a sentence longer than ``limit`` at the last space before it, and only
    with no space at all at exactly ``limit`` characters. The result is a pure function of the
    text: the same text always gives the same chunks, and no word is lost or repeated.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1.")
    chunks: list[str] = []
    current = ""
    for paragraph in (part for part in cleaned.split("\n\n") if part.strip()):
        for piece in _fit(paragraph.strip(), limit):
            joined = f"{current}\n\n{piece}" if current else piece
            if len(joined) <= limit:
                current = joined
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks


def _fit(paragraph: str, limit: int) -> list[str]:
    """``paragraph`` as pieces of at most ``limit`` characters, none empty."""
    if len(paragraph) <= limit:
        return [paragraph]
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(paragraph):
        for part in _hard_split(sentence, limit):
            joined = f"{current} {part}" if current else part
            if len(joined) <= limit:
                current = joined
            else:
                pieces.append(current)
                current = part
    if current:
        pieces.append(current)
    return pieces


def _hard_split(sentence: str, limit: int) -> list[str]:
    parts: list[str] = []
    rest = sentence
    while len(rest) > limit:
        cut = rest.rfind(" ", 0, limit + 1)
        if cut < 1:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts
