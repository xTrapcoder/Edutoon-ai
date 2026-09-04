from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.pagination import Page, paginate_rows
from edutoon.db.tables import audit_logs


@dataclass(frozen=True, slots=True)
class AuditLogRecord:
    id: UUID
    actor_id: UUID | None
    actor_type: str
    action: str
    entity_type: str
    entity_id: UUID | None
    project_id: UUID | None
    request_id: str | None
    ip_address: str | None
    context: dict[str, Any]
    created_at: datetime


def _from_row(row: RowMapping) -> AuditLogRecord:
    return AuditLogRecord(
        id=row["id"],
        actor_id=row["actor_id"],
        actor_type=row["actor_type"],
        action=row["action"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        project_id=row["project_id"],
        request_id=row["request_id"],
        ip_address=row["ip_address"],
        context=row["context"],
        created_at=row["created_at"],
    )


async def create(
    session: AsyncSession,
    *,
    action: str,
    entity_type: str,
    actor_id: UUID | None = None,
    actor_type: str = "user",
    entity_id: UUID | None = None,
    project_id: UUID | None = None,
    request_id: str | None = None,
    ip_address: str | None = None,
    context: dict[str, Any] | None = None,
) -> AuditLogRecord:
    """INSERT-only: the table's ``trg_audit_logs_append_only`` trigger rejects
    UPDATE/DELETE (rule 4), and no function here attempts either.
    """
    stmt = (
        audit_logs.insert()
        .values(
            action=action,
            entity_type=entity_type,
            actor_id=actor_id,
            actor_type=actor_type,
            entity_id=entity_id,
            project_id=project_id,
            request_id=request_id,
            ip_address=ip_address,
            context=context or {},
        )
        .returning(*audit_logs.c)
    )
    result = await session.execute(stmt)
    return _from_row(result.mappings().one())


async def list_by_entity(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: UUID,
    limit: int = 50,
    cursor: str | None = None,
) -> Page[AuditLogRecord]:
    stmt = select(audit_logs).where(
        audit_logs.c.entity_type == entity_type, audit_logs.c.entity_id == entity_id
    )
    rows, next_cursor = await paginate_rows(
        session, stmt, table=audit_logs, limit=limit, cursor=cursor
    )
    return Page(items=[_from_row(row) for row in rows], next_cursor=next_cursor)


async def list_by_project(
    session: AsyncSession, project_id: UUID, *, limit: int = 50, cursor: str | None = None
) -> Page[AuditLogRecord]:
    stmt = select(audit_logs).where(audit_logs.c.project_id == project_id)
    rows, next_cursor = await paginate_rows(
        session, stmt, table=audit_logs, limit=limit, cursor=cursor
    )
    return Page(items=[_from_row(row) for row in rows], next_cursor=next_cursor)
