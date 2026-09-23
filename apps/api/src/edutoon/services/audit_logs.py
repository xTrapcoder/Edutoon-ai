from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.pagination import Page
from edutoon.repositories import audit_logs as audit_logs_repo
from edutoon.repositories.audit_logs import AuditLogRecord


async def record_audit_log(
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
    return await audit_logs_repo.create(
        session,
        action=action,
        entity_type=entity_type,
        actor_id=actor_id,
        actor_type=actor_type,
        entity_id=entity_id,
        project_id=project_id,
        request_id=request_id,
        ip_address=ip_address,
        context=context,
    )


async def list_audit_logs_for_entity(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: UUID,
    limit: int = 50,
    cursor: str | None = None,
) -> Page[AuditLogRecord]:
    return await audit_logs_repo.list_by_entity(
        session, entity_type=entity_type, entity_id=entity_id, limit=limit, cursor=cursor
    )


async def list_audit_logs_for_project(
    session: AsyncSession, project_id: UUID, *, limit: int = 50, cursor: str | None = None
) -> Page[AuditLogRecord]:
    return await audit_logs_repo.list_by_project(session, project_id, limit=limit, cursor=cursor)
