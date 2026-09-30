"""What the LLM wrapper does with a response it must not trust, and how a request is made.

A malformed, truncated or refused provider response has to fail clearly and safely, never become
content that looks real. Every capability goes through one check (``providers.llm._tool_input``),
so the same table of bad responses is run against all of them. Never a real call: only the SDK's
``messages.create`` is replaced (the pattern of ``test_llm_provider.py``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import anthropic
import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

from edutoon.core.config import get_settings
from edutoon.domain.claim_extraction import ClaimCandidateLimits
from edutoon.domain.topic import SourcePassage, TopicLimits, TopicSeed
from edutoon.providers import llm as llm_module
from edutoon.providers.llm import (
    LLM,
    ClaimTopicBrief,
    LLMRefusalError,
    LLMResponseError,
    OutlineTopicBrief,
    Passage,
    ScriptBrief,
    SectionContinuity,
    SectionTopicBrief,
    StoryboardSegmentBrief,
    TokenUsage,
    TopicBrief,
    llm_from_settings,
)
from edutoon.providers.retry import RetryPolicy

# Stands in for an external provider: these tests prove how our code handles a response, never how
# a real provider behaves. See "Provider-mocked tests" in apps/api/README.md.
pytestmark = pytest.mark.provider_mocked

MODEL = "claude-sonnet-5"
TOPIC_ID = uuid4()


def _message(
    name: str,
    tool_input: Any,
    *,
    stop_reason: str | None = "tool_use",
    input_tokens: int = 42,
    output_tokens: int = 99,
    **usage_extra: int,
) -> Message:
    return Message(
        id="msg_1",
        content=[ToolUseBlock(type="tool_use", id="toolu_1", name=name, input=tool_input)],
        model=MODEL,
        role="assistant",
        type="message",
        stop_reason=stop_reason,  # type: ignore[arg-type]
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens, **usage_extra),
    )


def _text_only(stop_reason: str | None = "end_turn") -> Message:
    return Message(
        id="msg_2",
        content=[TextBlock(type="text", text="I would rather not.")],
        model=MODEL,
        role="assistant",
        type="message",
        stop_reason=stop_reason,  # type: ignore[arg-type]
        usage=Usage(input_tokens=42, output_tokens=7),
    )


def _llm(monkeypatch: Any, response: Any, **kwargs: Any) -> tuple[LLM, AsyncMock]:
    client = AsyncAnthropic(api_key="sk-ant-test-not-a-real-key")
    create = AsyncMock(return_value=response) if not isinstance(response, AsyncMock) else response
    monkeypatch.setattr(client.messages, "create", create)
    return LLM(client, model=MODEL, **kwargs), create


# --- one call per capability -------------------------------------------------------------------

SEGMENT_TEXT = "Plants make sugar from light."
_TOPIC_ROW = OutlineTopicBrief(
    topic_id=TOPIC_ID, title="T", summary=None, keywords=(), duration_ms=60_000
)


async def _judge(llm: LLM):
    return await llm.judge_claim("A claim.", [Passage(id="c1", content="A passage.")])


async def _outline(llm: LLM):
    return await llm.compose_script_outline(
        ScriptBrief(topic="Lesson", language="en-IN"), [_TOPIC_ROW]
    )


async def _section(llm: LLM):
    return await llm.compose_script_section(
        ScriptBrief(topic="Lesson", language="en-IN"),
        SectionTopicBrief(topic_id=TOPIC_ID, title="T", summary=None, keywords=()),
        [llm_module.Claim(id=str(uuid4()), text="A verified claim.")],
        SectionContinuity(lesson_title="Lesson"),
        duration_ms=60_000,
        max_segments=6,
        max_claims=15,
        words_per_minute=150,
    )


async def _topics(llm: LLM):
    return await llm.extract_topics(
        TopicBrief(subject="S", language="en-IN"),
        [SourcePassage(chunk_id=str(uuid4()), text="Some text.", page_from=1, page_to=1)],
        [TopicSeed(ref="s1", title="Seed", position=0)],
        limits=TopicLimits(
            max_topics=10,
            max_depth=3,
            max_title_chars=120,
            max_summary_chars=400,
            max_keywords_per_topic=8,
            max_keyword_chars=40,
            max_evidence_per_topic=6,
        ),
    )


async def _claims(llm: LLM):
    return await llm.extract_claims(
        ClaimTopicBrief(title="T", language="en-IN"),
        [],
        limits=ClaimCandidateLimits(max_claims_per_topic=6, max_claim_text_chars=300),
    )


async def _storyboard(llm: LLM):
    return await llm.compose_storyboard_scenes(
        StoryboardSegmentBrief(
            segment_index=0, narration_text=SEGMENT_TEXT, orientation="horizontal", lesson_title="L"
        ),
        max_scenes=3,
    )


CAPABILITIES: list[tuple[str, str, Callable[[LLM], Awaitable[Any]]]] = [
    ("claim_judge", "submit_verdict", _judge),
    ("script_outline", "submit_outline", _outline),
    ("script_section", "submit_section", _section),
    ("topic_extraction", "submit_topics", _topics),
    ("claim_extraction", "submit_claims", _claims),
    ("storyboard", "submit_scenes", _storyboard),
]
IDS = [kind for kind, _, _ in CAPABILITIES]


def _params():
    return pytest.mark.parametrize(("kind", "tool", "call"), CAPABILITIES, ids=IDS)


# --- a response that must never be used ---------------------------------------------------------


@_params()
async def test_a_refusal_fails_clearly_with_nothing_parsed(monkeypatch, kind, tool, call) -> None:
    # A refusal that still carries a plausible-looking tool call: the call must not be used.
    llm, _ = _llm(monkeypatch, _message(tool, {"anything": "at all"}, stop_reason="refusal"))
    with pytest.raises(LLMRefusalError) as caught:
        await call(llm)
    assert str(caught.value) == llm_module.REFUSED_MESSAGE
    assert caught.value.usage == TokenUsage(input_tokens=42, output_tokens=99)


@_params()
async def test_a_refusal_with_no_content_at_all_is_a_refusal_not_a_missing_tool(
    monkeypatch, kind, tool, call
) -> None:
    llm, _ = _llm(monkeypatch, _text_only("refusal"))
    with pytest.raises(LLMRefusalError):
        await call(llm)


@_params()
async def test_a_response_cut_off_at_the_token_limit_is_rejected_and_still_counted(
    monkeypatch, kind, tool, call
) -> None:
    llm, _ = _llm(monkeypatch, _message(tool, {}, stop_reason="max_tokens", output_tokens=1234))
    with pytest.raises(LLMResponseError, match="cut off at the output limit") as caught:
        await call(llm)
    assert not isinstance(caught.value, LLMRefusalError)
    assert caught.value.usage is not None and caught.value.usage.output_tokens == 1234


@pytest.mark.parametrize("stop", ["model_context_window_exceeded", "pause_turn", "stop_sequence"])
@_params()
async def test_any_other_incomplete_stop_reason_is_rejected(
    monkeypatch, kind, tool, call, stop
) -> None:
    llm, _ = _llm(monkeypatch, _message(tool, {}, stop_reason=stop))
    with pytest.raises(LLMResponseError) as caught:
        await call(llm)
    assert caught.value.usage is not None


@_params()
async def test_a_response_with_no_tool_call_is_rejected(monkeypatch, kind, tool, call) -> None:
    llm, _ = _llm(monkeypatch, _text_only())
    with pytest.raises(LLMResponseError, match="did not return") as caught:
        await call(llm)
    assert caught.value.usage == TokenUsage(input_tokens=42, output_tokens=7)


@_params()
async def test_a_call_to_a_different_tool_is_not_taken_for_the_answer(
    monkeypatch, kind, tool, call
) -> None:
    llm, _ = _llm(monkeypatch, _message("some_other_tool", {"segments": []}))
    with pytest.raises(LLMResponseError, match="did not return"):
        await call(llm)


@_params()
async def test_a_tool_input_that_is_not_an_object_is_rejected(
    monkeypatch, kind, tool, call
) -> None:
    # The SDK's own types refuse a non-object here, so the block is built without validation: this
    # is the guard for a response that arrived in a shape the SDK did not check.
    block = ToolUseBlock.model_construct(
        type="tool_use", id="toolu_1", name=tool, input="a string, not an object"
    )
    message = _message(tool, {})
    message.content = [block]
    llm, _ = _llm(monkeypatch, message)
    with pytest.raises(LLMResponseError, match="not in the expected format"):
        await call(llm)


@_params()
async def test_a_malformed_answer_carries_what_the_call_cost(monkeypatch, kind, tool, call) -> None:
    """A response that arrived, was paid for, and was then rejected while being parsed."""
    llm, _ = _llm(monkeypatch, _message(tool, {"nothing_useful": True}, input_tokens=7))
    with pytest.raises(LLMResponseError) as caught:
        await call(llm)
    assert caught.value.usage is not None and caught.value.usage.input_tokens == 7


async def test_end_turn_is_accepted_only_when_a_complete_tool_call_came_with_it(
    monkeypatch,
) -> None:
    ok = {"status": "supported", "rationale": "Stated.", "cited_passage_ids": ["c1"]}
    llm, _ = _llm(monkeypatch, _message("submit_verdict", ok, stop_reason="end_turn"))
    assert (await _judge(llm)).status == "supported"
    llm, _ = _llm(monkeypatch, _text_only("end_turn"))
    with pytest.raises(LLMResponseError):
        await _judge(llm)


async def test_a_verdict_cut_off_at_the_limit_is_no_longer_parsed_as_if_complete(
    monkeypatch,
) -> None:
    """Before this check the judge never looked at ``stop_reason``: a rationale cut off mid-way
    could still parse into a verdict."""
    ok = {"status": "supported", "rationale": "Stated.", "cited_passage_ids": ["c1"]}
    llm, _ = _llm(monkeypatch, _message("submit_verdict", ok, stop_reason="max_tokens"))
    with pytest.raises(LLMResponseError, match="verdict was cut off"):
        await _judge(llm)


# --- usage ---------------------------------------------------------------------------------


async def test_usage_reports_the_cache_figures_only_when_the_provider_gave_them(
    monkeypatch,
) -> None:
    ok = {"status": "unsupported", "rationale": "None.", "cited_passage_ids": []}
    llm, _ = _llm(monkeypatch, _message("submit_verdict", ok))
    verdict = await _judge(llm)
    assert verdict.usage == TokenUsage(42, 99)
    assert verdict.usage.cache_creation_input_tokens is None  # unavailable, not zero
    assert verdict.usage.cache_read_input_tokens is None

    llm, _ = _llm(
        monkeypatch,
        _message("submit_verdict", ok, cache_creation_input_tokens=5, cache_read_input_tokens=7),
    )
    verdict = await _judge(llm)
    assert verdict.usage == TokenUsage(42, 99, 5, 7)


async def test_every_capability_returns_its_usage(monkeypatch) -> None:
    llm, _ = _llm(
        monkeypatch,
        _message(
            "submit_scenes", {"scenes": [{"template": "title_card", "content": {"title": "T"}}]}
        ),
    )
    _, usage = await _storyboard(llm)
    assert usage == TokenUsage(42, 99)


# --- how the request is made ---------------------------------------------------------------------


@_params()
async def test_thinking_is_explicitly_disabled_and_the_tool_is_still_forced(
    monkeypatch, kind, tool, call
) -> None:
    llm, create = _llm(monkeypatch, _message(tool, {}))
    with pytest.raises(LLMResponseError):
        await call(llm)  # the empty answer is rejected; the request is what is under test
    request = create.await_args.kwargs
    assert request["thinking"] == {"type": "disabled"}
    assert request["tool_choice"] == {"type": "tool", "name": tool}
    assert request["model"] == MODEL
    assert request["max_tokens"] > 0


async def test_thinking_can_be_left_out_for_a_model_that_rejects_a_disabled_value(
    monkeypatch,
) -> None:
    llm, create = _llm(monkeypatch, _message("submit_verdict", {}), thinking="omit")
    with pytest.raises(LLMResponseError):
        await _judge(llm)
    assert "thinking" not in create.await_args.kwargs


async def test_the_judge_has_headroom_beyond_a_long_rationale() -> None:
    assert llm_module._JUDGE_MAX_OUTPUT_TOKENS >= 2048


def test_the_factory_takes_the_sdks_own_retries_out_and_sets_a_timeout() -> None:
    client = llm_module.get_llm_client(
        api_key="sk-ant-test-not-a-real-key", model=MODEL, timeout_seconds=42.0
    )
    assert client._client.max_retries == 0
    assert client._client.timeout == 42.0


def test_settings_build_the_client_the_api_and_the_worker_share() -> None:
    settings = get_settings()
    client = llm_from_settings(settings, model=MODEL)
    assert client.model == MODEL
    assert client._thinking == settings.LLM_THINKING == "disabled"
    assert client._retry.max_attempts == settings.PROVIDER_RETRY_ATTEMPTS
    assert client._client.timeout == settings.LLM_TIMEOUT_SECONDS


# --- transient failures --------------------------------------------------------------------


def _status_error(status: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.example.invalid/v1/messages")
    return anthropic.APIStatusError(
        "PRIVATE-USER-MATERIAL", response=httpx.Response(status, request=request), body=None
    )


def _retrying_llm(monkeypatch: Any, side_effect: list[Any], attempts: int = 3):
    create = AsyncMock(side_effect=side_effect)
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    llm, _ = _llm(
        monkeypatch,
        create,
        retry=RetryPolicy(max_attempts=attempts, base_delay_seconds=1.0, max_delay_seconds=4.0),
        sleep=_sleep,
    )
    return llm, create, slept


OK = {"status": "supported", "rationale": "Stated.", "cited_passage_ids": ["c1"]}


async def test_a_rate_limit_or_overload_is_retried_then_succeeds(monkeypatch) -> None:
    llm, create, slept = _retrying_llm(
        monkeypatch, [_status_error(429), _status_error(529), _message("submit_verdict", OK)]
    )
    assert (await _judge(llm)).status == "supported"
    assert create.await_count == 3 and len(slept) == 2


async def test_a_timeout_is_retried(monkeypatch) -> None:
    request = httpx.Request("POST", "https://api.example.invalid/v1/messages")
    llm, create, _ = _retrying_llm(
        monkeypatch, [anthropic.APITimeoutError(request=request), _message("submit_verdict", OK)]
    )
    assert (await _judge(llm)).status == "supported"
    assert create.await_count == 2


async def test_a_bad_request_or_a_rejected_key_is_not_retried(monkeypatch) -> None:
    for status in (400, 401, 404):
        llm, create, slept = _retrying_llm(monkeypatch, [_status_error(status)])
        with pytest.raises(anthropic.APIStatusError):
            await _judge(llm)
        assert create.await_count == 1 and slept == []


async def test_retries_stop_at_the_limit_and_the_last_error_is_raised(monkeypatch) -> None:
    llm, create, _ = _retrying_llm(monkeypatch, [_status_error(503)] * 5, attempts=3)
    with pytest.raises(anthropic.APIStatusError):
        await _judge(llm)
    assert create.await_count == 3


async def test_a_refusal_is_never_retried(monkeypatch) -> None:
    llm, create, slept = _retrying_llm(
        monkeypatch, [_message("submit_verdict", {}, stop_reason="refusal")]
    )
    with pytest.raises(LLMRefusalError):
        await _judge(llm)
    assert create.await_count == 1 and slept == []


async def test_a_client_built_directly_makes_a_single_attempt(monkeypatch) -> None:
    llm, create = _llm(monkeypatch, AsyncMock(side_effect=[_status_error(503)]))
    with pytest.raises(anthropic.APIStatusError):
        await _judge(llm)
    assert create.await_count == 1
