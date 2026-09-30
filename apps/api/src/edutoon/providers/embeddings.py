"""OpenAI embeddings - the only module allowed to import ``openai``
(rule 3).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog
from openai import AsyncOpenAI

from edutoon.core.config import Settings
from edutoon.domain.embedding_batches import (
    BatchLimits,
    estimate_tokens,
    plan_batches,
    pool_vectors,
    split_oversized,
)
from edutoon.providers.retry import RetryPolicy, retry_policy_from_settings, retry_transient

log = structlog.get_logger()


class EmbeddingResponseError(Exception):
    """The provider answered, but not with one vector per input - never returned as if it were
    a result."""


class Embeddings:
    """Thin async wrapper over an OpenAI client bound to one embedding
    model. The model travels with the wrapper (not passed per-call) so
    every embedding produced through one instance is comparable - mixing
    models within a single index would make vectors incompatible with each
    other despite sharing a column.

    :meth:`embed` accepts any number of texts of any length. It splits them into
    requests that respect the provider's three bounds (inputs per request, tokens per
    request, tokens per input - :mod:`edutoon.domain.embedding_batches`), repeats a
    request that failed transiently (:mod:`edutoon.providers.retry`), and never
    truncates: a text too long for one input is embedded in windows whose vectors are
    pooled into the one vector its chunk stores.
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        model: str,
        limits: BatchLimits | None = None,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._model = model
        self._limits = limits or BatchLimits()
        self._retry = retry or RetryPolicy()
        self._sleep = sleep

    @property
    def model(self) -> str:
        return self._model

    @property
    def limits(self) -> BatchLimits:
        """The bounds one request respects - a caller that persists results batch by batch
        groups its inputs with these so each group is one request."""
        return self._limits

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts``, returned in the same order they were given - each request's
        results are explicitly re-sorted by the response's own ``index`` field rather than
        trusted implicitly, since callers zip this against their input list to know which
        vector belongs to which chunk.

        Any failure that survives the retry policy is raised, and nothing partial is returned:
        the result is either a vector for every text or an exception.
        """
        if not texts:
            return []

        # Cut a text that cannot fit one input slot into windows (never truncate it).
        inputs: list[str] = []
        owner: list[int] = []  # inputs[i] is a window of texts[owner[i]]
        for position, text in enumerate(texts):
            windows = split_oversized(text, self._limits.max_input_tokens)
            if len(windows) > 1:
                log.warning(
                    "embeddings.oversized_input_windowed",
                    input_index=position,
                    windows=len(windows),
                )
            inputs.extend(windows)
            owner.extend([position] * len(windows))

        vectors: list[list[float] | None] = [None] * len(inputs)
        for batch in plan_batches([estimate_tokens(text) for text in inputs], self._limits):
            batch_vectors = await self._embed_batch([inputs[i] for i in batch])
            for position, vector in zip(batch, batch_vectors, strict=True):
                vectors[position] = vector

        grouped: dict[int, list[int]] = {}
        for input_index, text_index in enumerate(owner):
            grouped.setdefault(text_index, []).append(input_index)

        result: list[list[float]] = []
        for text_index in range(len(texts)):
            members = grouped[text_index]
            window_vectors = [vectors[i] for i in members]
            assert all(vector is not None for vector in window_vectors)  # every batch filled it
            result.append(
                pool_vectors(
                    [v for v in window_vectors if v is not None],
                    [estimate_tokens(inputs[i]) for i in members],
                )
            )
        return result

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        async def _call() -> list[list[float]]:
            response = await self._client.embeddings.create(input=texts, model=self._model)
            by_index = {item.index: item.embedding for item in response.data}
            if len(response.data) != len(texts) or set(by_index) != set(range(len(texts))):
                raise EmbeddingResponseError(
                    f"The embeddings response held {len(response.data)} vectors "
                    f"for {len(texts)} inputs."
                )
            return [by_index[i] for i in range(len(texts))]

        return await retry_transient(
            _call, policy=self._retry, what="embeddings.create", sleep=self._sleep
        )


def embedding_limits_from_settings(settings: Settings) -> BatchLimits:
    """The one place configuration becomes the size of an embeddings request."""
    return BatchLimits(
        max_inputs=settings.EMBEDDING_MAX_INPUTS_PER_REQUEST,
        max_request_tokens=settings.EMBEDDING_MAX_REQUEST_TOKENS,
    )


def embeddings_from_settings(settings: Settings) -> Embeddings:
    """The embeddings client the API and the worker both use, built from configuration."""
    return get_embeddings_client(
        api_key=settings.OPENAI_API_KEY,
        model=settings.EMBEDDING_MODEL,
        limits=embedding_limits_from_settings(settings),
        retry=retry_policy_from_settings(settings),
    )


def get_embeddings_client(
    *,
    api_key: str,
    model: str,
    limits: BatchLimits | None = None,
    retry: RetryPolicy | None = None,
) -> Embeddings:
    # The SDK's own retries are off: the retry policy above is the only one, so a failing call
    # is not repeated (2 + 1) x (attempts) times and the number of tries is a setting, not a guess.
    return Embeddings(
        AsyncOpenAI(api_key=api_key, max_retries=0), model=model, limits=limits, retry=retry
    )
