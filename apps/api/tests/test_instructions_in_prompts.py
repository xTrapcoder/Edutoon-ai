"""The owner's instructions reach every writer's request - and only as fenced guidance."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from edutoon.core.errors import ValidationError
from edutoon.domain import instructions as instr
from edutoon.domain.claim_extraction import ClaimCandidateLimits
from edutoon.domain.topic import SourcePassage, TopicLimits
from edutoon.providers import llm
from edutoon.services import instructions as instructions_service

TEXT = "Make it suitable for a beginner. Focus on the Calvin cycle and use simple examples."


def _project(text: str | None = TEXT) -> SimpleNamespace:
    return SimpleNamespace(instructions=text)


def test_the_prompt_block_fences_the_text_and_states_what_it_cannot_do() -> None:
    block = instructions_service.prompt_block(_project())
    assert block is not None
    assert f'"""{TEXT}"""' in block
    assert "never permit a fact that is not in the material" in block
    assert "Understood audience: complete beginners" in block
    assert "wants emphasis on: Calvin cycle" in block
    assert instructions_service.prompt_block(_project(None)) is None


def test_the_script_request_carries_audience_tone_and_the_block() -> None:
    audience, tone = instructions_service.audience_and_tone(_project())
    brief = llm.ScriptBrief(
        topic="Photosynthesis",
        language="en-IN",
        audience=audience,
        tone=tone,
        instructions=instructions_service.prompt_block(_project()),
    )
    lines = llm._format_script_brief(brief)
    joined = "\n".join(lines)
    assert "Audience: complete beginners" in joined
    assert f'"""{TEXT}"""' in joined
    # ...and the writers are told how to treat it.
    assert "Owner's instructions" in llm._SECTION_SYSTEM_PROMPT
    assert "never let them add a fact" in llm._SECTION_SYSTEM_PROMPT
    assert "Owner's instructions" in llm._OUTLINE_SYSTEM_PROMPT


def test_the_topic_request_carries_the_block_after_the_limits() -> None:
    limits = TopicLimits(
        max_topics=8,
        max_depth=2,
        max_title_chars=80,
        max_summary_chars=200,
        max_keywords_per_topic=5,
        max_keyword_chars=40,
        max_evidence_per_topic=3,
    )
    brief = llm.TopicBrief(
        subject="Photosynthesis",
        language="en-IN",
        instructions=instructions_service.prompt_block(_project()),
    )
    passage = SourcePassage(chunk_id=str(uuid4()), text="Plants make food.", page_from=1, page_to=1)
    request = llm._format_topic_request(brief, "pdf", [passage], [], limits)
    assert f'"""{TEXT}"""' in request
    assert request.index("Limits:") < request.index("Owner's instructions")
    assert "Owner's instructions" in llm._TOPIC_SYSTEM_PROMPT


def test_the_claim_request_carries_the_block() -> None:
    brief = llm.ClaimTopicBrief(
        title="Calvin cycle",
        language="en-IN",
        instructions=instructions_service.prompt_block(_project("Include their definitions.")),
    )
    request = llm._format_claim_topic(
        brief, [], ClaimCandidateLimits(max_claims_per_topic=5, max_claim_text_chars=300)
    )
    assert "Include their definitions." in request


def test_the_storyboard_request_carries_style_hints() -> None:
    hints = instructions_service.storyboard_hints(_project("Beginner level, include definitions"))
    brief = llm.StoryboardSegmentBrief(
        segment_index=0,
        narration_text="Rubisco fixes carbon.",
        orientation="horizontal",
        style_hints=hints,
    )
    request = llm._format_storyboard_request(brief, max_scenes=3)
    assert request.count("Style: ") == len(hints) >= 2
    assert "Style:" in llm._STORYBOARD_SYSTEM_PROMPT
    plain = llm.StoryboardSegmentBrief(
        segment_index=0, narration_text="Rubisco fixes carbon.", orientation="horizontal"
    )
    assert "Style:" not in llm._format_storyboard_request(plain, max_scenes=3)


class _Chunk:
    def __init__(self, text: str, page: int | None) -> None:
        self.content = text
        self.page_from = page
        self.page_to = page


def _chunks() -> list[_Chunk]:
    return [
        _Chunk("Contents\nChapter 1 .... 1\nChapter 2 .... 4", 1),
        _Chunk("Chapter 1\nRespiration.", 2),
        _Chunk("Chapter 2\nPhotosynthesis begins with light.", 3),
        _Chunk("The Calvin cycle fixes carbon dioxide.", 4),
        _Chunk("Chapter 3\nFermentation.", 5),
    ]


def test_a_page_range_narrows_the_pages_read() -> None:
    scoped = instructions_service.scope_chunks(
        _project("Use pages 3-4 only"), _chunks(), budget_chars=10_000
    )
    assert [chunk.page_from for chunk in scoped.chunks] == [3, 4]
    assert any(note.kind == "applied" and "pages 3-4" in note.text for note in scoped.notes)


def test_pages_with_no_text_are_a_clear_refusal_not_an_empty_lesson() -> None:
    with pytest.raises(ValidationError, match="pages 40-50"):
        instructions_service.scope_chunks(
            _project("Focus on pages 40-50"), _chunks(), budget_chars=10_000
        )


def test_a_chapter_found_narrows_the_pages_and_a_chapter_not_found_is_reported() -> None:
    found = instructions_service.scope_chunks(
        _project("Focus on Chapter 2"), _chunks(), budget_chars=10_000
    )
    assert [chunk.page_from for chunk in found.chunks] == [3, 4]
    assert found.chapter_found is True

    missing = instructions_service.scope_chunks(
        _project("Focus on Chapter 9"), _chunks(), budget_chars=10_000
    )
    assert len(missing.chunks) == len(_chunks())
    assert missing.chapter_found is False
    assert any("was not found as a heading" in note.text for note in missing.notes)


def test_pasted_text_has_no_pages_so_a_page_range_leaves_it_whole() -> None:
    chunks = [_Chunk("A paragraph of pasted notes.", None), _Chunk("Another paragraph.", None)]
    scoped = instructions_service.scope_chunks(
        _project("Use pages 3-4 only"), chunks, budget_chars=10_000
    )
    assert len(scoped.chunks) == 2


def test_focus_terms_only_reorder_reading_when_the_document_is_too_long_to_read_whole() -> None:
    chunks = [
        _Chunk(f"Filler paragraph number {n} about something else entirely.", n + 1)
        for n in range(30)
    ]
    chunks[17] = _Chunk("The Calvin cycle fixes carbon dioxide into sugar in the stroma.", 18)
    project = _project("Focus on the Calvin cycle")

    roomy = instructions_service.scope_chunks(project, chunks, budget_chars=1_000_000)
    assert len(roomy.chunks) == 30  # it all fits: nothing is dropped

    tight = instructions_service.scope_chunks(project, chunks, budget_chars=300)
    pages = [chunk.page_from for chunk in tight.chunks]
    assert 18 in pages and len(pages) < 30


def test_a_project_without_instructions_is_left_exactly_as_it_was() -> None:
    chunks = _chunks()
    scoped = instructions_service.scope_chunks(_project(None), chunks, budget_chars=1)
    assert list(scoped.chunks) == chunks
    assert scoped.notes == ()
    assert instr.interpret(None).is_empty
