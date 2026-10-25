"""What a failed provider call may say to the person who started the job, and which failures are
worth repeating (:func:`is_transient_provider_error`).

An SDK exception's own text is not safe to show: it carries the provider's
response body (which can echo the request, including the user's own material),
request ids, and whatever else the provider chose to put in it. This module turns
a provider error into one of a small, fixed set of sentences, chosen from its
*kind* (rate limit, timeout, bad request, ...) and HTTP status alone - never from
``str(exc)``, ``exc.body`` or ``exc.response``. The full exception is for the
server log.

Lives in ``providers`` because it must name the OpenAI and Anthropic SDK types
(rule 3: no provider SDK is imported anywhere else). Both SDKs share the same
shape - ``APITimeoutError`` under ``APIConnectionError``, ``APIStatusError`` with
a ``status_code`` - so the two are handled together.
"""

from __future__ import annotations

from typing import Literal

import anthropic
import openai

LocalFailure = Literal[
    "unreachable", "timeout", "model_missing", "rejected", "unavailable", "dimension", "unexpected"
]


class LocalModelError(Exception):
    """The local model server (Ollama, ``providers.ollama``) could not answer. Carries only the
    *kind* of failure and an HTTP status - never the server's response text, which can echo the
    request and so the user's own material."""

    def __init__(self, kind: LocalFailure, *, status_code: int | None = None) -> None:
        super().__init__(kind if status_code is None else f"{kind} (HTTP {status_code})")
        self.kind: LocalFailure = kind
        self.status_code = status_code


_TIMEOUT = (anthropic.APITimeoutError, openai.APITimeoutError)
_CONNECTION = (anthropic.APIConnectionError, openai.APIConnectionError)
_STATUS = (anthropic.APIStatusError, openai.APIStatusError)
_ANY_PROVIDER_ERROR = (anthropic.AnthropicError, openai.OpenAIError)

RATE_LIMITED = (
    "The AI provider is rate-limiting requests right now. Please try again in a few minutes."
)
TIMED_OUT = "The AI provider took too long to respond. Please try again."
UNREACHABLE = "The AI provider could not be reached. Please try again."
UNAVAILABLE = "The AI provider is temporarily unavailable. Please try again shortly."
NOT_AUTHORISED = (
    "This service is not currently authorised to use the AI provider. "
    "This is a configuration problem - please contact support."
)
MODEL_UNAVAILABLE = (
    "The AI model this service is configured to use is not available. "
    "This is a configuration problem - please contact support."
)
TOO_LARGE = "The material was too large for the AI provider to process."
REJECTED = "The AI provider could not process this request."
UNEXPECTED = "The AI provider returned an unexpected error."
QUOTA_EXHAUSTED = (
    "The AI provider account has no remaining quota or credit. "
    "This is a configuration problem - please contact support."
)


LOCAL_MESSAGES: dict[LocalFailure, str] = {
    "unreachable": (
        "The local AI model server (Ollama) could not be reached. "
        "Check that it is running, then try again."
    ),
    "timeout": (
        "The local AI model took too long to respond. A smaller model, or fewer topics, may help."
    ),
    "model_missing": (
        "The local AI model this service is configured to use has not been downloaded. "
        "Pull it in Ollama, then try again."
    ),
    "rejected": "The local AI model server could not process this request.",
    "unavailable": (
        "The local AI model server failed while answering. Check its log, then try again."
    ),
    "dimension": (
        "The local embedding model produces wider vectors than the database stores. "
        "Choose a model with at most 1536 dimensions."
    ),
    "unexpected": "The local AI model server returned an unexpected answer.",
}

_LOCAL_TRANSIENT: frozenset[LocalFailure] = frozenset({"unreachable", "timeout", "unavailable"})


def describe_provider_error(exc: BaseException) -> str | None:
    """A message safe to show the job's owner for a provider SDK error, or
    ``None`` if ``exc`` is not one (the caller then decides what to say).
    """
    if isinstance(exc, LocalModelError):
        return LOCAL_MESSAGES[exc.kind]
    if isinstance(exc, _TIMEOUT):
        return TIMED_OUT
    if isinstance(exc, _CONNECTION):
        return UNREACHABLE
    if isinstance(exc, _STATUS):
        if getattr(exc, "code", None) == "insufficient_quota":
            return QUOTA_EXHAUSTED
        return _describe_status(exc.status_code)
    if isinstance(exc, _ANY_PROVIDER_ERROR):
        return UNEXPECTED
    return None


def _describe_status(status: int) -> str:
    if status in (401, 403):
        return NOT_AUTHORISED
    if status == 404:
        return MODEL_UNAVAILABLE
    if status in (408, 504):
        return TIMED_OUT
    if status == 413:
        return TOO_LARGE
    if status == 429:
        return RATE_LIMITED
    if status >= 500:  # includes Anthropic's 529 "overloaded"
        return UNAVAILABLE
    if status in (400, 422):
        return REJECTED
    return UNEXPECTED


# --- which failures are worth repeating -------------------------------------------------------

_TRANSIENT_STATUSES = frozenset({408, 409, 429})


def is_transient_provider_error(exc: BaseException) -> bool:
    """Whether repeating the call that raised ``exc`` could plausibly succeed: a timeout, a
    dropped connection, a rate limit, a conflict the provider asks you to retry, or a 5xx
    (Anthropic's 529 "overloaded" included). A bad request, a rejected key, a missing model, a
    payload that is too large and - deliberately - an *exhausted quota* are not: they fail the same
    way every time, and retrying a paid call that cannot succeed only wastes time and money.

    A local model server that is unreachable, timed out or failed mid-answer is worth repeating (it
    may be loading a model); a missing model, a rejected request or too-wide vectors are not."""
    if isinstance(exc, LocalModelError):
        return exc.kind in _LOCAL_TRANSIENT
    if isinstance(exc, _TIMEOUT + _CONNECTION):
        return True
    if isinstance(exc, _STATUS):
        if getattr(exc, "code", None) == "insufficient_quota":
            return False  # a 429 that means "your billing is exhausted", not "slow down"
        return exc.status_code in _TRANSIENT_STATUSES or exc.status_code >= 500
    return False


def retry_after_seconds(exc: BaseException) -> float | None:
    """How long the provider asked the caller to wait (its ``Retry-After`` header, in seconds), or
    ``None`` if it did not say or said something unusable."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        value = float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None
