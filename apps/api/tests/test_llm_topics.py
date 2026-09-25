"""``providers/llm.py`` - ``LLM.extract_topics`` (Phase 5, Step 3).

Never a real call to Anthropic: same pattern as ``test_llm_provider.py`` - a
real ``LLM`` around a real ``AsyncAnthropic`` (which makes no network call at
construction) with only ``messages.create`` replaced.

The tests are about the contract, not the wording of the prompt: what is asked
of the model (one forced ``submit_topics`` call, structure only, bounded by the
domain limits), what is refused when it answers badly, and what can never get
through (evidence it was not shown, claims, an outline it was not asked for).
Where a guarantee is only complete once the domain rules have run - dropping an
uncited topic, preserving every user keyword - the draft is passed through
``validate_and_build`` so the two steps are proven to fit together.
"""

from __future__ import annotations

import dataclasses
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from anthropic import AsyncAnthropic
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

import edutoon.providers.llm as llm_module
from edutoon.domain.topic import SourcePassage, TopicDraft, TopicLimits, TopicSeed
from edutoon.domain.topic_tree import validate_and_build
from edutoon.providers.llm import LLM, LLMResponseError, TokenUsage, TopicBrief

# Stands in for an external provider (OpenAI / Anthropic): these tests prove how our code
# handles a response, never how a real provider behaves. See "Provider-mocked tests"
# in apps/api/README.md.
pytestmark = pytest.mark.provider_mocked

MODEL = "claude-sonnet-5"

LIMITS = TopicLimits(
    max_topics=6,
    max_depth=3,
    max_title_chars=60,
    max_summary_chars=120,
    max_keywords_per_topic=4,
    max_keyword_chars=20,
    max_evidence_per_topic=3,
)
BRIEF = TopicBrief(subject="How photosynthesis works")


# --- builders -------------------------------------------------------------------------


def _message(
    tool_input: dict[str, Any],
    *,
    stop_reason: str | None = None,
    input_tokens: int = 120,
    output_tokens: int = 340,
) -> Message:
    return Message(
        id="msg_1",
        content=[
            ToolUseBlock(type="tool_use", id="toolu_1", name="submit_topics", input=tool_input)
        ],
        model=MODEL,
        role="assistant",
        type="message",
        stop_reason=stop_reason,  # type: ignore[arg-type]
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _llm(monkeypatch: Any, message: Message | dict[str, Any]) -> tuple[LLM, AsyncMock]:
    client = AsyncAnthropic(api_key="sk-ant-test-not-a-real-key")
    response = message if isinstance(message, Message) else _message(message)
    create = AsyncMock(return_value=response)
    monkeypatch.setattr(client.messages, "create", create)
    return LLM(client, model=MODEL), create


def _passages(count: int = 3, **overrides: Any) -> list[SourcePassage]:
    return [
        SourcePassage(
            chunk_id=str(uuid4()),
            text=f"Passage {i} text about plants.",
            page_from=i + 1,
            page_to=i + 1,
            **overrides,
        )
        for i in range(count)
    ]


def _topic(
    ref: str,
    title: str,
    *,
    parent: str | None = None,
    summary: str = "",
    keywords: tuple[str, ...] = (),
    evidence: tuple[str, ...] = (),
    seed: str | None = None,
) -> dict[str, Any]:
    topic: dict[str, Any] = {
        "ref": ref,
        "title": title,
        "summary": summary,
        "keywords": list(keywords),
        "evidence_chunk_ids": list(evidence),
    }
    if parent is not None:
        topic["parent_ref"] = parent
    if seed is not None:
        topic["seed_ref"] = seed
    return topic


def _request(create: AsyncMock) -> dict[str, Any]:
    return dict(create.await_args.kwargs)


def _user_message(create: AsyncMock) -> str:
    return str(_request(create)["messages"][0]["content"])


async def _extract(
    llm: LLM,
    *,
    passages: list[SourcePassage] | None = None,
    seeds: list[TopicSeed] | None = None,
    brief: TopicBrief = BRIEF,
    limits: TopicLimits = LIMITS,
):
    return await llm.extract_topics(brief, passages or [], seeds or [], limits=limits)


def _validated(draft: TopicDraft, *, passages=(), seeds=()):
    return validate_and_build(
        draft,
        seeds=list(seeds),
        known_chunk_ids={UUID(p.chunk_id) for p in passages},
        limits=LIMITS,
    )


# --- the forced tool-call contract -----------------------------------------------------


async def test_it_makes_exactly_one_forced_submit_topics_call(monkeypatch):
    passages = _passages()
    llm, create = _llm(monkeypatch, {"topics": []})

    await _extract(llm, passages=passages)

    create.assert_awaited_once()
    request = _request(create)
    assert request["model"] == MODEL
    assert request["tool_choice"] == {"type": "tool", "name": "submit_topics"}
    assert [tool["name"] for tool in request["tools"]] == ["submit_topics"]
    assert request["system"] == llm_module._TOPIC_SYSTEM_PROMPT
    assert [m["role"] for m in request["messages"]] == ["user"]
    assert request["max_tokens"] == llm_module._topics_max_tokens(LIMITS)


async def test_the_same_inputs_always_produce_the_same_request(monkeypatch):
    passages = _passages()
    seeds = [TopicSeed(ref="s1", title="Chlorophyll")]
    llm, create = _llm(monkeypatch, {"topics": []})

    await _extract(llm, passages=passages, seeds=seeds)
    await _extract(llm, passages=passages, seeds=seeds)

    first, second = create.await_args_list
    assert first == second


async def test_the_result_is_a_draft_and_the_token_usage(monkeypatch):
    llm, _ = _llm(monkeypatch, _message({"topics": []}, input_tokens=42, output_tokens=99))

    draft, usage = await _extract(llm, passages=_passages())

    assert isinstance(draft, TopicDraft)
    assert usage == TokenUsage(input_tokens=42, output_tokens=99)


# --- the structure-only contract: no claims ---------------------------------------------


def _all_property_names(schema: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                names.update(value)
            names |= _all_property_names(value)
    elif isinstance(schema, list):
        for item in schema:
            names |= _all_property_names(item)
    return names


def test_the_tool_schema_describes_structure_only_and_carries_no_claims():
    tool = llm_module._topics_tool(LIMITS)

    item = tool["input_schema"]["properties"]["topics"]["items"]
    assert set(item["properties"]) == {
        "ref",
        "parent_ref",
        "title",
        "summary",
        "keywords",
        "evidence_chunk_ids",
        "seed_ref",
    }
    assert set(item["required"]) == {"ref", "title", "summary", "keywords", "evidence_chunk_ids"}
    names = {name.lower() for name in _all_property_names(tool["input_schema"])}
    for word in ("claim", "fact", "statement", "assertion"):
        assert not any(word in name for name in names), word
    # Where a topic sits and what kind it is are the domain's decision, never the model's.
    for decided_elsewhere in ("depth", "position", "origin", "status", "id"):
        assert decided_elsewhere not in names


async def test_fields_outside_the_contract_are_ignored_not_carried_through(monkeypatch):
    """A model that adds per-topic claims, statements or facts gets them dropped
    at the boundary: no candidate claims can leave this method.
    """
    topic = _topic("t1", "Light reactions", summary="Where light is captured.")
    topic["claims"] = ["Chlorophyll absorbs red light."]
    topic["factual_statements"] = ["Oxygen is released."]
    topic["depth"] = 7
    topic["origin"] = "source"
    llm, _ = _llm(monkeypatch, {"topics": [topic], "claims": ["x"]})

    draft, _ = await _extract(llm, passages=_passages())

    [node] = draft.nodes
    assert {f.name for f in dataclasses.fields(node)} == {
        "ref",
        "title",
        "parent_ref",
        "summary",
        "keywords",
        "evidence_chunk_ids",
        "seed_ref",
    }
    assert not hasattr(node, "claims")
    assert not hasattr(node, "depth")
    assert node.summary == "Where light is captured."


def test_the_tool_schema_takes_its_bounds_from_the_domain_limits():
    item = llm_module._topics_tool(LIMITS)["input_schema"]["properties"]["topics"]

    props = item["items"]["properties"]
    assert item["maxItems"] == LIMITS.max_topics
    assert props["title"]["maxLength"] == LIMITS.max_title_chars
    assert props["summary"]["maxLength"] == LIMITS.max_summary_chars
    assert props["keywords"]["maxItems"] == LIMITS.max_keywords_per_topic
    assert props["keywords"]["items"]["maxLength"] == LIMITS.max_keyword_chars
    assert props["evidence_chunk_ids"]["maxItems"] == LIMITS.max_evidence_per_topic

    looser = dataclasses.replace(LIMITS, max_topics=25, max_title_chars=90)
    other = llm_module._topics_tool(looser)["input_schema"]["properties"]["topics"]
    assert other["maxItems"] == 25
    assert other["items"]["properties"]["title"]["maxLength"] == 90


def test_the_output_token_allowance_follows_the_limits_and_stays_in_range():
    tiny = TopicLimits(1, 1, 10, 10, 1, 5, 1)
    huge = TopicLimits(500, 5, 500, 2000, 50, 100, 50)

    assert llm_module._topics_max_tokens(tiny) == llm_module._TOPICS_MIN_OUTPUT_TOKENS
    assert llm_module._topics_max_tokens(huge) == llm_module._TOPICS_MAX_OUTPUT_TOKENS
    assert (
        llm_module._TOPICS_MIN_OUTPUT_TOKENS
        <= llm_module._topics_max_tokens(LIMITS)
        <= llm_module._TOPICS_MAX_OUTPUT_TOKENS
    )
    more = dataclasses.replace(LIMITS, max_topics=LIMITS.max_topics * 3)
    assert llm_module._topics_max_tokens(more) >= llm_module._topics_max_tokens(LIMITS)


# --- valid structured output --------------------------------------------------------------


async def test_a_valid_outline_becomes_a_draft_in_the_order_the_model_gave_it(monkeypatch):
    passages = _passages(2)
    a, b = passages[0].chunk_id, passages[1].chunk_id
    llm, _ = _llm(
        monkeypatch,
        {
            "topics": [
                _topic("t1", "Photosynthesis", summary="How plants make food.", evidence=(a,)),
                _topic(
                    "t3",
                    "Calvin cycle",
                    parent="t1",
                    keywords=("carbon", "RuBisCO"),
                    evidence=(b,),
                ),
                _topic("t2", "Light reactions", parent="t1", evidence=(a, b)),
            ]
        },
    )

    draft, _ = await _extract(llm, passages=passages)

    assert [n.ref for n in draft.nodes] == ["t1", "t3", "t2"]  # given order, not sorted
    root, calvin, light = draft.nodes
    assert (root.title, root.summary, root.parent_ref) == (
        "Photosynthesis",
        "How plants make food.",
        None,
    )
    assert (calvin.parent_ref, calvin.keywords) == ("t1", ("carbon", "RuBisCO"))
    assert light.evidence_chunk_ids == (a, b)


async def test_an_empty_outline_is_valid_at_the_provider_boundary(monkeypatch):
    """With user topics the model may legitimately add nothing; whether an empty
    outline is acceptable is the domain's call (it is not, for material alone).
    """
    llm, _ = _llm(monkeypatch, {"topics": []})

    draft, _ = await _extract(llm, seeds=[TopicSeed(ref="s1", title="Chlorophyll")])

    assert draft.nodes == ()


# --- malformed output ------------------------------------------------------------------------

_GOOD = _topic("t1", "Topic", summary="Summary.", keywords=("k",), evidence=())


def _without(key: str) -> dict[str, Any]:
    return {k: v for k, v in _GOOD.items() if k != key}


@pytest.mark.parametrize(
    "tool_input",
    [
        pytest.param({}, id="no topics key"),
        pytest.param({"topics": "nope"}, id="topics not a list"),
        pytest.param({"topics": None}, id="topics null"),
        pytest.param({"topics": ["a string"]}, id="topic not an object"),
        pytest.param({"topics": [_without("ref")]}, id="missing ref"),
        pytest.param({"topics": [_without("title")]}, id="missing title"),
        pytest.param({"topics": [_without("summary")]}, id="missing summary"),
        pytest.param({"topics": [_without("keywords")]}, id="missing keywords"),
        pytest.param({"topics": [_without("evidence_chunk_ids")]}, id="missing evidence"),
        pytest.param({"topics": [{**_GOOD, "ref": 1}]}, id="ref not a string"),
        pytest.param({"topics": [{**_GOOD, "title": None}]}, id="title null"),
        pytest.param({"topics": [{**_GOOD, "summary": 5}]}, id="summary not a string"),
        pytest.param({"topics": [{**_GOOD, "keywords": "a, b"}]}, id="keywords not a list"),
        pytest.param({"topics": [{**_GOOD, "keywords": [1]}]}, id="keyword not a string"),
        pytest.param({"topics": [{**_GOOD, "evidence_chunk_ids": "id"}]}, id="evidence a string"),
        pytest.param({"topics": [{**_GOOD, "evidence_chunk_ids": [7]}]}, id="evidence id an int"),
        pytest.param({"topics": [{**_GOOD, "parent_ref": 3}]}, id="parent_ref not a string"),
        pytest.param({"topics": [{**_GOOD, "seed_ref": ["s1"]}]}, id="seed_ref not a string"),
        pytest.param(
            {"topics": [_topic(f"t{i}", f"T{i}") for i in range(LIMITS.max_topics + 1)]},
            id="more topics than the limit",
        ),
    ],
)
async def test_a_malformed_outline_raises_rather_than_returning_a_partial_draft(
    monkeypatch, tool_input
):
    llm, _ = _llm(monkeypatch, tool_input)

    with pytest.raises(LLMResponseError):
        await _extract(llm, passages=_passages())


async def test_a_topic_count_exactly_at_the_limit_is_accepted(monkeypatch):
    topics = [_topic(f"t{i}", f"T{i}") for i in range(LIMITS.max_topics)]
    llm, _ = _llm(monkeypatch, {"topics": topics})

    draft, _ = await _extract(llm, passages=_passages())

    assert len(draft.nodes) == LIMITS.max_topics


async def test_a_response_with_no_tool_call_raises(monkeypatch):
    text_only = Message(
        id="msg_1",
        content=[TextBlock(type="text", text="Here is an outline...")],
        model=MODEL,
        role="assistant",
        type="message",
        usage=Usage(input_tokens=1, output_tokens=1),
    )
    llm, _ = _llm(monkeypatch, text_only)

    with pytest.raises(LLMResponseError, match="tool call"):
        await _extract(llm, passages=_passages())


async def test_an_outline_cut_off_at_the_output_limit_is_never_accepted(monkeypatch):
    """A tool call truncated mid-JSON can still look like a valid, shorter
    outline; it must be refused, not silently used.
    """
    llm, _ = _llm(
        monkeypatch, _message({"topics": [_topic("t1", "Looks fine")]}, stop_reason="max_tokens")
    )

    with pytest.raises(LLMResponseError, match="cut off"):
        await _extract(llm, passages=_passages())


# --- evidence ids are narrowed to what was supplied ------------------------------------------


async def test_evidence_ids_the_model_was_not_shown_are_dropped_at_the_boundary(monkeypatch):
    passages = _passages(2)
    shown, other = passages[0].chunk_id, passages[1].chunk_id
    invented = str(uuid4())
    llm, _ = _llm(
        monkeypatch,
        {
            "topics": [
                _topic("t1", "Mixed", evidence=(invented, shown, "not-a-uuid", "")),
                _topic("t2", "Only invented", evidence=(invented, str(uuid4()))),
                _topic(
                    "t3", "Whitespace and repeats", evidence=(f"  {other} ", other, shown, other)
                ),
            ]
        },
    )

    draft, _ = await _extract(llm, passages=passages)

    mixed, only_invented, tidy = draft.nodes
    assert mixed.evidence_chunk_ids == (shown,)
    assert only_invented.evidence_chunk_ids == ()  # kept as a topic; the domain decides its fate
    assert tidy.evidence_chunk_ids == (other, shown)  # trimmed, de-duplicated, first-seen order


async def test_a_seed_link_to_a_user_topic_that_was_not_supplied_is_dropped(monkeypatch):
    seeds = [TopicSeed(ref="s1", title="Chlorophyll")]
    llm, _ = _llm(
        monkeypatch,
        {
            "topics": [
                _topic("t1", "Chlorophyll", seed="s1"),
                _topic("t2", "Invented link", seed="s99"),
            ]
        },
    )

    draft, _ = await _extract(llm, seeds=seeds)

    assert [n.seed_ref for n in draft.nodes] == ["s1", None]


# --- PDF only ------------------------------------------------------------------------------


async def test_pdf_only_sends_the_passages_with_their_ids_and_pages_and_no_user_topics(monkeypatch):
    passages = [
        SourcePassage(chunk_id=str(uuid4()), text="Alpha text.", page_from=1, page_to=1),
        SourcePassage(chunk_id=str(uuid4()), text="Beta text.", page_from=2, page_to=3),
        SourcePassage(chunk_id=str(uuid4()), text="Gamma text.", page_from=9, excerpt=True),
    ]
    llm, create = _llm(monkeypatch, {"topics": []})

    await _extract(
        llm,
        passages=passages,
        brief=TopicBrief(subject="Plants", description="A revision guide", language="en-GB"),
    )

    message = _user_message(create)
    assert "Mode: pdf\n" in message
    assert "Subject: Plants" in message and "Description: A revision guide" in message
    assert "Language: en-GB" in message
    for passage in passages:
        assert f"[{passage.chunk_id}]" in message and passage.text in message
    assert " page 1\n" in message and " pages 2-3\n" in message and " page 9 " in message
    assert message.count("excerpt") == 1  # only the cut passage is flagged
    positions = [message.index(p.text) for p in passages]
    assert positions == sorted(positions)  # passages keep the order they were given
    assert "User topics" not in message
    for number in (LIMITS.max_topics, LIMITS.max_title_chars, LIMITS.max_evidence_per_topic):
        assert str(number) in message


async def test_pdf_only_a_topic_with_no_valid_citation_is_dropped_by_the_domain(monkeypatch):
    passages = _passages(2)
    cited = passages[0].chunk_id
    llm, _ = _llm(
        monkeypatch,
        {
            "topics": [
                _topic("t1", "Supported", summary="Backed by page 1.", evidence=(cited,)),
                _topic("t2", "Made up", evidence=(str(uuid4()),)),  # cites nothing it was shown
                _topic("t3", "Supported child", parent="t2", evidence=(passages[1].chunk_id,)),
            ]
        },
    )

    draft, _ = await _extract(llm, passages=passages)
    tree = _validated(draft, passages=passages)

    assert [n.title for n in tree.nodes] == ["Supported", "Supported child"]
    assert tree.dropped_uncited == 1
    assert {n.origin for n in tree.nodes} == {"source"}
    assert tree.nodes[0].evidence_chunk_ids == (UUID(cited),)


# --- keywords only ---------------------------------------------------------------------------


SEEDS = [
    TopicSeed(ref="seed-1", title="Photosynthesis", position=0),
    TopicSeed(ref="seed-2", title="Respiration", position=1),
]


async def test_keywords_only_sends_the_user_topics_and_no_passages(monkeypatch):
    llm, create = _llm(monkeypatch, {"topics": []})

    await _extract(llm, seeds=SEEDS)

    message = _user_message(create)
    assert "Mode: keywords\n" in message
    assert "[seed-1] Photosynthesis" in message and "[seed-2] Respiration" in message
    assert message.index("Photosynthesis") < message.index("Respiration")
    assert "Passages:" not in message


async def test_keywords_only_preserves_every_seed_and_marks_additions_as_suggestions(monkeypatch):
    llm, _ = _llm(
        monkeypatch,
        {
            "topics": [
                # The model rewrites the user's wording and cites a passage it invented.
                _topic("t1", "Photosynthesis!", summary="Making food.", seed="seed-1",
                       evidence=(str(uuid4()),)),
                _topic("t2", "Light reactions", parent="t1", evidence=(str(uuid4()),)),
                # seed-2 is never mentioned at all.
            ]
        },
    )

    draft, _ = await _extract(llm, seeds=SEEDS)
    tree = _validated(draft, seeds=SEEDS)

    assert tree.mode == "keywords"
    assert [(n.title, n.origin) for n in tree.nodes] == [
        ("Photosynthesis", "user"),  # the user's own wording, not the model's
        ("Light reactions", "suggested"),
        ("Respiration", "user"),  # kept although the model omitted it
    ]
    assert all(n.evidence_chunk_ids == () for n in tree.nodes)  # nothing to cite, nothing cited


# --- PDF + keywords ----------------------------------------------------------------------------


async def test_pdf_plus_keywords_sends_both_the_user_topics_and_the_passages(monkeypatch):
    passages = _passages(2)
    llm, create = _llm(monkeypatch, {"topics": []})

    await _extract(llm, passages=passages, seeds=SEEDS)

    message = _user_message(create)
    assert "Mode: pdf_keywords\n" in message
    assert "[seed-1] Photosynthesis" in message and "Passages:" in message
    assert message.index("User topics") < message.index("Passages:")
    for passage in passages:
        assert f"[{passage.chunk_id}]" in message


async def test_pdf_plus_keywords_keeps_every_seed_and_leaves_an_uncovered_one_visible(monkeypatch):
    passages = _passages(2)
    a, b = passages[0].chunk_id, passages[1].chunk_id
    llm, _ = _llm(
        monkeypatch,
        {
            "topics": [
                _topic("t1", "photosynthesis (model wording)", summary="From p.1.",
                       seed="seed-1", evidence=(a,)),
                _topic("t2", "Light reactions", parent="t1", evidence=(b,)),
                _topic("t3", "Extra topic from the PDF", evidence=(b,)),
                _topic("t4", "Uncited extra"),  # cites nothing: to be dropped
                # seed-2 (Respiration) is not covered by the passages and is omitted.
            ]
        },
    )

    draft, _ = await _extract(llm, passages=passages, seeds=SEEDS)
    tree = _validated(draft, passages=passages, seeds=SEEDS)

    assert tree.mode == "pdf_keywords"
    by_title = {n.title: n for n in tree.nodes}
    assert list(by_title) == [
        "Photosynthesis",
        "Light reactions",
        "Respiration",
        "Extra topic from the PDF",
    ]
    covered, sub, uncovered, extra = by_title.values()
    assert (covered.origin, covered.evidence_chunk_ids) == ("user", (UUID(a),))
    assert (sub.origin, sub.parent_index, sub.depth) == ("source", 0, 1)  # nested under the seed
    assert (uncovered.origin, uncovered.evidence_chunk_ids) == ("user", ())  # visible, unbacked
    assert extra.origin == "source" and extra.parent_index is None
    assert tree.dropped_uncited == 1


# --- prompt construction (the contract, not the wording) ------------------------------------------


def test_the_prompt_is_one_module_level_constant_that_covers_all_three_modes():
    prompt = llm_module._TOPIC_SYSTEM_PROMPT

    assert isinstance(prompt, str)
    for marker in ("MODE pdf ", "MODE keywords ", "MODE pdf_keywords "):
        assert marker in prompt
    for contract_term in ("submit_topics", "evidence_chunk_ids", "seed_ref", "parent_ref"):
        assert contract_term in prompt


async def test_the_request_states_the_mode_that_follows_from_the_inputs(monkeypatch):
    llm, create = _llm(monkeypatch, {"topics": []})
    seen = []

    for passages, seeds in [(_passages(1), []), ([], SEEDS), (_passages(1), SEEDS)]:
        await _extract(llm, passages=passages, seeds=seeds)
        lines = _user_message(create).splitlines()
        seen.append(next(line for line in lines if line.startswith("Mode:")))

    assert seen == ["Mode: pdf", "Mode: keywords", "Mode: pdf_keywords"]


# --- caller errors: never an API call -----------------------------------------------------------


async def test_asking_for_topics_with_nothing_to_work_from_is_a_caller_error(monkeypatch):
    llm, create = _llm(monkeypatch, {"topics": []})

    with pytest.raises(ValueError, match="passages, seeds or both"):
        await _extract(llm)

    create.assert_not_awaited()


async def test_duplicate_passage_ids_or_seed_refs_are_caller_errors(monkeypatch):
    llm, create = _llm(monkeypatch, {"topics": []})
    duplicated = [SourcePassage(chunk_id="c1", text="A"), SourcePassage(chunk_id="c1", text="B")]

    with pytest.raises(ValueError, match="duplicate passage ids"):
        await _extract(llm, passages=duplicated)
    with pytest.raises(ValueError, match="duplicate seed refs"):
        await _extract(llm, seeds=[SEEDS[0], SEEDS[0]])

    create.assert_not_awaited()
