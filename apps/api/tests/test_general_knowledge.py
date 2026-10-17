"""``domain/general_knowledge.py`` and what it relies on: a keyword-only lesson is labelled, in
fixed words, and the label itself passes the same script validation as any narration."""

from __future__ import annotations

import re

import pytest

from edutoon.domain import general_knowledge as gk
from edutoon.domain.script import ScriptDraft, ScriptSegmentDraft
from edutoon.domain.script_provenance import GroundingReference, filter_and_validate


@pytest.mark.parametrize(
    ("source_type", "chunks", "expected"),
    [
        ("topic", 0, True),  # keywords alone: general knowledge
        ("topic", 3, False),  # keywords, and a PDF has since been indexed: verify against it
        ("pdf", 0, False),  # a PDF whose material is not indexed yet is never general knowledge
        ("pdf", 5, False),
        ("pptx", 0, False),
    ],
)
def test_only_a_keyword_project_with_no_material_is_general_knowledge(
    source_type: str, chunks: int, expected: bool
) -> None:
    assert (
        gk.is_general_knowledge_project(source_type=source_type, indexed_chunk_count=chunks)
        is expected
    )


def test_general_knowledge_is_never_called_supported() -> None:
    assert gk.STATUS != "supported"
    assert "not checked" in gk.RATIONALE


def test_the_notice_says_plainly_that_nothing_was_checked_and_needs_no_claim() -> None:
    assert "general knowledge" in gk.LESSON_NOTICE
    assert "not been checked against a source" in gk.LESSON_NOTICE
    assert not re.search(r"\d", gk.LESSON_NOTICE)  # no figure to ground


def test_the_notice_passes_the_same_validation_as_any_connective_segment() -> None:
    notice = ScriptSegmentDraft(
        segment_index=0,
        narration_text=gk.LESSON_NOTICE,
        claim_ids=[],
        estimated_duration_ms=1,
        connective=True,
    )
    validated = filter_and_validate(
        ScriptDraft(title="A lesson", segments=[notice], estimated_duration_ms=1),
        set(),
        150,
        grounding=GroundingReference(topic_texts={}, frame_text="A lesson"),
    )
    assert [segment.narration_text for segment in validated.segments] == [gk.LESSON_NOTICE]
