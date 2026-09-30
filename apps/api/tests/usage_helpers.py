"""The shape of ``result["usage"]`` that a job stores, for tests that assert on a whole result.

One call of one kind whose response carried input and output tokens (and no cache figures) is the
common case; :data:`NO_USAGE` is what a job that never got an answer stores.
"""

from __future__ import annotations

from typing import Any


def entry(
    *,
    calls: int = 1,
    with_usage: int | None = None,
    input_tokens: int | None,
    output_tokens: int | None,
) -> dict[str, Any]:
    return {
        "calls": calls,
        "calls_with_usage": calls if with_usage is None else with_usage,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": None,
        "cache_read_input_tokens": None,
    }


def usage_of(kind: str, input_tokens: int, output_tokens: int) -> dict[str, Any]:
    """The stored usage of a job that made exactly one ``kind`` call."""
    one = entry(input_tokens=input_tokens, output_tokens=output_tokens)
    return {"total": one, "by_call": {kind: dict(one)}}


NO_USAGE: dict[str, Any] = {
    "total": entry(calls=0, input_tokens=None, output_tokens=None),
    "by_call": {},
}
