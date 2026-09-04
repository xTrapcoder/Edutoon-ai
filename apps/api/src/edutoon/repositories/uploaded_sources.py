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
from edutoon.db.tables import uploaded_sources


@dataclass(frozen=True, slots=True)
class UploadedSourceRecord:
    id: UUID
    project_id: UUID
    uploader_id: UUID | None
    kind: str
    original_filename: str
    content_type: str
    storage_bucket: str
    storage_key: str
    byte_size: int
    checksum_sha256: str
    page_count: int | None
    status: str
    created_at: datetime
    updated_at: datetime


def _from_row(row: RowMapping) -> UploadedSourceRecord:
    return UploadedSourceRecord(
        id=row["id"],
        project_id=row["project_id"],
        uploader_id=row["uploader_id"],
        kind=row["kind"],
        original_filename=row["original_filename"],
        content_type=row["content_type"],
        storage_bucket=row["storage_bucket"],
        storage_key=row["storage_key"],
        byte_size=row["byte_size"],
        checksum_sha256=row["checksum_sha256"],
        page_count=row["page_count"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def create(
    session: AsyncSession,
    *,
    project_id: UUID,
    original_filename: str,
    storage_bucket: str,
    storage_key: str,
    byte_size: int,
    checksum_sha256: str,
    uploader_id: UUID | None = None,
    content_type: str = "application/pdf",
    page_count: int | None = None,
) -> UploadedSourceRecord:
    stmt = (
        uploaded_sources.insert()
        .values(
            project_id=project_id,
            uploader_id=uploader_id,
            original_filename=original_filename,
            content_type=content_type,
            storage_bucket=storage_bucket,
            storage_key=storage_key,
            byte_size=byte_size,
            checksum_sha256=checksum_sha256,
            page_count=page_count,
        )
        .returning(*uploaded_sources.c)
    )
    try:
        result = await session.execute(stmt)
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)
    return _from_row(result.mappings().one())


async def get_by_id(session: AsyncSession, source_id: UUID) -> UploadedSourceRecord | None:
    stmt = select(uploaded_sources).where(uploaded_sources.c.id == source_id)
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def get_by_id_for_project(
    session: AsyncSession, source_id: UUID, project_id: UUID
) -> UploadedSourceRecord | None:
    """Scoped fetch mirroring ``projects.get_by_id_for_owner`` (rule 9): a
    source that exists but belongs to a different project looks identical
    to one that doesn't exist at all.
    """
    stmt = select(uploaded_sources).where(
        uploaded_sources.c.id == source_id, uploaded_sources.c.project_id == project_id
    )
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def update(
    session: AsyncSession, *, source_id: UUID, project_id: UUID, **fields: Any
) -> UploadedSourceRecord | None:
    """Scoped partial update (e.g. ``status``/``page_count`` after parsing).
    Returns ``None`` if the source doesn't exist or belongs to a different
    project - same rule-9 shape as :func:`get_by_id_for_project`.
    """
    if not fields:
        return await get_by_id_for_project(session, source_id, project_id)

    stmt = (
        uploaded_sources.update()
        .where(uploaded_sources.c.id == source_id, uploaded_sources.c.project_id == project_id)
        .values(**fields)
        .returning(*uploaded_sources.c)
    )
    try:
        result = await session.execute(stmt)
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)
    row = result.mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def get_by_checksum(
    session: AsyncSession, *, project_id: UUID, checksum_sha256: str
) -> UploadedSourceRecord | None:
    stmt = select(uploaded_sources).where(
        uploaded_sources.c.project_id == project_id,
        uploaded_sources.c.checksum_sha256 == checksum_sha256,
    )
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def list_by_project(
    session: AsyncSession, project_id: UUID, *, limit: int = 50, cursor: str | None = None
) -> Page[UploadedSourceRecord]:
    stmt = select(uploaded_sources).where(uploaded_sources.c.project_id == project_id)
    rows, next_cursor = await paginate_rows(
        session, stmt, table=uploaded_sources, limit=limit, cursor=cursor
    )
    return Page(items=[_from_row(row) for row in rows], next_cursor=next_cursor)
