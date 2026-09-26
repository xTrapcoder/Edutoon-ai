from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, literal, select, union_all
from sqlalchemy.engine import CursorResult, RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import raise_conflict_from_integrity_error
from edutoon.core.pagination import Page, paginate_rows
from edutoon.db.tables import jobs, projects, uploaded_sources
from edutoon.domain.generation_settings import (
    DEFAULT_LANGUAGE,
    DEFAULT_ORIENTATION,
    DEFAULT_TARGET_DURATION_MS,
    Language,
    Orientation,
)
from edutoon.domain.project_status import Outcome, ProjectStatus, derive_project_status

# A job in either of these states is still in flight (same meaning as
# ``repositories.jobs.ACTIVE_STATUSES``, which imports this module and so cannot be imported here).
_IN_FLIGHT_JOB_STATUSES = ("queued", "running")


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    id: UUID
    owner_id: UUID
    title: str
    description: str | None
    source_type: str
    status: str
    language: Language
    orientation: Orientation
    target_duration_ms: int
    created_at: datetime
    updated_at: datetime


def _from_row(row: RowMapping) -> ProjectRecord:
    return ProjectRecord(
        id=row["id"],
        owner_id=row["owner_id"],
        title=row["title"],
        description=row["description"],
        source_type=row["source_type"],
        status=row["status"],
        language=row["language"],
        orientation=row["orientation"],
        target_duration_ms=row["target_duration_ms"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def create(
    session: AsyncSession,
    *,
    owner_id: UUID,
    title: str,
    source_type: str,
    description: str | None = None,
    language: Language = DEFAULT_LANGUAGE,
    orientation: Orientation = DEFAULT_ORIENTATION,
    target_duration_ms: int = DEFAULT_TARGET_DURATION_MS,
) -> ProjectRecord:
    stmt = (
        projects.insert()
        .values(
            owner_id=owner_id,
            title=title,
            source_type=source_type,
            description=description,
            language=language,
            orientation=orientation,
            target_duration_ms=target_duration_ms,
        )
        .returning(*projects.c)
    )
    result = await session.execute(stmt)
    return _from_row(result.mappings().one())


async def get_by_id(session: AsyncSession, project_id: UUID) -> ProjectRecord | None:
    stmt = select(projects).where(projects.c.id == project_id)
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def get_by_id_for_owner(
    session: AsyncSession, project_id: UUID, owner_id: UUID
) -> ProjectRecord | None:
    """Ownership-scoped fetch (rule 9): missing and not-owned look identical
    to the caller - both return ``None``, never a separate "forbidden" case.
    """
    stmt = select(projects).where(projects.c.id == project_id, projects.c.owner_id == owner_id)
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def update(
    session: AsyncSession, *, project_id: UUID, owner_id: UUID, **fields: Any
) -> ProjectRecord | None:
    """Ownership-scoped partial update. Returns ``None`` if the project does
    not exist or is not owned by ``owner_id`` - same rule-9 shape as
    :func:`get_by_id_for_owner`. An empty ``fields`` is a no-op read.
    """
    if not fields:
        return await get_by_id_for_owner(session, project_id, owner_id)

    stmt = (
        projects.update()
        .where(projects.c.id == project_id, projects.c.owner_id == owner_id)
        .values(**fields)
        .returning(*projects.c)
    )
    try:
        result = await session.execute(stmt)
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)
    row = result.mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def delete(session: AsyncSession, *, project_id: UUID, owner_id: UUID) -> bool:
    """Ownership-scoped delete. Returns ``True`` iff a row was removed."""
    stmt = projects.delete().where(projects.c.id == project_id, projects.c.owner_id == owner_id)
    result = cast(CursorResult[Any], await session.execute(stmt))
    return result.rowcount > 0


async def list_by_owner(
    session: AsyncSession, owner_id: UUID, *, limit: int = 50, cursor: str | None = None
) -> Page[ProjectRecord]:
    stmt = select(projects).where(projects.c.owner_id == owner_id)
    rows, next_cursor = await paginate_rows(
        session, stmt, table=projects, limit=limit, cursor=cursor
    )
    return Page(items=[_from_row(row) for row in rows], next_cursor=next_cursor)


async def refresh_status(session: AsyncSession, *, project_id: UUID) -> str | None:
    """Recompute ``project_id``'s status from its pipeline and store it if it changed.

    The rule is :func:`edutoon.domain.project_status.derive_project_status`; this reads its
    two inputs and applies the answer. Returns the project's status afterwards, or ``None``
    if there is no such project (for example, it was deleted while its last job ran).

    Called from ``repositories.jobs`` and ``repositories.uploaded_sources`` at the moments the
    pipeline's state changes - a job is queued or ends, a PDF fails to parse - so the status
    moves in the same transaction as the change that caused it, on every path (an API
    request, the worker, crash recovery) and for every job kind, with no call site to forget.

    The project row is locked (``FOR NO KEY UPDATE``) for the read-decide-write, so two
    transactions refreshing at once take turns: whichever runs second sees everything the
    first committed and decides on that. It is a lock that does not block anything that only
    references the project (inserting a job, a topic, a source), and callers only reach here
    at the *end* of a piece of work - never while a model call is in progress - so it is held
    for moments.
    """
    row = (
        await session.execute(
            select(projects.c.status)
            .where(projects.c.id == project_id)
            .with_for_update(key_share=True)
        )
    ).one_or_none()
    if row is None:
        return None
    current: str = row[0]

    active_jobs = (
        await session.execute(
            select(func.count())
            .select_from(jobs)
            .where(jobs.c.project_id == project_id, jobs.c.status.in_(_IN_FLIGHT_JOB_STATUSES))
        )
    ).scalar_one()

    # Everything that has finished, as (how it ended, when). A ``cancelled`` job is not an
    # outcome. An uploaded PDF that could not be read is one, and it has no job of its own.
    finished = union_all(
        select(
            jobs.c.status.label("outcome"),
            func.coalesce(jobs.c.finished_at, jobs.c.updated_at).label("at"),
        ).where(jobs.c.project_id == project_id, jobs.c.status.in_(("succeeded", "failed"))),
        select(
            literal("failed").label("outcome"),
            uploaded_sources.c.updated_at.label("at"),
        ).where(uploaded_sources.c.project_id == project_id, uploaded_sources.c.status == "failed"),
    ).subquery()
    # On an exact tie the failure wins: better to say "failed" once too often than to hide it.
    latest: Outcome | None = (
        await session.execute(
            select(finished.c.outcome)
            .order_by(finished.c.at.desc(), finished.c.outcome.asc())
            .limit(1)
        )
    ).scalar_one_or_none()

    derived: ProjectStatus = derive_project_status(
        current=current,  # type: ignore[arg-type]  # a CHECK constraint keeps it to ProjectStatus
        active_jobs=active_jobs,
        latest_outcome=latest,
    )
    if derived != current:
        # ``set_updated_at`` bumps ``updated_at`` with the change, which is right: the
        # project's state really did just change. An unchanged status writes nothing.
        await session.execute(
            projects.update().where(projects.c.id == project_id).values(status=derived)
        )
    return derived
