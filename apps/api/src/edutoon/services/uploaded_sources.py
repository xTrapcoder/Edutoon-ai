from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import NotFoundError
from edutoon.core.pagination import Page
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories.uploaded_sources import UploadedSourceRecord


async def create_uploaded_source(
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
    return await uploaded_sources_repo.create(
        session,
        project_id=project_id,
        original_filename=original_filename,
        storage_bucket=storage_bucket,
        storage_key=storage_key,
        byte_size=byte_size,
        checksum_sha256=checksum_sha256,
        uploader_id=uploader_id,
        content_type=content_type,
        page_count=page_count,
    )


async def get_uploaded_source_or_404(
    session: AsyncSession, source_id: UUID
) -> UploadedSourceRecord:
    source = await uploaded_sources_repo.get_by_id(session, source_id)
    if source is None:
        raise NotFoundError(f"Uploaded source {source_id} not found.")
    return source


async def get_uploaded_source_by_checksum(
    session: AsyncSession, *, project_id: UUID, checksum_sha256: str
) -> UploadedSourceRecord | None:
    return await uploaded_sources_repo.get_by_checksum(
        session, project_id=project_id, checksum_sha256=checksum_sha256
    )


async def list_uploaded_sources(
    session: AsyncSession, project_id: UUID, *, limit: int = 50, cursor: str | None = None
) -> Page[UploadedSourceRecord]:
    return await uploaded_sources_repo.list_by_project(
        session, project_id, limit=limit, cursor=cursor
    )
