from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import NotFoundError
from edutoon.core.pagination import Page
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories.source_chunks import NewSourceChunk as NewSourceChunk
from edutoon.repositories.source_chunks import SourceChunkRecord as SourceChunkRecord


async def create_source_chunks(
    session: AsyncSession, chunks: list[NewSourceChunk]
) -> list[SourceChunkRecord]:
    return await source_chunks_repo.create_many(session, chunks)


async def get_source_chunk_or_404(session: AsyncSession, chunk_id: UUID) -> SourceChunkRecord:
    chunk = await source_chunks_repo.get_by_id(session, chunk_id)
    if chunk is None:
        raise NotFoundError(f"Source chunk {chunk_id} not found.")
    return chunk


async def list_source_chunks(
    session: AsyncSession, source_id: UUID, *, limit: int = 50, cursor: str | None = None
) -> Page[SourceChunkRecord]:
    return await source_chunks_repo.list_by_source(session, source_id, limit=limit, cursor=cursor)


async def list_source_chunks_in_order(
    session: AsyncSession, source_id: UUID
) -> list[SourceChunkRecord]:
    return await source_chunks_repo.list_by_source_in_order(session, source_id)
