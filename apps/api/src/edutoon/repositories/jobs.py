from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import raise_conflict_from_integrity_error
from edutoon.core.pagination import Page, paginate_rows
from edutoon.db.tables import jobs
from edutoon.repositories import projects as projects_repo

# A job in one of these states has ended and will not run again. Reaching one is a moment the
# owning project's status is recomputed (see ``projects.refresh_status``); moving to ``running``
# is not, because it changes nothing the status looks at.
TERMINAL_STATUSES: tuple[str, ...] = ("succeeded", "failed", "cancelled")


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: UUID
    project_id: UUID
    kind: str
    status: str
    priority: int
    attempts: int
    max_attempts: int
    idempotency_key: str | None
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    scheduled_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _from_row(row: RowMapping) -> JobRecord:
    return JobRecord(
        id=row["id"],
        project_id=row["project_id"],
        kind=row["kind"],
        status=row["status"],
        priority=row["priority"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        idempotency_key=row["idempotency_key"],
        payload=row["payload"],
        result=row["result"],
        error=row["error"],
        scheduled_at=row["scheduled_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def create(
    session: AsyncSession,
    *,
    project_id: UUID,
    kind: str,
    payload: dict[str, Any] | None = None,
    priority: int = 100,
    max_attempts: int = 3,
    idempotency_key: str | None = None,
) -> JobRecord:
    stmt = (
        jobs.insert()
        .values(
            project_id=project_id,
            kind=kind,
            payload=payload or {},
            priority=priority,
            max_attempts=max_attempts,
            idempotency_key=idempotency_key,
        )
        .returning(*jobs.c)
    )
    try:
        result = await session.execute(stmt)
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)
    job = _from_row(result.mappings().one())
    # A new job means work is in flight: the project is now ``processing``.
    await projects_repo.refresh_status(session, project_id=job.project_id)
    return job


async def update(session: AsyncSession, *, job_id: UUID, **fields: Any) -> JobRecord | None:
    """Partial update for status-transition writes (queued -> running ->
    succeeded/failed). Returns ``None`` if the job doesn't exist. An empty
    ``fields`` is a no-op read, mirroring ``projects.update``.

    A write that ends the job (``succeeded`` / ``failed`` / ``cancelled``) also recomputes
    its project's status, in the same transaction. Every handler, the worker and crash
    recovery end jobs through here, so none of them has to remember to.
    """
    if not fields:
        return await get_by_id(session, job_id)

    stmt = jobs.update().where(jobs.c.id == job_id).values(**fields).returning(*jobs.c)
    try:
        result = await session.execute(stmt)
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)
    row = result.mappings().one_or_none()
    if row is None:
        return None
    job = _from_row(row)
    if fields.get("status") in TERMINAL_STATUSES:
        await projects_repo.refresh_status(session, project_id=job.project_id)
    return job


async def requeue_failed(session: AsyncSession, *, job_id: UUID) -> JobRecord | None:
    """Put a ``failed`` job back in the queue for a fresh series of attempts, keeping its
    identity (id, payload, idempotency key) so the work it names is still done at most once.

    A user-initiated retry starts over: ``attempts`` is reset (``ck_jobs_attempts_within_max``
    would otherwise refuse the next run of a job that already used every attempt), and the old
    outcome (``error``, ``result``, timestamps) is cleared. Only a ``failed`` job moves - a
    ``queued`` / ``running`` one is in flight and a ``succeeded`` one is done - so this returns
    ``None`` for any other status, or a missing job, and changes nothing.
    """
    stmt = (
        jobs.update()
        .where(jobs.c.id == job_id, jobs.c.status == "failed")
        .values(
            status="queued",
            attempts=0,
            error=None,
            result=None,
            started_at=None,
            finished_at=None,
            scheduled_at=func.now(),
        )
        .returning(*jobs.c)
    )
    row = (await session.execute(stmt)).mappings().one_or_none()
    if row is None:
        return None
    job = _from_row(row)
    # The job is in flight again, so the project is ``processing`` again.
    await projects_repo.refresh_status(session, project_id=job.project_id)
    return job


async def get_by_id(session: AsyncSession, job_id: UUID) -> JobRecord | None:
    stmt = select(jobs).where(jobs.c.id == job_id)
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def get_by_idempotency_key(
    session: AsyncSession, *, project_id: UUID, kind: str, idempotency_key: str
) -> JobRecord | None:
    stmt = select(jobs).where(
        jobs.c.project_id == project_id,
        jobs.c.kind == kind,
        jobs.c.idempotency_key == idempotency_key,
    )
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def claim_next(session: AsyncSession) -> JobRecord | None:
    """Atomically claim the next due ``queued`` job for a worker, flipping
    it straight to ``running``. Returns ``None`` if no job is claimable.

    The candidate row is picked with ``SELECT ... FOR UPDATE SKIP LOCKED``:
    if two workers call this concurrently, each one's subquery skips any
    row the other has already locked (rather than blocking on it or, worse,
    both resolving to the same row), so the same job can never be claimed
    twice. The lock itself is only held for this single statement - the
    caller commits right away - so it never blocks a concurrent claim
    attempt for longer than one UPDATE.

    Ordered by ``priority`` ascending (a lower number is more urgent) then
    ``scheduled_at`` ascending (oldest first within the same priority); a
    job whose ``scheduled_at`` is still in the future is not yet claimable.
    """
    candidate = (
        select(jobs.c.id)
        .where(jobs.c.status == "queued", jobs.c.scheduled_at <= func.now())
        .order_by(jobs.c.priority.asc(), jobs.c.scheduled_at.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    stmt = (
        jobs.update()
        .where(jobs.c.id == candidate.scalar_subquery())
        .values(status="running", started_at=func.now())
        .returning(*jobs.c)
    )
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def list_by_project(
    session: AsyncSession, project_id: UUID, *, limit: int = 50, cursor: str | None = None
) -> Page[JobRecord]:
    stmt = select(jobs).where(jobs.c.project_id == project_id)
    rows, next_cursor = await paginate_rows(session, stmt, table=jobs, limit=limit, cursor=cursor)
    return Page(items=[_from_row(row) for row in rows], next_cursor=next_cursor)


async def get_latest_by_project_kind(
    session: AsyncSession, *, project_id: UUID, kind: str
) -> JobRecord | None:
    """The most recently created job of ``kind`` for ``project_id``, whatever
    its status - the "what happened last" read a status view wants, distinct
    from :func:`get_active_by_project_kind` (which only ever returns an
    in-flight one).
    """
    stmt = (
        select(jobs)
        .where(jobs.c.project_id == project_id, jobs.c.kind == kind)
        .order_by(jobs.c.created_at.desc(), jobs.c.id.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


# A job in either of these states is *in flight*: it will still run, or is
# running now. ``succeeded`` / ``failed`` / ``cancelled`` are terminal.
ACTIVE_STATUSES: tuple[str, ...] = ("queued", "running")


async def lock_project_kind(session: AsyncSession, *, project_id: UUID, kind: str) -> None:
    """Serialise concurrent work on one ``(project, kind)`` for the rest of
    the current transaction, using a Postgres transaction-level advisory lock.

    "At most one active job of this kind per project" is a check-then-insert:
    two requests can each see no active job and both insert one. Taking this
    lock *before* the check makes the pair atomic - a second caller blocks here
    until the first commits or rolls back, then sees the first's job. The lock
    is released automatically at transaction end, so there is nothing to clean
    up and no schema change.

    Advisory locks are keyed by a 64-bit integer; hashing ``kind:project_id``
    means unrelated projects, and other job kinds, never contend.
    """
    key = f"{kind}:{project_id}"
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))


async def get_active_by_project_kind(
    session: AsyncSession, *, project_id: UUID, kind: str
) -> JobRecord | None:
    """The oldest in-flight (``queued`` or ``running``) job of ``kind`` for
    ``project_id``, if any.
    """
    stmt = (
        select(jobs)
        .where(
            jobs.c.project_id == project_id,
            jobs.c.kind == kind,
            jobs.c.status.in_(ACTIVE_STATUSES),
        )
        .order_by(jobs.c.created_at.asc(), jobs.c.id.asc())
        .limit(1)
    )
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def fail_stale_running(
    session: AsyncSession,
    *,
    project_id: UUID,
    kind: str,
    stale_after: timedelta,
    error: str,
) -> list[JobRecord]:
    """Fail every ``running`` job of ``kind`` for ``project_id`` that a crashed
    worker has left behind, and return them.

    A job qualifies only if **both** hold:

    - it has been ``running`` for longer than ``stale_after`` (measured from
      ``started_at``, which the claim sets from the database clock), and
    - nothing is holding its row. A live worker's handler marks the job
      ``running`` inside the transaction it keeps open until the job finishes, so
      the row stays locked for the whole run - however long a model call takes.
      A crashed worker's connection is closed, its transaction rolled back and
      the lock released. ``FOR UPDATE SKIP LOCKED`` skips a locked row, so a job
      that is genuinely being worked on is never touched, whatever its age.

    Only ``running`` jobs are considered: a ``queued`` one is waiting for a worker
    rather than abandoned by one, and terminal jobs are left alone. The
    ``finished_at`` CHECK needs ``started_at``, so a (never expected) running job
    without one is given it in the same statement.
    """
    started = func.coalesce(jobs.c.started_at, jobs.c.created_at)
    stale = (
        select(jobs.c.id)
        .where(
            jobs.c.project_id == project_id,
            jobs.c.kind == kind,
            jobs.c.status == "running",
            started < func.now() - stale_after,
        )
        .with_for_update(skip_locked=True)
    )
    stmt = (
        jobs.update()
        .where(jobs.c.id.in_(stale))
        .values(
            status="failed",
            started_at=func.coalesce(jobs.c.started_at, func.now()),
            finished_at=func.now(),
            error=error,
        )
        .returning(*jobs.c)
    )
    rows = (await session.execute(stmt)).mappings().all()
    if rows:
        # These jobs just ended (as failures), so the project may no longer be ``processing``.
        await projects_repo.refresh_status(session, project_id=project_id)
    return sorted((_from_row(row) for row in rows), key=lambda job: (job.created_at, str(job.id)))

