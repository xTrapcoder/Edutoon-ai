"""What a PDF's pages hold, and when a page needs reading by OCR - pure domain logic (rule 1: no
I/O, no configuration reads).

A PDF's own text layer is read first (``providers.pdf``). A page whose layer yields next to nothing
is a *scan* (its content is a picture of text), a blank page, or a figure with no caption. Local
text recognition (``providers.ocr``) is tried on those pages only, and what it returns is held to
the same bar as any other text: a page that comes back with no words is left out, never filled in.

Nothing here judges whether text is true; it only decides which pages to read and tidies what
recognition returned.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Final

MIN_PAGE_LETTERS: Final = 20
"""Fewer letters than this on a page and it is treated as having no text of its own: a page number,
a running header, or nothing. A real page of prose has hundreds."""

MIN_OCR_CONFIDENCE: Final = 0.5
"""A recognised line the engine is less sure of than this is dropped rather than kept as text: a
smudge read as a word would become a claim to verify."""

MIN_OCR_PAGE_CONFIDENCE: Final = 0.6
"""A page whose recognised lines average below this is treated as unreadable and left out. A
poor-quality scan is reported, never quietly turned into noise."""

# Recognition sometimes runs the end of one sentence into the start of the next ("clouds.When").
# A lower-case letter, a full stop and a capital cannot be an abbreviation ("U.S.", "e.g."), so a
# space is safe to put back. Words run together *inside* a sentence cannot be repaired this way.
_RUN_TOGETHER = re.compile(r"(?<=[a-z][.!?])(?=[A-Z])")
_SPACES = re.compile(r"[ \t]{2,}")


def letter_count(text: str) -> int:
    """How many alphabetic characters ``text`` has."""
    return sum(1 for character in text if character.isalpha())


def has_text(page_text: str | None) -> bool:
    """Whether a page's own text layer holds enough to count as text."""
    return page_text is not None and letter_count(page_text) >= MIN_PAGE_LETTERS


def pages_without_text(pages: Sequence[str | None]) -> list[int]:
    """The zero-based indexes of the pages whose text layer holds next to nothing, in order."""
    return [index for index, text in enumerate(pages) if not has_text(text)]


def tidy_ocr_text(lines: Iterable[str]) -> str:
    """Join recognised ``lines`` into one page of text: one line per recognised line, spaces
    normalised, sentences that ran together split again, blank lines dropped."""
    kept = []
    for line in lines:
        cleaned = _SPACES.sub(" ", line).strip()
        if cleaned:
            kept.append(_RUN_TOGETHER.sub(" ", cleaned))
    return "\n".join(kept)


def page_is_readable(text: str, mean_confidence: float) -> bool:
    """Whether one OCR'd page is good enough to use: enough letters and a confident reading."""
    return letter_count(text) >= MIN_PAGE_LETTERS and mean_confidence >= MIN_OCR_PAGE_CONFIDENCE
