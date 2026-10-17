"""How a project's free-text instructions reach each stage of the pipeline.

The instruction text lives on the project (``projects.instructions``). This module turns it into
what each stage can use - a prompt block for the writers, a narrowed set of pages for the topic
finder, an audience and tone for the script, style hints for the storyboard - using only the fixed
interpretation in ``domain.instructions``. Nothing here weakens a grounding rule: the text is
guidance, and every stage still checks its output against the source exactly as before.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from edutoon.core.errors import ValidationError
from edutoon.domain import instructions as instructions_domain
from edutoon.domain.instructions import Interpretation, Note


class _HasInstructions(Protocol):
    @property
    def instructions(self) -> str | None: ...


class _Chunk(Protocol):
    @property
    def content(self) -> str: ...
    @property
    def page_from(self) -> int | None: ...
    @property
    def page_to(self) -> int | None: ...


def interpretation_for(project: _HasInstructions) -> Interpretation:
    """What the project's instructions were understood to ask for (empty if it has none)."""
    return instructions_domain.interpret(project.instructions)


def prompt_block(project: _HasInstructions) -> str | None:
    """The block of instruction text shown to a writer, or ``None``."""
    return instructions_domain.render_for_prompt(project.instructions, interpretation_for(project))


def audience_and_tone(project: _HasInstructions) -> tuple[str | None, str | None]:
    """The audience and tone lines for the script writers, from the instructions."""
    understood = interpretation_for(project)
    return instructions_domain.audience_line(understood), instructions_domain.tone_line(understood)


def storyboard_hints(project: _HasInstructions) -> tuple[str, ...]:
    """How the instructions shape scenes (empty when they do not)."""
    return instructions_domain.style_hints(interpretation_for(project))


@dataclass(frozen=True, slots=True)
class ScopedChunks[C: _Chunk]:
    """The chunks the topic finder may read, and what narrowing them did."""

    chunks: Sequence[C]
    notes: tuple[Note, ...]
    chapter_found: bool | None


def scope_chunks[C: _Chunk](
    project: _HasInstructions, chunks: Sequence[C], *, budget_chars: int
) -> ScopedChunks[C]:
    """Narrow ``chunks`` (in reading order) to what the instructions ask for.

    A page range keeps the chunks on those pages; a chapter reference keeps that chapter's chunks
    when its heading is found (otherwise nothing is narrowed and the owner is told); focus terms
    put the chunks that mention them first in line *only when the document is longer than the topic
    finder can read*. Pasted text has no pages, so a page range does not apply to it. Asking for
    pages the document has no text on is a :class:`ValidationError`, never an empty lesson.
    """
    understood = interpretation_for(project)
    if understood.is_empty:
        return ScopedChunks(chunks, (), None)

    kept: Sequence[C] = chunks
    chapter_found: bool | None = None

    page_range = understood.page_range
    if page_range is not None and any(chunk.page_from is not None for chunk in chunks):
        within = [
            chunk
            for chunk in chunks
            if chunk.page_from is not None
            and chunk.page_from <= page_range.last
            and (chunk.page_to or chunk.page_from) >= page_range.first
        ]
        if not within:
            raise ValidationError(
                f"The instructions ask for pages {page_range.first}-{page_range.last}, but no "
                "text was read from those pages of the document. Change the instructions, or "
                "check the page numbers."
            )
        kept = within

    if understood.chapter is not None:
        span = instructions_domain.chapter_page_span(
            [chunk.content for chunk in kept], understood.chapter
        )
        chapter_found = span is not None
        if span is not None:
            kept = kept[span[0] : span[1] + 1]

    if understood.focus_terms and sum(len(chunk.content) for chunk in kept) > budget_chars:
        matching = {
            index
            for index, chunk in enumerate(kept)
            if instructions_domain.mentions_any(chunk.content, understood.focus_terms)
        }
        if matching:
            # The pages that mention a focus term and the page on either side, so a heading and
            # the explanation that follows it stay together.
            wanted = {n for index in matching for n in (index - 1, index, index + 1)}
            narrowed = [chunk for index, chunk in enumerate(kept) if index in wanted]
            if sum(len(chunk.content) for chunk in narrowed) >= min(
                budget_chars // 4, sum(len(chunk.content) for chunk in kept)
            ):
                kept = narrowed

    notes = tuple(
        instructions_domain.notes(project.instructions, understood, chapter_found=chapter_found)
    )
    return ScopedChunks(kept, notes, chapter_found)
