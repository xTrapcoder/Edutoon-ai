"""How text is grouped into embedding requests - pure domain logic (rule 1: no I/O, no imports of
repositories / providers / services, and no configuration reads).

An embedding request is bounded three ways by the provider (OpenAI ``text-embedding-3-small``, the
model this project indexes with): at most :data:`MAX_INPUTS_PER_REQUEST` inputs, at most
:data:`MAX_TOKENS_PER_REQUEST` tokens across all of them, and at most :data:`MAX_TOKENS_PER_INPUT`
tokens in any one input. A PDF is indexed as one input per page, so a long, dense document can
break any of the three; sending it as a single request fails the whole document on the first
breach and leaves nothing indexed.

This module decides only *how to group*: which inputs go in which request
(:func:`plan_batches`), how an input too long for one request slot is cut into windows
(:func:`split_oversized`) and how the windows' vectors are folded back into the one vector the
input's chunk stores (:func:`pool_vectors`). It never truncates: every character of an input is
embedded, and a chunk always keeps its whole text.

**Token counts are an upper bound, not a measurement.** The real tokenizer is not a
dependency, so :func:`estimate_tokens` returns the UTF-8 length in bytes. A byte-level BPE tokenizer
(OpenAI's) never produces a token shorter than one byte, so bytes is a strict upper bound on tokens
for any text in any script - an English page is over-counted about fourfold, and Devanagari or
Chinese text, which costs up to a token per byte, is not under-counted. The price of being safe is
a few more, smaller requests; the alternative is a request that fails at the provider. The vector
dimension (:data:`EMBEDDING_DIMENSIONS`) is the database column's (``vector(1536)``) and is checked
before a vector is stored.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

MAX_INPUTS_PER_REQUEST: Final = 2048
"""The provider's ceiling on inputs in one request."""

MAX_TOKENS_PER_REQUEST: Final = 300_000
"""The provider's ceiling on tokens summed across one request's inputs."""

MAX_TOKENS_PER_INPUT: Final = 8192
"""The provider's ceiling on tokens in any single input."""

EMBEDDING_DIMENSIONS: Final = 1536
"""The width of ``source_chunks.embedding`` (``vector(1536)``) and of the model's output."""

_MIN_INPUT_TOKENS: Final = 4
"""The smallest per-input budget accepted: the widest UTF-8 character is four bytes, so a smaller
one could never hold even one character."""


@dataclass(frozen=True, slots=True)
class BatchLimits:
    """The bounds one embedding request must respect. The defaults are the provider's own ceilings;
    a smaller value is accepted (to make requests smaller), a larger one is not."""

    max_inputs: int = MAX_INPUTS_PER_REQUEST
    max_request_tokens: int = MAX_TOKENS_PER_REQUEST
    max_input_tokens: int = MAX_TOKENS_PER_INPUT

    def __post_init__(self) -> None:
        if not 1 <= self.max_inputs <= MAX_INPUTS_PER_REQUEST:
            raise ValueError(f"max_inputs must be between 1 and {MAX_INPUTS_PER_REQUEST}.")
        if not _MIN_INPUT_TOKENS <= self.max_input_tokens <= MAX_TOKENS_PER_INPUT:
            raise ValueError(
                f"max_input_tokens must be between {_MIN_INPUT_TOKENS} and {MAX_TOKENS_PER_INPUT}."
            )
        if not self.max_input_tokens <= self.max_request_tokens <= MAX_TOKENS_PER_REQUEST:
            raise ValueError(
                f"max_request_tokens must be between max_input_tokens and {MAX_TOKENS_PER_REQUEST}."
            )


def estimate_tokens(text: str) -> int:
    """A strict upper bound on ``text``'s token count: its length in UTF-8 bytes (see the module
    docstring for why that is safe for any script)."""
    return len(text.encode("utf-8"))


def split_oversized(text: str, max_tokens: int) -> list[str]:
    """``text`` cut into consecutive windows, each within ``max_tokens`` by
    :func:`estimate_tokens`, that together contain every character of ``text`` exactly once
    (``"".join(windows) == text``). A window ends at a space when one falls in its last quarter, so
    words are not cut in half where avoidable. Text already within the bound is returned whole.
    Raises :class:`ValueError` for a ``max_tokens`` too small to hold one character."""
    if max_tokens < _MIN_INPUT_TOKENS:
        raise ValueError(f"max_tokens must be at least {_MIN_INPUT_TOKENS}.")
    if estimate_tokens(text) <= max_tokens:
        return [text]

    windows: list[str] = []
    start, length = 0, len(text)
    while start < length:
        end, used = start, 0
        while end < length:
            width = len(text[end].encode("utf-8"))
            if used + width > max_tokens:
                break
            used += width
            end += 1
        if end < length:
            space = text.rfind(" ", start + (end - start) * 3 // 4, end)
            if space >= start:
                end = space + 1
        windows.append(text[start:end])
        start = end
    return windows


def plan_batches(costs: Sequence[int], limits: BatchLimits) -> list[tuple[int, ...]]:
    """Group input positions ``0..len(costs)-1`` into requests, in order, so that no request has
    more than ``limits.max_inputs`` inputs or more than ``limits.max_request_tokens`` tokens.

    Greedy and order-preserving: an input joins the current request if it still fits, otherwise
    it opens the next one, so the result is deterministic and every position appears exactly
    once. ``costs[i]`` is input ``i``'s :func:`estimate_tokens`; a cost above the whole request's
    budget is clamped to it (that input then gets a request to itself - the caller cuts it into
    windows first, so this only matters to a caller that has not). Raises :class:`ValueError` for a
    negative cost.
    """
    batches: list[tuple[int, ...]] = []
    current: list[int] = []
    used = 0
    for position, raw_cost in enumerate(costs):
        if raw_cost < 0:
            raise ValueError("A cost must not be negative.")
        cost = min(raw_cost, limits.max_request_tokens)
        if current and (
            len(current) >= limits.max_inputs or used + cost > limits.max_request_tokens
        ):
            batches.append(tuple(current))
            current, used = [], 0
        current.append(position)
        used += cost
    if current:
        batches.append(tuple(current))
    return batches


def pool_vectors(vectors: Sequence[Sequence[float]], weights: Sequence[int]) -> list[float]:
    """One vector standing for several windows of the same text: their mean, weighted by each
    window's length, scaled to unit length (the model's vectors are unit length, and cosine
    similarity - the retrieval metric - ignores length anyway). A single vector is returned
    unchanged. Raises :class:`ValueError` for no vectors, mismatched counts or widths, or a
    non-positive weight."""
    if not vectors:
        raise ValueError("There is nothing to pool.")
    if len(vectors) != len(weights):
        raise ValueError("Each vector needs exactly one weight.")
    if len(vectors) == 1:
        return list(vectors[0])
    if any(weight <= 0 for weight in weights):
        raise ValueError("Every weight must be positive.")
    width = len(vectors[0])
    if any(len(vector) != width for vector in vectors):
        raise ValueError("Vectors to pool must all have the same width.")

    total = sum(weights)
    mean = [
        sum(vector[i] * weight for vector, weight in zip(vectors, weights, strict=True)) / total
        for i in range(width)
    ]
    norm = math.sqrt(sum(value * value for value in mean))
    return [value / norm for value in mean] if norm > 0 else mean
