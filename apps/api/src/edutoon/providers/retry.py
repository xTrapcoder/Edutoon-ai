"""One retry policy for provider calls that are safe to repeat.

A provider call fails transiently far more often than it fails for good: a rate limit, a
timeout, a dropped connection, a 5xx (including Anthropic's 529 "overloaded").
:func:`retry_transient` repeats an operation that failed in one of those ways - and only those
(:func:`~edutoon.providers.errors.is_transient_provider_error`) - up to ``policy.max_attempts``
times in all, waiting exponentially longer between tries, with jitter so many workers do not retry
in step, and honouring the provider's own ``Retry-After`` when it sends one (never longer than
``max_delay_seconds``). Anything else - a bad request, a rejected key,
an exhausted quota, a bug of ours - is raised on the first attempt, unchanged: retrying it would
only spend time and, for a paid call, money.

The operation must be safe to repeat: an embedding request is (it has no side effect), and the
caller decides for anything else. Nothing here logs an exception's message (a provider's error
text can echo the request, which contains a user's own material): only its type and status.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

import structlog

from edutoon.core.config import Settings
from edutoon.providers.errors import is_transient_provider_error, retry_after_seconds

log = structlog.get_logger()

DEFAULT_MAX_ATTEMPTS: Final = 3
DEFAULT_BASE_DELAY_SECONDS: Final = 2.0
DEFAULT_MAX_DELAY_SECONDS: Final = 30.0


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How often, and how patiently, a transient failure is retried. ``max_attempts`` counts the
    first try, so ``1`` means never retry."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS
    max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("Delays must not be negative.")

    def delay_before_retry(self, attempt: int, *, jitter: float = 1.0) -> float:
        """Seconds to wait after failed attempt number ``attempt`` (1 for the first try):
        ``base * 2 ** (attempt - 1)`` capped at ``max_delay_seconds``, then scaled by
        ``0.5 + jitter / 2`` for a ``jitter`` in ``[0, 1]`` - so between half and all of it."""
        raw = float(min(self.max_delay_seconds, self.base_delay_seconds * 2 ** (attempt - 1)))
        return raw * (0.5 + min(max(jitter, 0.0), 1.0) / 2)


def retry_policy_from_settings(settings: Settings) -> RetryPolicy:
    """The one place configuration becomes a :class:`RetryPolicy`."""
    return RetryPolicy(
        max_attempts=settings.PROVIDER_RETRY_ATTEMPTS,
        base_delay_seconds=settings.PROVIDER_RETRY_BASE_DELAY_SECONDS,
        max_delay_seconds=settings.PROVIDER_RETRY_MAX_DELAY_SECONDS,
    )


async def retry_transient[T](
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    what: str,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[], float] = random.random,
) -> T:
    """Run ``operation``; on a transient provider failure wait and run it again, up to
    ``policy.max_attempts`` attempts. The last failure - or the first non-transient one - is raised
    unchanged. ``what`` names the call for the log."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return await operation()
        except Exception as exc:
            if attempt >= policy.max_attempts or not is_transient_provider_error(exc):
                raise
            delay = policy.delay_before_retry(attempt, jitter=jitter())
            advised = retry_after_seconds(exc)
            if advised is not None:
                delay = min(max(delay, advised), policy.max_delay_seconds)
            log.warning(
                "provider.retrying",
                call=what,
                attempt=attempt,
                max_attempts=policy.max_attempts,
                error_type=type(exc).__name__,
                status=getattr(exc, "status_code", None),
                wait_seconds=round(delay, 2),
            )
            await sleep(delay)
