"""``providers/llm.py`` - never a real call to Anthropic.

Same pattern as ``test_embeddings_provider.py``: ``AsyncAnthropic(api_key=...)``
makes no network call at construction, so these tests build a real client
and monkeypatch only ``messages.create`` - the one thing that would
otherwise hit the network.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from anthropic import AsyncAnthropic
from anthropic.types import Message, ToolUseBlock, Usage

import edutoon.providers.llm as llm_module
from edutoon.domain.generation_settings import SUPPORTED_LANGUAGES
from edutoon.providers.llm import (
    LLM,
    Claim,
    LLMResponseError,
    OutlineTopicBrief,
    Passage,
    ScriptBrief,
    SectionContinuity,
    SectionTopicBrief,
    get_llm_client,
)

# Stands in for an external provider (OpenAI / Anthropic): these tests prove how our code
# handles a response, never how a real provider behaves. See "Provider-mocked tests"
# in apps/api/README.md.
pytestmark = pytest.mark.provider_mocked

MODEL = "claude-sonnet-5"


def _tool_use_message(tool_input: dict[str, Any]) -> Message:
    return Message(
        id="msg_1",
        content=[
            ToolUseBlock(type="tool_use", id="toolu_1", name="submit_verdict", input=tool_input)
        ],
        model=MODEL,
        role="assistant",
        type="message",
        usage=Usage(input_tokens=10, output_tokens=10),
    )


def _llm_with_fake_response(monkeypatch: Any, tool_input: dict[str, Any]) -> LLM:
    client = AsyncAnthropic(api_key="sk-ant-test-not-a-real-key")
    monkeypatch.setattr(
        client.messages, "create", AsyncMock(return_value=_tool_use_message(tool_input))
    )
    return LLM(client, model=MODEL)


async def test_judge_claim_returns_a_verdict(monkeypatch):
    llm = _llm_with_fake_response(
        monkeypatch,
        {"status": "supported", "rationale": "Directly stated.", "cited_passage_ids": ["c1"]},
    )

    verdict = await llm.judge_claim(
        "The sky is blue.", [Passage(id="c1", content="The sky is blue during the day.")]
    )

    assert verdict.status == "supported"
    assert verdict.rationale == "Directly stated."
    assert verdict.cited_passage_ids == ["c1"]


async def test_judge_claim_filters_cited_ids_not_in_the_given_passages(monkeypatch):
    """The model citing a passage id it was never shown must not leak
    through - this is the provider-level half of the "never fabricate a
    citation" guarantee (services/evidence.py re-checks this again at the
    persistence boundary, but it must already be true here).
    """
    llm = _llm_with_fake_response(
        monkeypatch,
        {"status": "supported", "rationale": "...", "cited_passage_ids": ["c1", "made-up-id"]},
    )

    verdict = await llm.judge_claim("Claim", [Passage(id="c1", content="Text")])

    assert verdict.cited_passage_ids == ["c1"]


async def test_judge_claim_with_no_citations_is_a_valid_verdict(monkeypatch):
    llm = _llm_with_fake_response(
        monkeypatch,
        {"status": "unsupported", "rationale": "Not mentioned.", "cited_passage_ids": []},
    )

    verdict = await llm.judge_claim("Claim", [Passage(id="c1", content="Unrelated text.")])

    assert verdict.status == "unsupported"
    assert verdict.cited_passage_ids == []


@pytest.mark.parametrize(
    "broken_input",
    [
        {"status": "definitely true", "rationale": "...", "cited_passage_ids": []},
        {"rationale": "missing status", "cited_passage_ids": []},
        {"status": "supported", "rationale": 123, "cited_passage_ids": []},
        {"status": "supported", "rationale": "...", "cited_passage_ids": "not-a-list"},
        {"status": "supported", "rationale": "...", "cited_passage_ids": [1, 2]},
    ],
)
async def test_judge_claim_raises_on_malformed_verdict(monkeypatch, broken_input):
    llm = _llm_with_fake_response(monkeypatch, broken_input)

    with pytest.raises(LLMResponseError):
        await llm.judge_claim("Claim", [Passage(id="c1", content="Text")])


async def test_judge_claim_raises_when_no_tool_use_block_is_returned(monkeypatch):
    client = AsyncAnthropic(api_key="sk-ant-test-not-a-real-key")
    text_only_message = Message(
        id="msg_1",
        content=[],
        model=MODEL,
        role="assistant",
        type="message",
        usage=Usage(input_tokens=1, output_tokens=1),
    )
    monkeypatch.setattr(client.messages, "create", AsyncMock(return_value=text_only_message))
    llm = LLM(client, model=MODEL)

    with pytest.raises(LLMResponseError):
        await llm.judge_claim("Claim", [Passage(id="c1", content="Text")])


def test_llm_model_property_reflects_construction():
    llm = get_llm_client(api_key="sk-ant-test-not-a-real-key", model=MODEL)

    assert llm.model == MODEL


# --- compose_script_outline (Phase 7) ---------------------------------------------

BRIEF = ScriptBrief(topic="Photosynthesis", language="en-GB", audience="GCSE students")
TOPIC_A_ID = uuid4()
TOPIC_B_ID = uuid4()
OUTLINE_TOPICS = [
    OutlineTopicBrief(
        topic_id=TOPIC_A_ID, title="Light reactions", summary=None, keywords=(), duration_ms=60_000
    ),
    OutlineTopicBrief(
        topic_id=TOPIC_B_ID, title="Calvin cycle", summary=None, keywords=(), duration_ms=60_000
    ),
]


def _tool_message(
    name: str, tool_input: dict[str, Any], *, input_tokens: int = 42, output_tokens: int = 99
) -> Message:
    return Message(
        id="msg_1",
        content=[ToolUseBlock(type="tool_use", id="toolu_1", name=name, input=tool_input)],
        model=MODEL,
        role="assistant",
        type="message",
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _llm_for_tool(
    monkeypatch: Any, name: str, tool_input: dict[str, Any], **usage: int
) -> tuple[LLM, AsyncMock]:
    client = AsyncAnthropic(api_key="sk-ant-test-not-a-real-key")
    create = AsyncMock(return_value=_tool_message(name, tool_input, **usage))
    monkeypatch.setattr(client.messages, "create", create)
    return LLM(client, model=MODEL), create


def _llm_for_outline(monkeypatch: Any, tool_input: dict[str, Any], **usage: int):
    return _llm_for_tool(monkeypatch, "submit_outline", tool_input, **usage)


async def _outline(llm: LLM, *, topics: list[OutlineTopicBrief] | None = None):
    return await llm.compose_script_outline(BRIEF, OUTLINE_TOPICS if topics is None else topics)


async def test_compose_script_outline_returns_draft_and_token_usage(monkeypatch):
    llm, _ = _llm_for_outline(
        monkeypatch,
        {
            "title": "How photosynthesis works",
            "topics": [
                {
                    "topic_id": str(TOPIC_A_ID),
                    "purpose": "Explain light capture.",
                    "key_concepts": ["chlorophyll"],
                },
                {
                    "topic_id": str(TOPIC_B_ID),
                    "purpose": "Explain carbon fixation.",
                    "key_concepts": [],
                },
            ],
        },
        input_tokens=1234,
        output_tokens=567,
    )

    draft, usage = await _outline(llm)

    assert draft.title == "How photosynthesis works"
    assert [t.topic_id for t in draft.topics] == [TOPIC_A_ID, TOPIC_B_ID]
    assert draft.topics[0].purpose == "Explain light capture."
    assert draft.topics[0].key_concepts == ("chlorophyll",)
    assert (usage.input_tokens, usage.output_tokens) == (1234, 567)


async def test_compose_script_outline_drops_an_unknown_topic_id(monkeypatch):
    llm, _ = _llm_for_outline(
        monkeypatch,
        {
            "title": "T",
            "topics": [
                {"topic_id": str(TOPIC_A_ID), "purpose": "p", "key_concepts": []},
                {"topic_id": str(uuid4()), "purpose": "invented", "key_concepts": []},
            ],
        },
    )

    draft, _ = await _outline(llm)

    assert [t.topic_id for t in draft.topics] == [TOPIC_A_ID]


async def test_compose_script_outline_drops_a_malformed_topic_id(monkeypatch):
    llm, _ = _llm_for_outline(
        monkeypatch,
        {
            "title": "T",
            "topics": [{"topic_id": "not-a-uuid", "purpose": "p", "key_concepts": []}],
        },
    )

    draft, _ = await _outline(llm)

    assert draft.topics == ()


async def test_compose_script_outline_topic_the_model_never_mentioned_is_simply_absent(monkeypatch):
    llm, _ = _llm_for_outline(
        monkeypatch,
        {
            "title": "T",
            "topics": [{"topic_id": str(TOPIC_A_ID), "purpose": "p", "key_concepts": []}],
        },
    )

    draft, _ = await _outline(llm)

    assert [t.topic_id for t in draft.topics] == [TOPIC_A_ID]


@pytest.mark.parametrize(
    "broken_input",
    [
        {"topics": []},
        {"title": 123, "topics": []},
        {"title": "   ", "topics": []},
        {"title": "T", "topics": "not-a-list"},
        {"title": "T", "topics": ["not-an-object"]},
        {"title": "T", "topics": [{"topic_id": str(TOPIC_A_ID), "key_concepts": []}]},
        {"title": "T", "topics": [{"topic_id": str(TOPIC_A_ID), "purpose": 1, "key_concepts": []}]},
        {
            "title": "T",
            "topics": [{"topic_id": str(TOPIC_A_ID), "purpose": "p", "key_concepts": "nope"}],
        },
    ],
)
async def test_compose_script_outline_rejects_malformed_output(monkeypatch, broken_input):
    llm, _ = _llm_for_outline(monkeypatch, broken_input)

    with pytest.raises(LLMResponseError):
        await _outline(llm)


async def test_compose_script_outline_rejects_when_no_tool_use_block_is_returned(monkeypatch):
    client = AsyncAnthropic(api_key="sk-ant-test-not-a-real-key")
    empty_message = Message(
        id="msg_1",
        content=[],
        model=MODEL,
        role="assistant",
        type="message",
        usage=Usage(input_tokens=1, output_tokens=1),
    )
    monkeypatch.setattr(client.messages, "create", AsyncMock(return_value=empty_message))
    llm = LLM(client, model=MODEL)

    with pytest.raises(LLMResponseError):
        await _outline(llm)


async def test_compose_script_outline_rejects_output_cut_off_at_the_token_limit(monkeypatch):
    llm, create = _llm_for_outline(monkeypatch, {"title": "T", "topics": []})
    create.return_value = create.return_value.model_copy(update={"stop_reason": "max_tokens"})

    with pytest.raises(LLMResponseError, match="cut off"):
        await _outline(llm)


async def test_compose_script_outline_forces_the_submit_outline_tool(monkeypatch):
    llm, create = _llm_for_outline(monkeypatch, {"title": "T", "topics": []})

    await _outline(llm)

    kwargs = create.await_args.kwargs
    assert kwargs["model"] == MODEL
    assert kwargs["tool_choice"] == {"type": "tool", "name": "submit_outline"}
    assert [tool["name"] for tool in kwargs["tools"]] == ["submit_outline"]


async def test_compose_script_outline_rejects_no_topics_without_calling_the_api(monkeypatch):
    llm, create = _llm_for_outline(monkeypatch, {"title": "T", "topics": []})

    with pytest.raises(ValueError):
        await _outline(llm, topics=[])
    create.assert_not_called()


async def test_compose_script_outline_rejects_duplicate_topic_ids_without_calling_the_api(
    monkeypatch,
):
    llm, create = _llm_for_outline(monkeypatch, {"title": "T", "topics": []})

    with pytest.raises(ValueError):
        await _outline(
            llm,
            topics=[
                OutlineTopicBrief(
                    topic_id=TOPIC_A_ID, title="a", summary=None, keywords=(), duration_ms=1
                ),
                OutlineTopicBrief(
                    topic_id=TOPIC_A_ID, title="b", summary=None, keywords=(), duration_ms=1
                ),
            ],
        )
    create.assert_not_called()


async def test_compose_script_outline_puts_topic_budgets_and_brief_fields_in_the_prompt(
    monkeypatch,
):
    llm, create = _llm_for_outline(monkeypatch, {"title": "T", "topics": []})

    await _outline(llm)

    content = create.await_args.kwargs["messages"][0]["content"]
    assert "Photosynthesis" in content
    assert "GCSE students" in content
    assert str(TOPIC_A_ID) in content and str(TOPIC_B_ID) in content
    assert "60000" in content


# --- compose_script_section (Phase 7) -----------------------------------------------

CLAIMS = [
    Claim(id="cl1", text="Plants convert light energy into chemical energy."),
    Claim(id="cl2", text="Chlorophyll absorbs mostly red and blue light."),
]
TOPIC = SectionTopicBrief(
    topic_id=TOPIC_A_ID,
    title="Light reactions",
    summary=None,
    keywords=(),
    purpose="Explain light capture.",
)
CONTINUITY = SectionContinuity(lesson_title="How photosynthesis works")


def _llm_for_section(monkeypatch: Any, tool_input: dict[str, Any], **usage: int):
    return _llm_for_tool(monkeypatch, "submit_section", tool_input, **usage)


async def _section(
    llm: LLM,
    *,
    claims: list[Claim] | None = None,
    max_segments: int = 12,
    max_claims: int = 40,
    words_per_minute: int = 150,
    duration_ms: int = 60_000,
):
    return await llm.compose_script_section(
        BRIEF,
        TOPIC,
        CLAIMS if claims is None else claims,
        CONTINUITY,
        duration_ms=duration_ms,
        max_segments=max_segments,
        max_claims=max_claims,
        words_per_minute=words_per_minute,
    )


async def test_compose_script_section_returns_segments_and_token_usage(monkeypatch):
    llm, _ = _llm_for_section(
        monkeypatch,
        {
            "segments": [
                {"narration_text": "Plants make their own food.", "claim_ids": ["cl1"]},
                {
                    "narration_text": "The green pigment chlorophyll captures light.",
                    "claim_ids": ["cl2"],
                },
            ]
        },
        input_tokens=1234,
        output_tokens=567,
    )

    segments, usage = await _section(llm)

    assert [segment.segment_index for segment in segments] == [0, 1]
    assert [segment.claim_ids for segment in segments] == [["cl1"], ["cl2"]]
    assert all(segment.estimated_duration_ms > 0 for segment in segments)
    assert all(segment.topic_id is None for segment in segments)  # the caller stamps topic_id
    assert (usage.input_tokens, usage.output_tokens) == (1234, 567)


async def test_compose_script_section_assigns_segment_positions_from_order(monkeypatch):
    llm, _ = _llm_for_section(
        monkeypatch,
        {
            "segments": [
                {"narration_text": "One.", "claim_ids": []},
                {"narration_text": "Two.", "claim_ids": ["cl1"]},
                {"narration_text": "Three.", "claim_ids": ["cl2"]},
            ]
        },
    )

    segments, _ = await _section(llm)

    assert [segment.segment_index for segment in segments] == [0, 1, 2]


async def test_compose_script_section_derives_duration_from_word_count(monkeypatch):
    narration = " ".join(["word"] * 60)
    llm, _ = _llm_for_section(
        monkeypatch, {"segments": [{"narration_text": narration, "claim_ids": ["cl1"]}]}
    )

    segments, _ = await _section(llm, words_per_minute=120)

    # 60 words / 120 wpm = 0.5 min = 30_000 ms.
    assert segments[0].estimated_duration_ms == 30_000


async def test_compose_script_section_allows_a_connective_segment_with_no_claims(monkeypatch):
    llm, _ = _llm_for_section(
        monkeypatch,
        {
            "segments": [
                {"narration_text": "Let us explore how plants feed.", "claim_ids": []},
                {"narration_text": "They convert light to energy.", "claim_ids": ["cl1"]},
            ]
        },
    )

    segments, _ = await _section(llm)

    assert segments[0].claim_ids == []
    assert segments[0].connective is False


async def test_compose_script_section_parses_the_connective_flag(monkeypatch):
    llm, _ = _llm_for_section(
        monkeypatch,
        {
            "segments": [
                {"narration_text": "An aside.", "claim_ids": [], "connective": True},
                {"narration_text": "A fact.", "claim_ids": ["cl1"], "connective": False},
            ]
        },
    )

    segments, _ = await _section(llm)

    assert [s.connective for s in segments] == [True, False]


async def test_compose_script_section_rejects_a_non_boolean_connective_flag(monkeypatch):
    llm, _ = _llm_for_section(
        monkeypatch,
        {"segments": [{"narration_text": "x.", "claim_ids": ["cl1"], "connective": "yes"}]},
    )

    with pytest.raises(LLMResponseError):
        await _section(llm)


async def test_compose_script_section_deduplicates_claim_ids_within_a_segment(monkeypatch):
    llm, _ = _llm_for_section(
        monkeypatch,
        {
            "segments": [
                {"narration_text": "Both facts at once.", "claim_ids": ["cl1", "cl2", "cl1"]}
            ]
        },
    )

    segments, _ = await _section(llm)

    assert segments[0].claim_ids == ["cl1", "cl2"]


async def test_compose_script_section_drops_a_claim_id_that_was_not_supplied(monkeypatch):
    llm, _ = _llm_for_section(
        monkeypatch,
        {
            "segments": [
                {"narration_text": "An invented citation.", "claim_ids": ["cl1", "cl-nope"]}
            ]
        },
    )

    segments, _ = await _section(llm)

    assert segments[0].claim_ids == ["cl1"]  # the invented one is gone, never kept or guessed


async def test_compose_script_section_joins_a_reply_with_too_many_segments_down_to_the_limit(
    monkeypatch,
):
    """Regression: told to cover every claim, the writer replied with 26 one-sentence passages for a
    section whose limit is 12, and the whole six-minute generation failed. The passages are joined
    down to the limit, keeping every citation."""
    llm, _ = _llm_for_section(
        monkeypatch,
        {
            "segments": [
                {"narration_text": "One.", "claim_ids": ["cl1"]},
                {"narration_text": "Two.", "claim_ids": ["cl2"]},
            ]
        },
    )

    segments, _ = await _section(llm, max_segments=1)

    assert [s.narration_text for s in segments] == ["One. Two."]
    assert segments[0].claim_ids == ["cl1", "cl2"]
    assert segments[0].segment_index == 0


async def test_compose_script_section_still_rejects_a_reply_far_over_the_limit(monkeypatch):
    llm, _ = _llm_for_section(
        monkeypatch,
        {"segments": [{"narration_text": f"Passage {n}.", "claim_ids": ["cl1"]} for n in range(5)]},
    )

    with pytest.raises(LLMResponseError, match="limit is 1"):
        await _section(llm, max_segments=1)


async def test_compose_script_section_rejects_when_no_tool_use_block_is_returned(monkeypatch):
    client = AsyncAnthropic(api_key="sk-ant-test-not-a-real-key")
    empty_message = Message(
        id="msg_1",
        content=[],
        model=MODEL,
        role="assistant",
        type="message",
        usage=Usage(input_tokens=1, output_tokens=1),
    )
    monkeypatch.setattr(client.messages, "create", AsyncMock(return_value=empty_message))
    llm = LLM(client, model=MODEL)

    with pytest.raises(LLMResponseError):
        await _section(llm)


async def test_compose_script_section_rejects_output_cut_off_at_the_token_limit(monkeypatch):
    llm, create = _llm_for_section(
        monkeypatch, {"segments": [{"narration_text": "x", "claim_ids": ["cl1"]}]}
    )
    create.return_value = create.return_value.model_copy(update={"stop_reason": "max_tokens"})

    with pytest.raises(LLMResponseError, match="cut off"):
        await _section(llm)


@pytest.mark.parametrize(
    "broken_input",
    [
        {},
        {"segments": []},
        {"segments": "not-a-list"},
        {"segments": ["not-an-object"]},
        {"segments": [{"claim_ids": []}]},
        {"segments": [{"narration_text": "", "claim_ids": []}]},
        {"segments": [{"narration_text": "x", "claim_ids": "cl1"}]},
        {"segments": [{"narration_text": "x", "claim_ids": [1]}]},
    ],
)
async def test_compose_script_section_rejects_malformed_output(monkeypatch, broken_input):
    llm, _ = _llm_for_section(monkeypatch, broken_input)

    with pytest.raises(LLMResponseError):
        await _section(llm)


async def test_compose_script_section_forces_the_submit_section_tool(monkeypatch):
    llm, create = _llm_for_section(
        monkeypatch, {"segments": [{"narration_text": "x.", "claim_ids": ["cl1"]}]}
    )

    await _section(llm)

    kwargs = create.await_args.kwargs
    assert kwargs["model"] == MODEL
    assert kwargs["tool_choice"] == {"type": "tool", "name": "submit_section"}
    assert [tool["name"] for tool in kwargs["tools"]] == ["submit_section"]


async def test_compose_script_section_rejects_an_empty_claim_list_without_calling_the_api(
    monkeypatch,
):
    llm, create = _llm_for_section(
        monkeypatch, {"segments": [{"narration_text": "x", "claim_ids": []}]}
    )

    with pytest.raises(ValueError):
        await _section(llm, claims=[])
    create.assert_not_called()


async def test_compose_script_section_rejects_more_claims_than_the_configured_maximum(monkeypatch):
    llm, create = _llm_for_section(
        monkeypatch, {"segments": [{"narration_text": "x", "claim_ids": []}]}
    )

    with pytest.raises(ValueError):
        await _section(llm, max_claims=1)
    create.assert_not_called()


async def test_compose_script_section_rejects_duplicate_claim_ids_in_the_input(monkeypatch):
    llm, create = _llm_for_section(
        monkeypatch, {"segments": [{"narration_text": "x", "claim_ids": []}]}
    )

    with pytest.raises(ValueError):
        await _section(llm, claims=[Claim(id="dup", text="a"), Claim(id="dup", text="b")])
    create.assert_not_called()


async def test_compose_script_section_puts_topic_and_continuity_in_the_prompt(monkeypatch):
    llm, create = _llm_for_section(
        monkeypatch, {"segments": [{"narration_text": "x.", "claim_ids": ["cl1"]}]}
    )

    await llm.compose_script_section(
        BRIEF,
        TOPIC,
        CLAIMS,
        SectionContinuity(
            lesson_title="How photosynthesis works",
            previous_topic_title="Introduction",
            previous_section_tail="...and that sets the stage.",
            next_topic_title="Calvin cycle",
        ),
        duration_ms=60_000,
        max_segments=12,
        max_claims=40,
        words_per_minute=150,
    )

    content = create.await_args.kwargs["messages"][0]["content"]
    assert "Light reactions" in content
    assert "Explain light capture." in content
    assert "Introduction" in content
    assert "sets the stage" in content
    assert "Calvin cycle" in content
    # The model sees short handles, not the 36-character ids (which it can mistype).
    assert "[c1]" in content and "[c2]" in content
    assert "cl1" not in content


# --- the language a script is written in --------------------------------------------------


@pytest.mark.parametrize(
    ("language", "asked_for", "not_asked_for"),
    [
        ("en-IN", "Indian English", "British English"),
        ("en-GB", "British English", "Indian English"),
    ],
)
async def test_compose_script_section_asks_for_the_language_the_brief_names(
    monkeypatch, language, asked_for, not_asked_for
):
    llm, create = _llm_for_section(
        monkeypatch, {"segments": [{"narration_text": "x.", "claim_ids": ["cl1"]}]}
    )

    await llm.compose_script_section(
        ScriptBrief(topic="Photosynthesis", language=language),
        TOPIC,
        CLAIMS,
        CONTINUITY,
        duration_ms=60_000,
        max_segments=12,
        max_claims=40,
        words_per_minute=150,
    )

    request = create.await_args.kwargs
    content = request["messages"][0]["content"]
    assert f"Language: {language}" in content
    assert asked_for in content
    assert not_asked_for not in content
    # The system prompt is not tied to a language: it defers to the request's own lines.
    assert "British English" not in request["system"]
    assert "Indian English" not in request["system"]


def test_every_supported_language_has_a_writing_style():
    """Adding a language to the domain without saying how to write it must fail here,
    not fall back to some other language in production.
    """
    assert set(llm_module._LANGUAGE_STYLE) == SUPPORTED_LANGUAGES


async def test_compose_script_section_rejects_a_language_with_no_style_without_calling_the_api(
    monkeypatch,
):
    llm, create = _llm_for_section(
        monkeypatch, {"segments": [{"narration_text": "x.", "claim_ids": ["cl1"]}]}
    )

    with pytest.raises(ValueError, match="fr-FR"):
        await llm.compose_script_section(
            ScriptBrief(topic="Photosynthesis", language="fr-FR"),  # type: ignore[arg-type]
            TOPIC,
            CLAIMS,
            CONTINUITY,
            duration_ms=60_000,
            max_segments=12,
            max_claims=40,
            words_per_minute=150,
        )
    create.assert_not_called()
