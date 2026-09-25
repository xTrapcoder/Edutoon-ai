"""``providers/llm.py`` - never a real call to Anthropic.

Same pattern as ``test_embeddings_provider.py``: ``AsyncAnthropic(api_key=...)``
makes no network call at construction, so these tests build a real client
and monkeypatch only ``messages.create`` - the one thing that would
otherwise hit the network.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from anthropic import AsyncAnthropic
from anthropic.types import Message, ToolUseBlock, Usage

from edutoon.providers.llm import (
    LLM,
    Claim,
    LLMResponseError,
    Passage,
    ScriptBrief,
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


# --- compose_script (SG-2) -------------------------------------------------------

BRIEF = ScriptBrief(topic="Photosynthesis", audience="GCSE students")
CLAIMS = [
    Claim(id="cl1", text="Plants convert light energy into chemical energy."),
    Claim(id="cl2", text="Chlorophyll absorbs mostly red and blue light."),
]


def _script_message(
    tool_input: dict[str, Any], *, input_tokens: int = 42, output_tokens: int = 99
) -> Message:
    return Message(
        id="msg_1",
        content=[
            ToolUseBlock(
                type="tool_use", id="toolu_1", name="submit_script", input=tool_input
            )
        ],
        model=MODEL,
        role="assistant",
        type="message",
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _llm_for_script(
    monkeypatch: Any, tool_input: dict[str, Any], **usage: int
) -> tuple[LLM, AsyncMock]:
    client = AsyncAnthropic(api_key="sk-ant-test-not-a-real-key")
    create = AsyncMock(return_value=_script_message(tool_input, **usage))
    monkeypatch.setattr(client.messages, "create", create)
    return LLM(client, model=MODEL), create


async def _compose(
    llm: LLM,
    *,
    claims: list[Claim] | None = None,
    max_segments: int = 12,
    max_claims: int = 40,
    words_per_minute: int = 150,
):
    return await llm.compose_script(
        BRIEF,
        CLAIMS if claims is None else claims,
        max_segments=max_segments,
        max_claims=max_claims,
        words_per_minute=words_per_minute,
    )


async def test_compose_script_returns_draft_and_token_usage(monkeypatch):
    llm, _ = _llm_for_script(
        monkeypatch,
        {
            "title": "How photosynthesis works",
            "segments": [
                {"narration_text": "Plants make their own food.", "claim_ids": ["cl1"]},
                {
                    "narration_text": "The green pigment chlorophyll captures light.",
                    "claim_ids": ["cl2"],
                },
            ],
        },
        input_tokens=1234,
        output_tokens=567,
    )

    draft, usage = await _compose(llm)

    assert draft.title == "How photosynthesis works"
    assert [segment.segment_index for segment in draft.segments] == [0, 1]
    assert [segment.claim_ids for segment in draft.segments] == [["cl1"], ["cl2"]]
    assert all(segment.estimated_duration_ms > 0 for segment in draft.segments)
    assert draft.estimated_duration_ms == sum(
        segment.estimated_duration_ms for segment in draft.segments
    )
    assert (usage.input_tokens, usage.output_tokens) == (1234, 567)


async def test_compose_script_assigns_segment_positions_from_order(monkeypatch):
    llm, _ = _llm_for_script(
        monkeypatch,
        {
            "title": "T",
            "segments": [
                {"narration_text": "One.", "claim_ids": []},
                {"narration_text": "Two.", "claim_ids": ["cl1"]},
                {"narration_text": "Three.", "claim_ids": ["cl2"]},
            ],
        },
    )

    draft, _ = await _compose(llm)

    assert [segment.segment_index for segment in draft.segments] == [0, 1, 2]


async def test_compose_script_derives_duration_from_word_count(monkeypatch):
    narration = " ".join(["word"] * 60)
    llm, _ = _llm_for_script(
        monkeypatch,
        {"title": "T", "segments": [{"narration_text": narration, "claim_ids": ["cl1"]}]},
    )

    draft, _ = await _compose(llm, words_per_minute=120)

    # 60 words / 120 wpm = 0.5 min = 30_000 ms.
    assert draft.segments[0].estimated_duration_ms == 30_000
    assert draft.estimated_duration_ms == 30_000


async def test_compose_script_allows_a_connective_segment_with_no_claims(monkeypatch):
    llm, _ = _llm_for_script(
        monkeypatch,
        {
            "title": "T",
            "segments": [
                {"narration_text": "Let us explore how plants feed.", "claim_ids": []},
                {"narration_text": "They convert light to energy.", "claim_ids": ["cl1"]},
            ],
        },
    )

    draft, _ = await _compose(llm)

    assert draft.segments[0].claim_ids == []
    assert draft.segments[0].connective is False


async def test_compose_script_parses_the_connective_flag(monkeypatch):
    llm, _ = _llm_for_script(
        monkeypatch,
        {
            "title": "T",
            "segments": [
                {"narration_text": "An aside.", "claim_ids": [], "connective": True},
                {"narration_text": "A fact.", "claim_ids": ["cl1"], "connective": False},
            ],
        },
    )

    draft, _ = await _compose(llm)

    assert [s.connective for s in draft.segments] == [True, False]


async def test_compose_script_rejects_a_non_boolean_connective_flag(monkeypatch):
    llm, _ = _llm_for_script(
        monkeypatch,
        {
            "title": "T",
            "segments": [
                {"narration_text": "x.", "claim_ids": ["cl1"], "connective": "yes"}
            ],
        },
    )

    with pytest.raises(LLMResponseError):
        await _compose(llm)


async def test_compose_script_deduplicates_claim_ids_within_a_segment(monkeypatch):
    llm, _ = _llm_for_script(
        monkeypatch,
        {
            "title": "T",
            "segments": [
                {"narration_text": "Both facts at once.", "claim_ids": ["cl1", "cl2", "cl1"]}
            ],
        },
    )

    draft, _ = await _compose(llm)

    assert draft.segments[0].claim_ids == ["cl1", "cl2"]


async def test_compose_script_rejects_a_claim_id_that_was_not_supplied(monkeypatch):
    llm, _ = _llm_for_script(
        monkeypatch,
        {
            "title": "T",
            "segments": [
                {"narration_text": "An invented citation.", "claim_ids": ["cl1", "cl-nope"]}
            ],
        },
    )

    with pytest.raises(LLMResponseError):
        await _compose(llm)


async def test_compose_script_rejects_a_script_with_too_many_segments(monkeypatch):
    llm, _ = _llm_for_script(
        monkeypatch,
        {
            "title": "T",
            "segments": [
                {"narration_text": "One.", "claim_ids": ["cl1"]},
                {"narration_text": "Two.", "claim_ids": ["cl2"]},
            ],
        },
    )

    with pytest.raises(LLMResponseError):
        await _compose(llm, max_segments=1)


async def test_compose_script_rejects_when_no_tool_use_block_is_returned(monkeypatch):
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
        await _compose(llm)


@pytest.mark.parametrize(
    "broken_input",
    [
        {"segments": [{"narration_text": "x", "claim_ids": []}]},
        {"title": 123, "segments": [{"narration_text": "x", "claim_ids": []}]},
        {"title": "   ", "segments": [{"narration_text": "x", "claim_ids": []}]},
        {"title": "T"},
        {"title": "T", "segments": []},
        {"title": "T", "segments": "not-a-list"},
        {"title": "T", "segments": ["not-an-object"]},
        {"title": "T", "segments": [{"claim_ids": []}]},
        {"title": "T", "segments": [{"narration_text": "", "claim_ids": []}]},
        {"title": "T", "segments": [{"narration_text": "x", "claim_ids": "cl1"}]},
        {"title": "T", "segments": [{"narration_text": "x", "claim_ids": [1]}]},
    ],
)
async def test_compose_script_rejects_malformed_output(monkeypatch, broken_input):
    llm, _ = _llm_for_script(monkeypatch, broken_input)

    with pytest.raises(LLMResponseError):
        await _compose(llm)


async def test_compose_script_forces_the_submit_script_tool(monkeypatch):
    llm, create = _llm_for_script(
        monkeypatch,
        {"title": "T", "segments": [{"narration_text": "x.", "claim_ids": ["cl1"]}]},
    )

    await _compose(llm)

    kwargs = create.await_args.kwargs
    assert kwargs["model"] == MODEL
    assert kwargs["tool_choice"] == {"type": "tool", "name": "submit_script"}
    assert [tool["name"] for tool in kwargs["tools"]] == ["submit_script"]


async def test_compose_script_rejects_an_empty_claim_list_without_calling_the_api(monkeypatch):
    llm, create = _llm_for_script(
        monkeypatch, {"title": "T", "segments": [{"narration_text": "x", "claim_ids": []}]}
    )

    with pytest.raises(ValueError):
        await _compose(llm, claims=[])
    create.assert_not_called()


async def test_compose_script_rejects_more_claims_than_the_configured_maximum(monkeypatch):
    llm, create = _llm_for_script(
        monkeypatch, {"title": "T", "segments": [{"narration_text": "x", "claim_ids": []}]}
    )

    with pytest.raises(ValueError):
        await _compose(llm, max_claims=1)
    create.assert_not_called()


async def test_compose_script_rejects_duplicate_claim_ids_in_the_input(monkeypatch):
    llm, create = _llm_for_script(
        monkeypatch, {"title": "T", "segments": [{"narration_text": "x", "claim_ids": []}]}
    )

    with pytest.raises(ValueError):
        await _compose(
            llm,
            claims=[Claim(id="dup", text="a"), Claim(id="dup", text="b")],
        )
    create.assert_not_called()


async def test_compose_script_puts_optional_brief_fields_in_the_prompt(monkeypatch):
    llm, create = _llm_for_script(
        monkeypatch,
        {"title": "T", "segments": [{"narration_text": "x.", "claim_ids": ["cl1"]}]},
    )

    await llm.compose_script(
        ScriptBrief(
            topic="Photosynthesis",
            audience="GCSE students",
            tone="curious",
            target_duration_ms=90_000,
        ),
        CLAIMS,
        max_segments=12,
        max_claims=40,
        words_per_minute=150,
    )

    content = create.await_args.kwargs["messages"][0]["content"]
    assert "Photosynthesis" in content
    assert "GCSE students" in content
    assert "curious" in content
    assert "90000" in content
    assert "[cl1]" in content and "[cl2]" in content
