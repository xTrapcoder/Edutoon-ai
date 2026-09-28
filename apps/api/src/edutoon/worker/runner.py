"""The job worker's poll/claim/dispatch loop (Phase C).

``process_one_job`` is the whole unit of work: claim at most one job, run
whatever handler its kind maps to, and leave it terminal (``succeeded`` /
``failed``) - never stuck ``running``. ``run_forever`` just calls that in a
loop until asked to stop.

Two separate sessions/transactions per job, deliberately:

1. The claim (``services.jobs.claim_next_job``) runs in its own session,
   committed immediately. This is the only part that takes the
   ``FOR UPDATE SKIP LOCKED`` row lock, and it holds it for a single UPDATE
   statement - not for the lifetime of whatever the job goes on to do.
2. The handler (an existing ``run_*_job`` function) runs in a second,
   fresh session. Those functions already do their own "mark running",
   business logic, and "mark succeeded/failed" inside the one session
   they're given, committed here as one transaction - that transaction
   does span the handler's AI call, which is unavoidable without changing
   those functions (out of scope: this worker only orchestrates, it
   doesn't re-implement their business logic). Keeping it a *second*
   transaction, separate from the claim, at least means the claim's lock
   is never the thing held open for that long.

``sessionmaker`` is also handed to the handler itself (Phase 7): a
long-running handler such as ``scripts.run_generate_script_job`` commits its
own "running" transition right away, releasing that row's lock before its
generation work begins, and then reports interim progress through short,
independent sessions opened from this same ``sessionmaker`` - never through
``session`` (which stays free for the handler's own reads and the single
final write). Every other handler ignores the extra argument.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from edutoon.repositories import jobs as jobs_repo
from edutoon.services import jobs as jobs_service
from edutoon.services.job_errors import describe_job_failure
from edutoon.worker.dispatch import JOB_HANDLERS, WorkerProviders

log = structlog.get_logger()

DEFAULT_POLL_INTERVAL_SECONDS = 2.0


async def process_one_job(
    sessionmaker: async_sessionmaker[AsyncSession],
    providers: WorkerProviders,
) -> bool:
    """Claim and process at most one queued job.

    Returns ``True`` if a job was claimed (whether it went on to succeed or
    fail), ``False`` if the queue had nothing claimable - the caller uses
    that to decide whether to poll again immediately or wait.
    """
    async with sessionmaker() as claim_session:
        claimed = await jobs_service.claim_next_job(claim_session)
        await claim_session.commit()

    if claimed is None:
        return False

    log.info("worker.job_claimed", job_id=str(claimed.id), kind=claimed.kind)

    handler = JOB_HANDLERS.get(claimed.kind)
    if handler is None:
        async with sessionmaker() as session:
            await jobs_repo.update(
                session,
                job_id=claimed.id,
                status="failed",
                finished_at=datetime.now(UTC),
                attempts=claimed.attempts + 1,
                error=f"No worker handler is registered for job kind '{claimed.kind}'.",
            )
            await session.commit()
        log.error("worker.job_unhandled_kind", job_id=str(claimed.id), kind=claimed.kind)
        return True

    async with sessionmaker() as session:
        try:
            result = await handler(session, providers, claimed.id, sessionmaker)
            await session.commit()
            log.info(
                "worker.job_finished",
                job_id=str(claimed.id),
                kind=claimed.kind,
                status=result.status,
            )
        except Exception as exc:  # noqa: BLE001 - last-resort safety net: every existing
            # run_*_job already catches its own business-logic failures and
            # returns a terminal job rather than raising, so reaching this
            # branch means something unexpected happened (e.g. the DB
            # connection dropped mid-call). The job must still end up
            # terminal, never stuck "running" forever - hence the fresh
            # session below, since `session` itself may now be unusable.
            await session.rollback()
            # The full exception (with its stack trace) goes to the log; the job
            # - which its owner can read - gets only a message written for them.
            log.error("worker.job_crashed", job_id=str(claimed.id), kind=claimed.kind)
            async with sessionmaker() as failure_session:
                await jobs_repo.update(
                    failure_session,
                    job_id=claimed.id,
                    status="failed",
                    finished_at=datetime.now(UTC),
                    error=describe_job_failure(exc, job_id=claimed.id, kind=claimed.kind),
                )
                await failure_session.commit()

    return True


async def run_forever(
    sessionmaker: async_sessionmaker[AsyncSession],
    providers: WorkerProviders,
    *,
    shutdown_event: asyncio.Event,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> None:
    """Poll for jobs until ``shutdown_event`` is set.

    Processes at most one job per iteration. When the queue is empty, waits
    up to ``poll_interval_seconds`` for either new work or a shutdown
    signal - whichever comes first - so a shutdown request is honoured
    right away rather than after a full sleep. A job already in progress is
    always allowed to finish (``process_one_job`` isn't cancelled mid-way);
    the loop simply stops claiming new ones once ``shutdown_event`` fires.
    """
    log.info("worker.started", poll_interval_seconds=poll_interval_seconds)
    while not shutdown_event.is_set():
        try:
            claimed_something = await process_one_job(sessionmaker, providers)
        except Exception as exc:  # noqa: BLE001 - the poll loop itself must never die
            log.error("worker.poll_error", error=str(exc))
            claimed_something = False

        if not claimed_something:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(shutdown_event.wait(), timeout=poll_interval_seconds)
    log.info("worker.stopped")
