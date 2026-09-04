from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import NotFoundError
from edutoon.core.pagination import Page
from edutoon.providers import pdf
from edutoon.providers.storage import Storage
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories.source_chunks import NewSourceChunk
from edutoon.repositories.uploaded_sources import UploadedSourceRecord as UploadedSourceRecord
from edutoon.services import source_chunks as source_chunks_service


async def upload_source(
    session: AsyncSession,
    storage: Storage,
    *,
    project_id: UUID,
    uploader_id: UUID,
    original_filename: str,
    content_type: str,
    content: bytes,
    bucket: str,
    max_pages: int,
) -> UploadedSourceRecord:
    """Store ``content``, record it against ``project_id``, and parse it
    into ``source_chunks`` inline (no job queue - this phase is
    synchronous), one chunk per non-blank page.

    The storage key is deterministic (derived from the content's checksum),
    so a genuine re-upload of identical bytes overwrites the same object
    rather than accumulating duplicates - the ``uploaded_sources`` row for
    it is what actually enforces "already uploaded" (rule 4-adjacent: the
    unique index on ``(project_id, checksum_sha256)``), via
    ``raise_conflict_from_integrity_error`` in the repository.

    A PDF that can't be read at all, or exceeds ``max_pages``, still
    produces a row - with ``status="failed"`` - rather than failing the
    whole request: the upload (storage + traceability record) succeeded
    even though parsing didn't, and a failed source can't feed anything
    downstream.
    """
    checksum = hashlib.sha256(content).hexdigest()
    storage_key = f"projects/{project_id}/{checksum}.pdf"

    await storage.put_object(
        bucket=bucket, key=storage_key, body=content, content_type=content_type
    )

    source = await create_uploaded_source(
        session,
        project_id=project_id,
        original_filename=original_filename,
        storage_bucket=bucket,
        storage_key=storage_key,
        byte_size=len(content),
        checksum_sha256=checksum,
        uploader_id=uploader_id,
        content_type=content_type,
    )

    try:
        pages = pdf.extract_pages(content)
    except pdf.PdfParseError:
        return await update_uploaded_source(
            session, source_id=source.id, project_id=project_id, status="failed"
        )

    if len(pages) > max_pages:
        return await update_uploaded_source(
            session,
            source_id=source.id,
            project_id=project_id,
            status="failed",
            page_count=len(pages),
        )

    chunks = [
        NewSourceChunk(
            source_id=source.id,
            project_id=project_id,
            chunk_index=index,
            content=text.strip(),
            page_from=index + 1,
            page_to=index + 1,
        )
        for index, text in enumerate(pages)
        if text.strip()
    ]
    if chunks:
        await source_chunks_service.create_source_chunks(session, chunks)

    return await update_uploaded_source(
        session,
        source_id=source.id,
        project_id=project_id,
        status="parsed",
        page_count=len(pages),
    )


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


async def get_uploaded_source_for_project(
    session: AsyncSession, *, source_id: UUID, project_id: UUID
) -> UploadedSourceRecord:
    """Ownership-scoped fetch (rule 9): a source from a different project
    looks exactly like one that doesn't exist.
    """
    source = await uploaded_sources_repo.get_by_id_for_project(session, source_id, project_id)
    if source is None:
        raise NotFoundError(f"Uploaded source {source_id} not found.")
    return source


async def update_uploaded_source(
    session: AsyncSession, *, source_id: UUID, project_id: UUID, **fields: object
) -> UploadedSourceRecord:
    source = await uploaded_sources_repo.update(
        session, source_id=source_id, project_id=project_id, **fields
    )
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
