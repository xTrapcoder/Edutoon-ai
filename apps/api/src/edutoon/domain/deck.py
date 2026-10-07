"""The structured representation of a slide deck - pure domain logic (rule 1: no I/O, no imports of
repositories / providers / services).

A deck is parsed once (``providers.pptx``) into :class:`DeckSlide` values, and everything that
turns a deck into a lesson (:mod:`edutoon.domain.deck_plan`) reads only these. A slide is kept as
*structure*, not as a flat string, because the generator needs to know what is a title, what is a
bullet and at which level, what is a numbered step, what is a table row and what is a speaker
note: a sentence read out of a bullet list and a sentence read out of a paragraph are narrated
differently.

Each slide is stored as one ``source_chunks`` row, so the rest of the system (citations, the
review pages, deletion) treats it exactly like a PDF page. The row carries two renderings of the
same slide:

- ``content`` - :func:`slide_text`, a readable flat text. It is what a citation shows and what
  every claim about the slide is checked against: a claim is *quoted* from a slide, so its text is
  a substring of this rendering (:func:`claim_is_quoted`).
- ``structure`` - :func:`slide_to_json`, the structure itself, versioned so a later change to it
  can still read an older deck.

Slide numbers are 1-based and are the chunk's ``page_from`` / ``page_to``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, get_args

from edutoon.domain.deck_text import lower_first

STRUCTURE_VERSION: Final = 1

BlockKind = Literal["paragraph", "bullet", "numbered", "table", "notes"]
BLOCK_KINDS: Final[frozenset[str]] = frozenset(get_args(BlockKind))

MAX_BLOCKS_PER_SLIDE: Final = 200
MAX_TEXT_CHARS: Final = 4000
MAX_TABLE_ROWS: Final = 60
MAX_TABLE_COLUMNS: Final = 12
MAX_BULLET_LEVEL: Final = 8

NOTES_PREFIX: Final = "Notes: "

_WHITESPACE = re.compile(r"\s+")
# PowerPoint stores a soft line break as a vertical tab; a zero-width character is invisible.
_INVISIBLE = re.compile("[​‌‍⁠﻿]")


class DeckStructureError(ValueError):
    """A stored slide structure is not one this version can read."""


@dataclass(frozen=True, slots=True)
class DeckBlock:
    """One piece of a slide, in reading order.

    ``text`` holds a paragraph, a bullet, a numbered step or the speaker notes; ``level`` is a
    bullet's nesting (0 is a top-level bullet); ``rows`` holds a table's cells, the first row
    being its header when there is more than one.
    """

    kind: BlockKind
    text: str = ""
    level: int = 0
    rows: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class DeckSlide:
    index: int
    title: str | None
    blocks: tuple[DeckBlock, ...]
    layout: str | None = None
    deck_title: str | None = None

    def blocks_of(self, *kinds: str) -> tuple[DeckBlock, ...]:
        return tuple(block for block in self.blocks if block.kind in kinds)

    @property
    def notes(self) -> str:
        return " ".join(block.text for block in self.blocks if block.kind == "notes")


def clean_text(text: str) -> str:
    """One line, single-spaced, with invisible characters removed."""
    return _WHITESPACE.sub(" ", _INVISIBLE.sub("", text.replace("\xa0", " "))).strip()


# --- the flat rendering --------------------------------------------------------------


def table_rows_text(rows: Sequence[Sequence[str]]) -> tuple[tuple[str, str], ...]:
    """A table as one ``(line, spoken)`` pair per data row.

    ``line`` is the row written out in the slide's flat text - ``Label - Header2: cell; Header3:
    cell`` when there is a header row (the first of two or more) and a first column of labels,
    otherwise the cells joined by semicolons. It is what a claim about the row says, so it is built
    in exactly one place. ``spoken`` is the same row worded to be read aloud: ``Label: header cell;
    header cell``, joining a header of three or more words straight to its cell (``use it on paper
    and wood``) and a short header with a colon (``price: 100``).
    """
    cleaned = [[clean_text(cell) for cell in row] for row in rows]
    cleaned = [row for row in cleaned if any(row)]
    if not cleaned:
        return ()
    if len(cleaned) == 1 or max(len(row) for row in cleaned) == 1:
        joined = tuple("; ".join(cell for cell in row if cell) for row in cleaned)
        return tuple((line, line) for line in joined)
    header, *body = cleaned
    pairs: list[tuple[str, str]] = []
    for row in body:
        label = row[0]
        written: list[str] = []
        spoken: list[str] = []
        for head, cell in zip(header[1:], row[1:], strict=False):
            if not cell:
                continue
            written.append(f"{head}: {cell}" if head else cell)
            if len(head.split()) >= 3:
                spoken.append(lower_first(f"{head} {lower_first(cell)}"))
            else:
                spoken.append(lower_first(f"{head}: {cell}" if head else cell))
        if label and written:
            pairs.append((f"{label} - " + "; ".join(written), f"{label}: " + "; ".join(spoken)))
        elif written:
            pairs.append(("; ".join(written), "; ".join(spoken)))
        elif label:
            pairs.append((label, label))
    return tuple(pairs)


def table_row_lines(rows: Sequence[Sequence[str]]) -> tuple[str, ...]:
    """The written lines of :func:`table_rows_text`."""
    return tuple(line for line, _ in table_rows_text(rows))


def slide_text(slide: DeckSlide) -> str:
    """The slide as readable flat text - the chunk's ``content``."""
    lines: list[str] = []
    if slide.title:
        lines.append(slide.title)
    step = 0
    for block in slide.blocks:
        step = step + 1 if block.kind == "numbered" else 0
        if block.kind == "paragraph":
            lines.append(block.text)
        elif block.kind == "bullet":
            lines.append("  " * block.level + "- " + block.text)
        elif block.kind == "numbered":
            lines.append("  " * block.level + f"{step}. " + block.text)
        elif block.kind == "table":
            lines.extend(table_row_lines(block.rows))
        elif block.kind == "notes":
            lines.append(NOTES_PREFIX + block.text)
    return "\n".join(line for line in lines if line.strip())


def claim_is_quoted(claim_text: str, chunk_text: str) -> bool:
    """Whether ``claim_text`` is quoted from ``chunk_text``: every word of it, in order and
    unbroken, appears in the chunk (case and spacing aside). This is the whole of verification for
    a deck: the claim *is* the slide's own wording, so there is nothing for a judge to weigh."""
    needle = clean_text(claim_text).casefold()
    return bool(needle) and needle in clean_text(chunk_text).casefold()


# --- the stored structure ------------------------------------------------------------


def slide_to_json(slide: DeckSlide) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    for block in slide.blocks:
        if block.kind == "table":
            blocks.append({"kind": "table", "rows": [list(row) for row in block.rows]})
        elif block.kind == "bullet":
            blocks.append({"kind": "bullet", "text": block.text, "level": block.level})
        else:
            blocks.append({"kind": block.kind, "text": block.text})
    return {
        "v": STRUCTURE_VERSION,
        "index": slide.index,
        "title": slide.title,
        "layout": slide.layout,
        "deck_title": slide.deck_title,
        "blocks": blocks,
    }


def _optional_text(value: object, what: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DeckStructureError(f"{what} must be a string.")
    cleaned = clean_text(value)
    return cleaned or None


def slide_from_json(data: object) -> DeckSlide:
    """Read a stored structure back. Raises :class:`DeckStructureError` if it is not one."""
    if not isinstance(data, Mapping):
        raise DeckStructureError("A slide structure must be an object.")
    if data.get("v") != STRUCTURE_VERSION:
        raise DeckStructureError(f"Unsupported slide structure version {data.get('v')!r}.")
    index = data.get("index")
    if not isinstance(index, int) or isinstance(index, bool) or index < 1:
        raise DeckStructureError("A slide's index must be a positive integer.")
    raw_blocks = data.get("blocks")
    if not isinstance(raw_blocks, list):
        raise DeckStructureError("A slide's blocks must be a list.")

    blocks: list[DeckBlock] = []
    for raw in raw_blocks:
        if not isinstance(raw, Mapping):
            raise DeckStructureError("A block must be an object.")
        kind = raw.get("kind")
        if kind not in BLOCK_KINDS:
            raise DeckStructureError(f"Unknown block kind {kind!r}.")
        if kind == "table":
            rows = raw.get("rows")
            if not isinstance(rows, list):
                raise DeckStructureError("A table's rows must be a list.")
            parsed = tuple(
                tuple(clean_text(str(cell)) for cell in row)
                for row in rows
                if isinstance(row, list)
            )
            blocks.append(DeckBlock(kind="table", rows=parsed))
            continue
        text = raw.get("text")
        if not isinstance(text, str):
            raise DeckStructureError("A block's text must be a string.")
        level = raw.get("level", 0)
        if not isinstance(level, int) or isinstance(level, bool) or level < 0:
            raise DeckStructureError("A bullet's level must be a non-negative integer.")
        blocks.append(DeckBlock(kind=kind, text=clean_text(text), level=level))

    return DeckSlide(
        index=index,
        title=_optional_text(data.get("title"), "A slide's title"),
        blocks=tuple(blocks),
        layout=_optional_text(data.get("layout"), "A slide's layout"),
        deck_title=_optional_text(data.get("deck_title"), "A deck's title"),
    )


def normalise_slide(slide: DeckSlide) -> DeckSlide:
    """The slide with every text cleaned, empty blocks dropped and sizes bounded - what the parser
    hands on, so nothing downstream has to defend against stray whitespace or an empty bullet."""
    blocks: list[DeckBlock] = []
    for block in slide.blocks[:MAX_BLOCKS_PER_SLIDE]:
        if block.kind == "table":
            rows = tuple(
                tuple(clean_text(cell)[:MAX_TEXT_CHARS] for cell in row[:MAX_TABLE_COLUMNS])
                for row in block.rows[:MAX_TABLE_ROWS]
            )
            if any(any(cell for cell in row) for row in rows):
                blocks.append(DeckBlock(kind="table", rows=rows))
            continue
        text = clean_text(block.text)[:MAX_TEXT_CHARS]
        if text:
            blocks.append(
                DeckBlock(
                    kind=block.kind, text=text, level=min(max(block.level, 0), MAX_BULLET_LEVEL)
                )
            )
    title = clean_text(slide.title)[:MAX_TEXT_CHARS] if slide.title else None
    return DeckSlide(
        index=slide.index,
        title=title or None,
        blocks=tuple(blocks),
        layout=clean_text(slide.layout) if slide.layout else None,
        deck_title=clean_text(slide.deck_title) if slide.deck_title else None,
    )


def has_content(slide: DeckSlide) -> bool:
    """Whether a slide has anything a lesson could say: any word at all in its title or blocks."""
    return any(character.isalpha() for character in slide_text(slide))
