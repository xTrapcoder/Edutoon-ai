"""Recording what a language-model call cost, including when its answer is rejected.

A job builds one :class:`~edutoon.domain.llm_usage.UsageReport`, records every call it makes into
it with :func:`record_call`, and stores the report on its result - failed jobs too, since a run
that fails on its fifth call has still paid for the first four. The kinds of call and the rules for
combining them live in :mod:`edutoon.domain.llm_usage`; this module is only the glue between a
provider's return value (or its :class:`~edutoon.providers.llm.LLMResponseError`) and a report.
"""

from __future__ import annotations

from collections.abc import Awaitable
from inspect import iscoroutine

from edutoon.core.config import get_settings
from edutoon.domain.llm_usage import UsageReport
from edutoon.providers.llm import LLMResponseError, TokenUsage


def new_usage_report() -> UsageReport:
    """A report for one job, armed with the per-job provider-call tripwire."""
    return UsageReport(max_calls=get_settings().MAX_PROVIDER_CALLS_PER_JOB)


async def record_call[T](
    report: UsageReport, kind: str, call: Awaitable[tuple[T, TokenUsage]]
) -> T:
    """Await a provider call that returns ``(result, usage)``, record its usage under ``kind``, and
    return the result.

    If the provider answered and the answer was rejected (:class:`LLMResponseError` - cut off,
    refused, malformed), the usage carried by the error is recorded before it propagates. A failure
    with no answer at all (a timeout, a rate limit) records nothing: there is no usage to record.

    The job's call tripwire is checked first: if the job has already made the most calls it may,
    :class:`~edutoon.domain.llm_usage.ProviderCallLimitError` is raised and ``call`` is never
    awaited, so the call that would have exceeded the limit costs nothing.
    """
    try:
        report.ensure_capacity()
    except BaseException:
        if iscoroutine(call):
            call.close()  # never started: no "coroutine was never awaited" warning
        raise
    try:
        result, usage = await call
    except LLMResponseError as exc:
        report.add(kind, exc.usage)
        raise
    report.add(kind, usage)
    return result
