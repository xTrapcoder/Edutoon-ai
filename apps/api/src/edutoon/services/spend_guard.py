"""Guards against accidental or abusive provider spend - the smallest safe set for pre-production.

The application does not meter or bill; the provider consoles' own spend limits are the real
ceiling and **must be set before any real-provider testing**. What this module adds is a cheap
in-process stop for the two ways spend runs away without anyone meaning it:

* **a runaway job** - :func:`edutoon.services.llm_usage.new_usage_report` arms every job with a
  ceiling on its provider calls (``MAX_PROVIDER_CALLS_PER_JOB``). It is a tripwire, far above what
  any bounded job (``domain.script_duration.max_provider_calls``, one call per segment, one per
  topic) ever makes, so it only trips on a bug; the job stops before the next call and says why.
* **a runaway caller** - a client that starts paid work over and over (each request with a fresh
  ``Idempotency-Key``, so idempotency does not help) is stopped by counting, per project and per
  hour, the jobs that spend on a provider (:data:`PROVIDER_JOB_KINDS`) and the claims typed in by
  hand. Past the limit the request is refused with a ``429`` before anything is started.

Both count from the database (the jobs and claim tables), so the limit survives a restart and
needs no new infrastructure, and a job that failed still counts: it was paid for. Rendering and
narration are local compute with their own one-at-a-time guards and are not counted here.

An idempotent replay is not a new start and is never counted: callers check for the replay first.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.config import get_settings
from edutoon.core.errors import TooManyRequestsError
from edutoon.repositories import claim_verifications as claim_verifications_repo
from edutoon.repositories import jobs as jobs_repo

PROVIDER_JOB_KINDS: Final[tuple[str, ...]] = (
    "build_evidence",
    "extract_topics",
    "extract_claims",
    "generate_script",
    "generate_storyboard",
)
"""The job kinds that call a paid provider (embeddings or the language model)."""

WINDOW: Final = timedelta(hours=1)


async def ensure_can_start_provider_job(session: AsyncSession, *, project_id: UUID) -> None:
    """Raise :class:`~edutoon.core.errors.TooManyRequestsError` if ``project_id`` has already
    started ``MAX_PROVIDER_JOBS_PER_PROJECT_PER_HOUR`` paid jobs in the last hour."""
    limit = get_settings().MAX_PROVIDER_JOBS_PER_PROJECT_PER_HOUR
    started = await jobs_repo.count_created_within(
        session, project_id=project_id, kinds=PROVIDER_JOB_KINDS, within=WINDOW
    )
    if started >= limit:
        raise TooManyRequestsError(
            f"This project has already started {started} AI jobs in the last hour, the most "
            f"it may ({limit}). Nothing was started; please try again later."
        )


async def ensure_can_verify_manual_claim(session: AsyncSession, *, project_id: UUID) -> None:
    """Raise :class:`~edutoon.core.errors.TooManyRequestsError` if ``project_id`` has already had
    ``MAX_MANUAL_CLAIM_VERIFICATIONS_PER_PROJECT_PER_HOUR`` typed-in claims verified in the last
    hour."""
    limit = get_settings().MAX_MANUAL_CLAIM_VERIFICATIONS_PER_PROJECT_PER_HOUR
    verified = await claim_verifications_repo.count_manual_within(
        session, project_id=project_id, within=WINDOW
    )
    if verified >= limit:
        raise TooManyRequestsError(
            f"This project has already had {verified} claims verified by hand in the last hour, "
            f"the most it may ({limit}). Nothing was verified; please try again later."
        )
