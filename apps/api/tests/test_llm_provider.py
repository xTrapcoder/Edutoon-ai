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

from edutoon.providers.llm import LLM, LLMResponseError, Passage, get_llm_client

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
