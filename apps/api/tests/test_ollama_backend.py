"""The local AI backend (``AI_BACKEND=local``, ``providers/ollama.py``).

No Ollama server is started: each test answers the HTTP call itself (``httpx.MockTransport``) and
checks two things - what our code *sends* (the schema as ``format``, the context window, no
cloud-only field) and how it *treats what comes back* (the unchanged ``LLM`` / ``Embeddings``
checks still apply). That proves our handling, not how a real local model behaves; running the
real thing is ``scripts/check_local_ai.py``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from anthropic import AsyncAnthropic

from edutoon.core.config import Settings
from edutoon.providers.embeddings import Embeddings, embeddings_from_settings
from edutoon.providers.errors import (
    LOCAL_MESSAGES,
    LocalModelError,
    describe_provider_error,
    is_transient_provider_error,
)
from edutoon.providers.llm import (
    CONTEXT_WINDOW_MESSAGE,
    LLM,
    LLMResponseError,
    Passage,
    llm_from_settings,
)
from edutoon.providers.ollama import (
    LocalEmbeddingsClient,
    LocalMessagesClient,
    pad_to_width,
)
from edutoon.providers.retry import RetryPolicy

pytestmark = pytest.mark.provider_mocked

Handler = Callable[[httpx.Request], httpx.Response]

VERDICT = {
    "status": "supported",
    "rationale": "It says so.",
    "cited_passage_ids": ["p1", "invented"],
}
PASSAGES = [Passage(id="p1", content="Water boils at 100 degrees Celsius at sea level.")]


def _chat_reply(content: object, **extra: Any) -> httpx.Response:
    body = {
        "message": {"role": "assistant", "content": json.dumps(content)},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 120,
        "eval_count": 40,
        **extra,
    }
    return httpx.Response(200, json=body)


def _llm(
    handler: Handler, *, num_ctx: int = 8192, disable_thinking: bool = False, **kw: Any
) -> LLM:
    client = LocalMessagesClient(
        base_url="http://ollama.test",
        timeout_seconds=30,
        num_ctx=num_ctx,
        disable_thinking=disable_thinking,
        transport=httpx.MockTransport(handler),
    )
    return LLM(client, model="qwen-test", **kw)


async def _no_sleep(_: float) -> None:
    return None


# --- language model: what is sent ----------------------------------------------------------


async def test_a_judgement_is_asked_for_as_schema_constrained_json_and_read_back() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        seen.append(json.loads(request.content))
        return _chat_reply(VERDICT)

    verdict = await _llm(handler).judge_claim("Water boils at 100 C.", PASSAGES)

    assert verdict.status == "supported"
    assert verdict.cited_passage_ids == ["p1"]  # the invented id is still filtered out
    assert verdict.usage is not None
    assert (verdict.usage.input_tokens, verdict.usage.output_tokens) == (120, 40)

    [sent] = seen
    assert sent["model"] == "qwen-test"
    assert sent["stream"] is False
    assert sent["format"]["required"] == ["status", "rationale", "cited_passage_ids"]
    assert sent["options"] == {"num_ctx": 8192, "num_predict": 2048, "temperature": 0}
    assert "think" not in sent
    assert "thinking" not in sent  # the cloud-only field is dropped, not forwarded
    system, user = sent["messages"]
    assert system["role"] == "system" and "submit_verdict" in system["content"]
    assert "JSON Schema" in system["content"]
    assert user["role"] == "user" and "Water boils" in user["content"]


async def test_thinking_is_switched_off_only_when_asked() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _chat_reply(VERDICT)

    await _llm(handler, disable_thinking=True).judge_claim("c", PASSAGES)
    assert seen[0]["think"] is False


# --- language model: what is refused --------------------------------------------------------


async def test_a_reply_cut_off_at_the_output_limit_is_not_used() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _chat_reply({"status": "supp"}, done_reason="length", eval_count=2048)

    with pytest.raises(LLMResponseError, match="cut off"):
        await _llm(handler).judge_claim("c", PASSAGES)


async def test_a_runaway_reply_is_retried_once_sampled_with_a_repeat_penalty() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            return _chat_reply({"status": "supp"}, done_reason="length", eval_count=2048)
        return _chat_reply(VERDICT)

    verdict = await _llm(handler).judge_claim("c", PASSAGES)
    assert verdict is not None
    assert len(seen) == 2
    assert seen[0]["options"]["temperature"] == 0 and "repeat_penalty" not in seen[0]["options"]
    assert seen[1]["options"]["temperature"] > 0
    assert seen[1]["options"]["repeat_penalty"] > 1


async def test_a_reply_cut_off_twice_is_still_refused_after_exactly_one_retry() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _chat_reply({"status": "supp"}, done_reason="length", eval_count=2048)

    with pytest.raises(LLMResponseError, match="cut off"):
        await _llm(handler).judge_claim("c", PASSAGES)
    assert calls == 2


async def test_a_prompt_that_fills_the_context_window_is_refused_not_trusted() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        # A well-formed answer - but to a prompt Ollama had to cut the front off.
        return _chat_reply(VERDICT, prompt_eval_count=4090)

    with pytest.raises(LLMResponseError) as raised:
        await _llm(handler, num_ctx=4096).judge_claim("c", PASSAGES)
    assert str(raised.value) == CONTEXT_WINDOW_MESSAGE
    assert raised.value.usage is not None and raised.value.usage.input_tokens == 4090


async def test_a_generation_that_runs_out_of_context_is_refused_not_called_truncation() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _chat_reply(
            {"status": "su"}, done_reason="length", prompt_eval_count=3000, eval_count=1100
        )

    with pytest.raises(LLMResponseError) as raised:
        await _llm(handler, num_ctx=4096).judge_claim("c", PASSAGES)
    assert str(raised.value) == CONTEXT_WINDOW_MESSAGE


async def test_an_answer_that_is_not_json_is_a_missing_tool_call() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"message": {"content": "Sure, it is supported."}, "done_reason": "stop"},
        )

    with pytest.raises(LLMResponseError, match="did not return a verdict"):
        await _llm(handler).judge_claim("c", PASSAGES)


async def test_an_answer_of_the_wrong_shape_is_still_rejected_by_the_existing_checks() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _chat_reply({"status": "probably", "rationale": "x", "cited_passage_ids": []})

    with pytest.raises(LLMResponseError, match="Unexpected verdict status"):
        await _llm(handler).judge_claim("c", PASSAGES)


# --- language model: failures ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "kind", "transient"),
    [
        (404, "model_missing", False),
        (400, "rejected", False),
        (500, "unavailable", True),
        (503, "unavailable", True),
        (504, "timeout", True),
    ],
)
async def test_an_http_failure_becomes_a_kind_with_a_fixed_message(
    status: int, kind: str, transient: bool
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="model 'x' not found: the user's own passage text")

    with pytest.raises(LocalModelError) as raised:
        await _llm(handler).judge_claim("c", PASSAGES)

    assert raised.value.kind == kind
    assert is_transient_provider_error(raised.value) is transient
    message = describe_provider_error(raised.value)
    assert message == LOCAL_MESSAGES[raised.value.kind]
    assert "passage text" not in str(raised.value) and "passage text" not in (message or "")


async def test_a_server_that_is_not_running_is_unreachable_and_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("refused", request=request)

    llm = _llm(handler, retry=RetryPolicy(max_attempts=3), sleep=_no_sleep)
    with pytest.raises(LocalModelError) as raised:
        await llm.judge_claim("c", PASSAGES)

    assert raised.value.kind == "unreachable"
    assert calls == 3  # transient, so retried by the one existing policy
    assert "Ollama" in (describe_provider_error(raised.value) or "")


async def test_a_transient_failure_is_retried_and_then_succeeds() -> None:
    replies = [httpx.Response(503), _chat_reply(VERDICT)]

    def handler(_: httpx.Request) -> httpx.Response:
        return replies.pop(0)

    verdict = await _llm(handler, retry=RetryPolicy(max_attempts=3), sleep=_no_sleep).judge_claim(
        "c", PASSAGES
    )
    assert verdict.status == "supported" and replies == []


async def test_a_missing_model_is_not_retried() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    with pytest.raises(LocalModelError):
        await _llm(handler, retry=RetryPolicy(max_attempts=3), sleep=_no_sleep).judge_claim(
            "c", PASSAGES
        )
    assert calls == 1


# --- embeddings -----------------------------------------------------------------------------


def _embeddings(handler: Handler) -> Embeddings:
    client = LocalEmbeddingsClient(
        base_url="http://ollama.test",
        timeout_seconds=30,
        num_ctx=8192,
        transport=httpx.MockTransport(handler),
    )
    return Embeddings(client, model="embed-test")


async def test_embeddings_are_requested_untruncated_and_padded_to_the_column_width() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/embed"
        payload = json.loads(request.content)
        seen.append(payload)
        vectors = [[float(i + 1), 0.5, -0.25] for i, _ in enumerate(payload["input"])]
        return httpx.Response(200, json={"embeddings": vectors})

    vectors = await _embeddings(handler).embed(["alpha", "beta"])

    assert [len(v) for v in vectors] == [1536, 1536]
    assert vectors[0][:3] == [1.0, 0.5, -0.25] and vectors[1][:3] == [2.0, 0.5, -0.25]
    assert set(vectors[0][3:]) == {0.0}
    assert seen[0]["model"] == "embed-test"
    assert seen[0]["truncate"] is False
    assert seen[0]["options"] == {"num_ctx": 8192}


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def test_zero_padding_leaves_cosine_similarity_exactly_where_it_was() -> None:
    a, b = [0.3, -0.8, 0.5, 0.1], [0.9, 0.2, -0.4, 0.7]
    assert _cosine(pad_to_width(a), pad_to_width(b)) == pytest.approx(_cosine(a, b), abs=1e-12)


async def test_a_vector_wider_than_the_column_is_an_error_not_a_truncation() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [[0.1] * 1537]})

    with pytest.raises(LocalModelError) as raised:
        await _embeddings(handler).embed(["x"])
    assert raised.value.kind == "dimension"
    assert is_transient_provider_error(raised.value) is False


async def test_a_malformed_embeddings_reply_is_an_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [["not", "numbers"]]})

    with pytest.raises(LocalModelError) as raised:
        await _embeddings(handler).embed(["x"])
    assert raised.value.kind == "unexpected"


async def test_an_input_the_model_cannot_hold_is_a_rejection_not_a_silent_cut() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "input length exceeds the context length"})

    with pytest.raises(LocalModelError) as raised:
        await _embeddings(handler).embed(["x" * 100])
    assert raised.value.kind == "rejected"


# --- choosing the backend -------------------------------------------------------------------


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_the_default_backend_is_still_the_cloud_one() -> None:
    settings = _settings()
    assert settings.AI_BACKEND == "cloud"
    assert isinstance(llm_from_settings(settings, model="m")._client, AsyncAnthropic)


def test_the_local_backend_builds_local_clients_and_needs_no_real_key() -> None:
    settings = _settings(
        AI_BACKEND="local",
        OPENAI_API_KEY="placeholder",
        ANTHROPIC_API_KEY="placeholder",
        LLM_MODEL="qwen-test",
        EMBEDDING_MODEL="embed-test",
        EMBEDDING_MAX_INPUT_TOKENS=4096,
    )
    llm = llm_from_settings(settings, model=settings.LLM_MODEL)
    embeddings = embeddings_from_settings(settings)

    assert isinstance(llm._client, LocalMessagesClient)
    assert llm.model == "qwen-test"
    assert embeddings.model == "embed-test"
    assert embeddings.limits.max_input_tokens == 4096


# --- short handles: a model cannot mistype a 36-character id it is never asked to copy -----------


async def test_a_citation_by_short_handle_is_translated_back_to_the_real_id() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["messages"][1]["content"])
        return _chat_reply(
            {"status": "supported", "rationale": "x", "cited_passage_ids": ["[P2]", "p9", "p1"]}
        )

    passages = [
        Passage(id="11111111-aaaa-bbbb-cccc-000000000001", content="First passage."),
        Passage(id="22222222-aaaa-bbbb-cccc-000000000002", content="Second passage."),
    ]
    verdict = await _llm(handler).judge_claim("c", passages)

    assert "[p1]" in seen[0] and "[p2]" in seen[0]
    assert "11111111" not in seen[0]  # the long id is never shown to the model
    # [P2] and p1 mean passages that were shown; p9 names nothing and is dropped, never guessed at.
    assert verdict.cited_passage_ids == [
        "22222222-aaaa-bbbb-cccc-000000000002",
        "11111111-aaaa-bbbb-cccc-000000000001",
    ]


def _section_reply(claim_ids: list[str]) -> httpx.Response:
    return _chat_reply(
        {"segments": [{"narration_text": "Plants make food from light.", "claim_ids": claim_ids}]}
    )


async def _section(handler: Handler, *, corrections: tuple[str, ...] = ()) -> list[Any]:
    from uuid import uuid4

    from edutoon.providers.llm import (
        Claim,
        ScriptBrief,
        SectionContinuity,
        SectionTopicBrief,
    )

    segments, _ = await _llm(handler).compose_script_section(
        ScriptBrief(topic="Photosynthesis", language="en-GB"),
        SectionTopicBrief(topic_id=uuid4(), title="Light", summary=None, keywords=()),
        [
            Claim(id="aaaaaaaa-1111-2222-3333-444444444441", text="Light is absorbed."),
            Claim(id="aaaaaaaa-1111-2222-3333-444444444442", text="Sugar is made."),
        ],
        SectionContinuity(lesson_title="A lesson"),
        duration_ms=30_000,
        max_segments=6,
        max_claims=15,
        words_per_minute=150,
        corrections=corrections,
    )
    return segments


async def test_a_section_cites_claims_by_short_handle_and_gets_real_ids_back() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["messages"][1]["content"])
        return _section_reply(["c2", "C1", "c2"])

    [segment] = await _section(handler)

    assert "[c1] Light is absorbed." in seen[0] and "aaaaaaaa" not in seen[0]
    assert segment.claim_ids == [
        "aaaaaaaa-1111-2222-3333-444444444442",
        "aaaaaaaa-1111-2222-3333-444444444441",
    ]


async def test_a_citation_of_something_that_was_never_shown_is_dropped_not_kept() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        # an id with a typo, a handle past the last claim, and a real handle
        return _section_reply(["c1", "aaaaaaaa-1111-2222-3333-4444444444419", "[c9]"])

    [segment] = await _section(handler)

    assert segment.claim_ids == ["aaaaaaaa-1111-2222-3333-444444444441"]


async def test_a_rewrite_is_asked_for_at_a_higher_temperature_and_says_what_was_wrong() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _section_reply(["c1"])

    await _section(handler)
    await _section(handler, corrections=('Passage 2 ("Plants make ...") mentions 10000.',))

    first, rewrite = seen
    assert first["options"]["temperature"] == 0
    assert rewrite["options"]["temperature"] == 0.7
    assert "previous answer for this section was rejected" in rewrite["messages"][1]["content"]
    assert "mentions 10000" in rewrite["messages"][1]["content"]
    assert "rejected" not in first["messages"][1]["content"]


# --- the storyboard schema declares the slots the templates require --------------------------

_MINIMAL_CONTENT: dict[str, dict[str, Any]] = {
    "title_card": {"title": "T"},
    "bullet_points": {"bullets": ["a", "b"]},
    "labelled_diagram": {
        "illustration": {"description": "d"},
        "labels": [{"text": "x", "position": "top"}],
    },
    "split_text_illustration": {"text": "t", "illustration": {"description": "d"}},
    "full_bleed_illustration": {"illustration": {"description": "d"}},
    "formula_or_code": {"content": "E = mc^2", "kind": "formula"},
}


def test_the_storyboard_schema_declares_every_slot_a_template_needs() -> None:
    """A model that decodes against the schema can only write what it declares. The schema once
    declared only ``illustration``, so a local model could never write a title or a bullet and all
    31 segments of a real run failed. Each template's schema must name its own required slots."""
    from edutoon.domain.storyboard_template import TEMPLATES, validate_scene_content
    from edutoon.providers.llm import _scene_schemas

    by_template = {
        schema["properties"]["template"]["enum"][0]: schema["properties"]["content"]
        for schema in _scene_schemas()
    }
    assert set(by_template) == TEMPLATES
    for template, content_schema in by_template.items():
        example = _MINIMAL_CONTENT[template]
        validate_scene_content(template, example)  # the example is what the domain accepts
        assert set(content_schema["required"]) <= set(example)
        assert set(example) <= set(content_schema["properties"])


def test_no_string_length_limit_reaches_the_decoding_grammar() -> None:
    """Grammar-constrained decoding enforces ``maxLength`` by ending the string where it stands, so
    a 100-character subtitle slot once produced "... differ in their obj" and the clipped text
    passed validation. The limit stays in the prompt and in the domain validators; it must not be
    in the grammar, at any depth, and nothing else in the schema may change."""
    from edutoon.providers.llm import _scene_schemas
    from edutoon.providers.ollama import _without_string_length_limits

    schema = {
        "type": "object",
        "required": ["subtitle"],
        "properties": {
            "subtitle": {"type": "string", "maxLength": 100},
            "items": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 5}},
        },
    }
    assert _without_string_length_limits(schema) == {
        "type": "object",
        "required": ["subtitle"],
        "properties": {
            "subtitle": {"type": "string"},
            "items": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
        },
    }
    assert "maxLength" in schema["properties"]["subtitle"]  # the original is not mutated
    for real in _scene_schemas():
        assert "maxLength" not in json.dumps(_without_string_length_limits(real))
