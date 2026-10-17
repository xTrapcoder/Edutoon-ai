"""General knowledge: what a project built from keywords alone may say, and how it says so.

A project with source material verifies every claim against it, and only a claim a cited passage
backs is ever ``supported``. A project built from keywords alone has nothing to verify against.
It is
still allowed to make a video - but only as a different, openly labelled kind of lesson: its claims
are the language model's general knowledge, recorded with their own status (:data:`STATUS`), never
called ``supported``, never given a citation, and the video says so aloud in its first sentence
(:data:`LESSON_NOTICE`).

Pure (rule 1): constants and one predicate, no I/O.
"""

from __future__ import annotations

from typing import Final

STATUS: Final = "general_knowledge"
"""``claim_verifications.status`` of a claim that is the model's general knowledge, unchecked."""

SOURCE_TYPE: Final = "topic"
"""``projects.source_type`` of a keyword-only project."""

EMBEDDING_MODEL_NONE: Final = "none"
"""What ``claim_verifications.embedding_model`` records for a claim that was never embedded."""

RATIONALE: Final = (
    "General knowledge from the language model. It was not checked against any source material, "
    "and nothing cites it."
)

LESSON_NOTICE: Final = (
    "This lesson is based on general knowledge, not on a particular document. "
    "The explanations in it have not been checked against a source."
)
"""The fixed first sentence of a keyword-only lesson. Written here, never by the model, and free of
figures and names so it needs no claim behind it."""


def is_general_knowledge_project(*, source_type: str, indexed_chunk_count: int) -> bool:
    """Whether a project's claims are general knowledge: it was built from keywords alone and has no
    source material. A keyword project that later gains a PDF verifies against it like any other."""
    return source_type == SOURCE_TYPE and indexed_chunk_count == 0
