"""Embedding batching and retry: the planner (pure), the provider wrapper (fake SDK call) and the
transient-failure classification.

Nothing here reaches OpenAI: the wrapper is a real ``Embeddings`` around a real ``AsyncOpenAI``
whose ``embeddings.create`` is replaced, the same pattern as ``test_embeddings_provider.py``. The
database half (per-batch commits, retry of a failed job, project isolation) is in
``test_evidence_indexing.py``.
"""

from __future__ import annotations

import math
from typing import Any

import httpx
import openai
import pytest
from openai import AsyncOpenAI
from openai.types import CreateEmbeddingResponse, Embedding
from openai.types.create_embedding_response import Usage

from edutoon.domain import embedding_batches as eb
from edutoon.providers import errors as provider_errors
from edutoon.providers.embeddings import EmbeddingResponseError, Embeddings
from edutoon.providers.retry import RetryPolicy, retry_transient

MODEL = "text-embedding-3-small"

# --- the planner ---------------------------------------------------------------------------


def test_the_defaults_are_the_providers_own_ceilings() -> None:
    limits = eb.BatchLimits()
    assert (limits.max_inputs, limits.max_request_tokens, limits.max_input_tokens) == (
        2048,
        300_000,
        8192,
    )
    assert eb.EMBEDDING_DIMENSIONS == 1536


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_inputs": 0},
        {"max_inputs": 2049},
        {"max_input_tokens": 3},
        {"max_input_tokens": 8193},
        {"max_request_tokens": 300_001},
        {"max_input_tokens": 8000, "max_request_tokens": 7999},
    ],
)
def test_limits_above_the_providers_ceilings_or_incoherent_are_refused(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        eb.BatchLimits(**kwargs)


def test_the_token_estimate_is_the_utf8_length_so_it_never_undercounts() -> None:
    assert eb.estimate_tokens("") == 0
    assert eb.estimate_tokens("abc") == 3
    assert eb.estimate_tokens("é") == 2  # two bytes
    assert eb.estimate_tokens("हिन्दी") == len("हिन्दी".encode())  # up to a token per byte
    assert eb.estimate_tokens("日本語") == 9


def test_nothing_to_plan_gives_no_batches() -> None:
    assert eb.plan_batches([], eb.BatchLimits()) == []


def test_a_small_document_is_one_batch() -> None:
    assert eb.plan_batches([100, 200, 300], eb.BatchLimits()) == [(0, 1, 2)]


def test_batches_respect_the_input_count() -> None:
    batches = eb.plan_batches([1] * 5, eb.BatchLimits(max_inputs=2))
    assert batches == [(0, 1), (2, 3), (4,)]


def test_more_inputs_than_a_request_may_hold_are_split_at_2048() -> None:
    batches = eb.plan_batches([10] * 5000, eb.BatchLimits())
    assert [len(batch) for batch in batches] == [2048, 2048, 904]
    assert [i for batch in batches for i in batch] == list(range(5000))  # order kept, none lost


def test_batches_respect_the_total_token_budget() -> None:
    limits = eb.BatchLimits(max_request_tokens=10_000, max_input_tokens=8192)
    costs = [6000, 3000, 2000, 8000, 1000]
    batches = eb.plan_batches(costs, limits)
    assert batches == [(0, 1), (2, 3), (4,)]  # 9 000, then exactly 10 000, then 1 000
    for batch in batches:
        assert sum(costs[i] for i in batch) <= 10_000


def test_token_heavy_inputs_fill_a_request_to_the_budget_and_no_further() -> None:
    limits = eb.BatchLimits()
    batches = eb.plan_batches([8192] * 100, limits)  # 819 200 tokens in all
    assert all(sum(8192 for _ in batch) <= 300_000 for batch in batches)
    assert max(len(batch) for batch in batches) == 36  # 36 * 8192 = 294 912 <= 300 000
    assert [i for batch in batches for i in batch] == list(range(100))


def test_an_input_bigger_than_a_whole_request_is_clamped_and_gets_a_request_to_itself() -> None:
    limits = eb.BatchLimits(max_request_tokens=10_000)
    assert eb.plan_batches([1, 50_000, 1], limits) == [(0,), (1,), (2,)]


def test_a_negative_cost_is_refused() -> None:
    with pytest.raises(ValueError):
        eb.plan_batches([1, -1], eb.BatchLimits())


def test_text_within_the_bound_is_not_split() -> None:
    assert eb.split_oversized("short", 100) == ["short"]


def test_an_oversized_text_is_cut_into_windows_that_lose_nothing() -> None:
    text = " ".join(f"word{i}" for i in range(2000))
    windows = eb.split_oversized(text, 500)
    assert len(windows) > 1
    assert "".join(windows) == text  # every character, exactly once, in order
    assert all(eb.estimate_tokens(window) <= 500 for window in windows)


def test_windows_break_at_a_space_when_one_is_near() -> None:
    windows = eb.split_oversized("aaaa bbbb cccc dddd eeee ffff", 12)
    assert all(window.endswith(" ") for window in windows[:-1])


def test_multibyte_text_is_windowed_on_character_boundaries() -> None:
    text = "日本語のテキスト" * 200
    windows = eb.split_oversized(text, 100)
    assert "".join(windows) == text
    assert all(eb.estimate_tokens(window) <= 100 for window in windows)


def test_a_budget_too_small_for_one_character_is_refused() -> None:
    with pytest.raises(ValueError):
        eb.split_oversized("x" * 100, 3)


def test_pooling_one_vector_returns_it_unchanged() -> None:
    assert eb.pool_vectors([[3.0, 4.0]], [10]) == [3.0, 4.0]


def test_pooling_weights_by_length_and_scales_to_unit_length() -> None:
    pooled = eb.pool_vectors([[1.0, 0.0], [0.0, 1.0]], [3, 1])
    assert math.isclose(math.sqrt(sum(v * v for v in pooled)), 1.0)
    assert pooled[0] > pooled[1] > 0  # the longer window counts for more


def test_pooling_zero_vectors_does_not_divide_by_zero() -> None:
    assert eb.pool_vectors([[0.0, 0.0], [0.0, 0.0]], [1, 1]) == [0.0, 0.0]


@pytest.mark.parametrize(
    ("vectors", "weights"),
    [([], []), ([[1.0], [1.0]], [1]), ([[1.0], [1.0, 2.0]], [1, 1]), ([[1.0], [1.0]], [1, 0])],
)
def test_pooling_refuses_incoherent_input(vectors: list, weights: list) -> None:
    with pytest.raises(ValueError):
        eb.pool_vectors(vectors, weights)


# --- the provider wrapper ---------------------------------------------------------------------


class FakeCreate:
    """Stands in for ``client.embeddings.create``: records every request and answers with a
    deterministic vector per input (its length in the first slot, so a test can tell inputs
    apart), optionally raising scripted failures first."""

    def __init__(
        self,
        failures: list[BaseException] | None = None,
        width: int = 3,
        fail_at: dict[int, BaseException] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.failures = list(failures or [])  # raised by the first calls, in order
        self.fail_at = dict(fail_at or {})  # or by one specific call (1 = the first)
        self.width = width

    async def __call__(self, *, input: list[str], model: str, **_kwargs: Any):  # noqa: A002
        self.calls.append(list(input))
        if self.failures:
            raise self.failures.pop(0)
        if len(self.calls) in self.fail_at:
            raise self.fail_at.pop(len(self.calls))
        return CreateEmbeddingResponse(
            object="list",
            model=model,
            usage=Usage(prompt_tokens=0, total_tokens=0),
            data=[
                Embedding(
                    object="embedding",
                    index=i,
                    embedding=[float(len(text))] + [1.0] * (self.width - 1),
                )
                for i, text in enumerate(input)
            ],
        )


def _embeddings(
    monkeypatch: Any,
    create: FakeCreate,
    *,
    limits: eb.BatchLimits | None = None,
    attempts: int = 3,
) -> tuple[Embeddings, list[float]]:
    client = AsyncOpenAI(api_key="sk-test-not-a-real-key")
    monkeypatch.setattr(client.embeddings, "create", create)
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    embeddings = Embeddings(
        client,
        model=MODEL,
        limits=limits,
        retry=RetryPolicy(max_attempts=attempts, base_delay_seconds=1.0, max_delay_seconds=8.0),
        sleep=_sleep,
    )
    return embeddings, slept


def _status_error(status: int, *, code: str | None = None) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.example.invalid/v1/embeddings")
    body = {"code": code, "message": "PRIVATE-USER-MATERIAL"} if code else None
    return openai.APIStatusError(
        "PRIVATE-USER-MATERIAL", response=httpx.Response(status, request=request), body=body
    )


async def test_a_small_document_is_one_request(monkeypatch) -> None:
    create = FakeCreate()
    embeddings, _ = _embeddings(monkeypatch, create)
    vectors = await embeddings.embed(["a", "bb", "ccc"])
    assert len(create.calls) == 1
    assert [v[0] for v in vectors] == [1.0, 2.0, 3.0]


async def test_many_inputs_become_several_requests_and_come_back_in_order(monkeypatch) -> None:
    create = FakeCreate()
    embeddings, _ = _embeddings(monkeypatch, create, limits=eb.BatchLimits(max_inputs=2))
    texts = ["a" * n for n in range(1, 6)]
    vectors = await embeddings.embed(texts)
    assert [len(call) for call in create.calls] == [2, 2, 1]
    assert [v[0] for v in vectors] == [1.0, 2.0, 3.0, 4.0, 5.0]  # order preserved across requests


async def test_more_than_2048_inputs_never_send_a_request_over_the_limit(monkeypatch) -> None:
    create = FakeCreate()
    embeddings, _ = _embeddings(monkeypatch, create)
    texts = [f"t{i}" for i in range(5000)]
    vectors = await embeddings.embed(texts)
    assert [len(call) for call in create.calls] == [2048, 2048, 904]
    assert len(vectors) == 5000
    assert [v[0] for v in vectors[:3]] == [2.0, 2.0, 2.0]


async def test_a_token_heavy_document_never_sends_a_request_over_the_token_budget(
    monkeypatch,
) -> None:
    create = FakeCreate()
    embeddings, _ = _embeddings(monkeypatch, create)
    texts = ["x" * 8000 for _ in range(100)]  # 800 000 tokens by the safe estimate
    await embeddings.embed(texts)
    assert len(create.calls) > 2
    for call in create.calls:
        assert sum(eb.estimate_tokens(text) for text in call) <= 300_000
        assert len(call) <= 2048
    assert sum(len(call) for call in create.calls) == 100  # nothing dropped, nothing repeated


async def test_an_oversized_chunk_is_embedded_whole_in_windows_and_never_truncated(
    monkeypatch,
) -> None:
    create = FakeCreate(width=4)
    embeddings, _ = _embeddings(monkeypatch, create)
    giant = " ".join(f"word{i}" for i in range(6000))  # ~40 000 bytes, over the 8192 limit
    assert eb.estimate_tokens(giant) > 8192
    [vector] = await embeddings.embed([giant])
    sent = [text for call in create.calls for text in call]
    assert len(sent) > 1  # windowed
    assert "".join(sent) == giant  # every character was embedded: nothing truncated
    assert all(eb.estimate_tokens(text) <= 8192 for text in sent)
    assert len(vector) == 4  # still exactly one vector of the same width
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0)


async def test_an_oversized_chunk_among_normal_ones_keeps_every_position(monkeypatch) -> None:
    create = FakeCreate()
    embeddings, _ = _embeddings(monkeypatch, create)
    giant = "y" * 20_000
    vectors = await embeddings.embed(["a", giant, "ccc"])
    assert len(vectors) == 3
    assert vectors[0][0] == 1.0 and vectors[2][0] == 3.0  # neighbours untouched


async def test_a_transient_failure_is_retried_with_a_growing_wait(monkeypatch) -> None:
    create = FakeCreate(failures=[_status_error(429), _status_error(503)])
    embeddings, slept = _embeddings(monkeypatch, create)
    vectors = await embeddings.embed(["a"])
    assert len(create.calls) == 3  # two failures, then success
    assert len(vectors) == 1
    assert len(slept) == 2 and slept[1] > slept[0] > 0


async def test_timeouts_and_dropped_connections_are_retried(monkeypatch) -> None:
    request = httpx.Request("POST", "https://api.example.invalid/v1/embeddings")
    create = FakeCreate(
        failures=[
            openai.APITimeoutError(request=request),
            openai.APIConnectionError(request=request),
        ]
    )
    embeddings, _ = _embeddings(monkeypatch, create)
    await embeddings.embed(["a"])
    assert len(create.calls) == 3


async def test_only_the_failing_batch_is_retried_not_the_ones_before_it(monkeypatch) -> None:
    create = FakeCreate(fail_at={2: _status_error(429)})
    embeddings, _ = _embeddings(monkeypatch, create, limits=eb.BatchLimits(max_inputs=2))
    vectors = await embeddings.embed(["a", "b", "c", "d"])
    assert len(vectors) == 4
    # the first batch once, the second twice (one failure) - the first was not sent again
    assert create.calls == [["a", "b"], ["c", "d"], ["c", "d"]]


async def test_a_permanent_failure_is_not_retried(monkeypatch) -> None:
    create = FakeCreate(failures=[_status_error(400)])
    embeddings, slept = _embeddings(monkeypatch, create)
    with pytest.raises(openai.APIStatusError):
        await embeddings.embed(["a"])
    assert len(create.calls) == 1 and slept == []


async def test_an_exhausted_quota_is_not_retried_even_though_it_is_a_429(monkeypatch) -> None:
    create = FakeCreate(failures=[_status_error(429, code="insufficient_quota")])
    embeddings, slept = _embeddings(monkeypatch, create)
    with pytest.raises(openai.APIStatusError):
        await embeddings.embed(["a"])
    assert len(create.calls) == 1 and slept == []


async def test_a_bug_of_ours_is_not_retried(monkeypatch) -> None:
    create = FakeCreate(failures=[RuntimeError("boom")])
    embeddings, _ = _embeddings(monkeypatch, create)
    with pytest.raises(RuntimeError):
        await embeddings.embed(["a"])
    assert len(create.calls) == 1


async def test_retries_stop_at_the_attempt_limit_and_raise_the_last_error(monkeypatch) -> None:
    create = FakeCreate(failures=[_status_error(503)] * 5)
    embeddings, slept = _embeddings(monkeypatch, create, attempts=3)
    with pytest.raises(openai.APIStatusError):
        await embeddings.embed(["a"])
    assert len(create.calls) == 3 and len(slept) == 2


async def test_a_failure_returns_nothing_partial(monkeypatch) -> None:
    create = FakeCreate(fail_at={2: _status_error(503)})
    embeddings, _ = _embeddings(
        monkeypatch, create, limits=eb.BatchLimits(max_inputs=1), attempts=1
    )
    with pytest.raises(openai.APIStatusError):
        await embeddings.embed(["a", "b"])  # an exception, never a list with a hole in it
    assert create.calls == [["a"], ["b"]]


async def test_a_response_with_the_wrong_number_of_vectors_is_refused(monkeypatch) -> None:
    client = AsyncOpenAI(api_key="sk-test-not-a-real-key")

    async def short(*, input: list[str], model: str, **_kwargs: Any):  # noqa: A002
        return CreateEmbeddingResponse(
            object="list",
            model=model,
            usage=Usage(prompt_tokens=0, total_tokens=0),
            data=[Embedding(object="embedding", index=0, embedding=[1.0])],
        )

    monkeypatch.setattr(client.embeddings, "create", short)
    embeddings = Embeddings(client, model=MODEL)
    with pytest.raises(EmbeddingResponseError):
        await embeddings.embed(["a", "b"])


# --- what is worth retrying -------------------------------------------------------------------


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504, 529])
def test_transient_statuses(status: int) -> None:
    assert provider_errors.is_transient_provider_error(_status_error(status))


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_permanent_statuses(status: int) -> None:
    assert not provider_errors.is_transient_provider_error(_status_error(status))


def test_an_exhausted_quota_names_the_real_problem_and_is_permanent() -> None:
    error = _status_error(429, code="insufficient_quota")
    assert not provider_errors.is_transient_provider_error(error)
    assert provider_errors.describe_provider_error(error) == provider_errors.QUOTA_EXHAUSTED


def test_an_ordinary_rate_limit_still_reads_as_a_rate_limit() -> None:
    error = _status_error(429)
    assert provider_errors.is_transient_provider_error(error)
    assert provider_errors.describe_provider_error(error) == provider_errors.RATE_LIMITED


def test_the_providers_retry_after_is_read_and_capped_by_the_policy() -> None:
    request = httpx.Request("POST", "https://api.example.invalid/v1/embeddings")
    response = httpx.Response(429, headers={"retry-after": "5"}, request=request)
    error = openai.APIStatusError("x", response=response, body=None)
    assert provider_errors.retry_after_seconds(error) == 5.0
    assert provider_errors.retry_after_seconds(RuntimeError()) is None
    junk = httpx.Response(429, headers={"retry-after": "soon"}, request=request)
    assert (
        provider_errors.retry_after_seconds(openai.APIStatusError("x", response=junk, body=None))
        is None
    )


async def test_retry_after_lengthens_the_wait_but_never_past_the_policy_maximum() -> None:
    request = httpx.Request("POST", "https://api.example.invalid/v1/embeddings")
    slow = httpx.Response(429, headers={"retry-after": "999"}, request=request)
    errors = [openai.APIStatusError("x", response=slow, body=None)]
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    async def operation() -> str:
        if errors:
            raise errors.pop()
        return "ok"

    policy = RetryPolicy(max_attempts=2, base_delay_seconds=1.0, max_delay_seconds=8.0)
    assert await retry_transient(operation, policy=policy, what="t", sleep=_sleep) == "ok"
    assert slept == [8.0]


def test_the_backoff_doubles_and_jitter_keeps_between_half_and_all_of_it() -> None:
    policy = RetryPolicy(max_attempts=5, base_delay_seconds=2.0, max_delay_seconds=10.0)
    assert policy.delay_before_retry(1, jitter=1.0) == 2.0
    assert policy.delay_before_retry(2, jitter=1.0) == 4.0
    assert policy.delay_before_retry(3, jitter=1.0) == 8.0
    assert policy.delay_before_retry(4, jitter=1.0) == 10.0  # capped
    assert policy.delay_before_retry(3, jitter=0.0) == 4.0  # half


def test_a_policy_must_allow_at_least_one_attempt() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay_seconds=-1)
