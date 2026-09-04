from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import raise_conflict_from_integrity_error
from edutoon.core.pagination import Page, paginate_rows
from edutoon.db.tables import jobs


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
    return _from_row(result.mappings().one())


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


async def list_by_project(
    session: AsyncSession, project_id: UUID, *, limit: int = 50, cursor: str | None = None
) -> Page[JobRecord]:
    stmt = select(jobs).where(jobs.c.project_id == project_id)
    rows, next_cursor = await paginate_rows(session, stmt, table=jobs, limit=limit, cursor=cursor)
    return Page(items=[_from_row(row) for row in rows], next_cursor=next_cursor)
