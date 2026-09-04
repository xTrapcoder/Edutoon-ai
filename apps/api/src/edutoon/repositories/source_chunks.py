from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import raise_conflict_from_integrity_error
from edutoon.core.pagination import Page, paginate_rows
from edutoon.db.tables import source_chunks


@dataclass(frozen=True, slots=True)
class NewSourceChunk:
    source_id: UUID
    project_id: UUID
    chunk_index: int
    content: str
    page_from: int | None = None
    page_to: int | None = None
    token_count: int | None = None


@dataclass(frozen=True, slots=True)
class SourceChunkRecord:
    id: UUID
    source_id: UUID
    project_id: UUID
    chunk_index: int
    page_from: int | None
    page_to: int | None
    content: str
    token_count: int | None
    embedding_model: str | None
    created_at: datetime
    updated_at: datetime


def _from_row(row: RowMapping) -> SourceChunkRecord:
    return SourceChunkRecord(
        id=row["id"],
        source_id=row["source_id"],
        project_id=row["project_id"],
        chunk_index=row["chunk_index"],
        page_from=row["page_from"],
        page_to=row["page_to"],
        content=row["content"],
        token_count=row["token_count"],
        embedding_model=row["embedding_model"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def create_many(
    session: AsyncSession, chunks: list[NewSourceChunk]
) -> list[SourceChunkRecord]:
    """Bulk-insert chunks (e.g. one PDF's worth) in a single round trip.

    Embeddings are set later, in a dedicated UPDATE, once an embedding
    provider exists — that's business logic this step deliberately excludes.
    """
    if not chunks:
        return []
    stmt = (
        source_chunks.insert()
        .values(
            [
                {
                    "source_id": chunk.source_id,
                    "project_id": chunk.project_id,
                    "chunk_index": chunk.chunk_index,
                    "content": chunk.content,
                    "page_from": chunk.page_from,
                    "page_to": chunk.page_to,
                    "token_count": chunk.token_count,
                }
                for chunk in chunks
            ]
        )
        .returning(*source_chunks.c)
    )
    try:
        result = await session.execute(stmt)
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)
    return [_from_row(row) for row in result.mappings().all()]


async def get_by_id(session: AsyncSession, chunk_id: UUID) -> SourceChunkRecord | None:
    stmt = select(source_chunks).where(source_chunks.c.id == chunk_id)
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def list_by_source(
    session: AsyncSession, source_id: UUID, *, limit: int = 50, cursor: str | None = None
) -> Page[SourceChunkRecord]:
    stmt = select(source_chunks).where(source_chunks.c.source_id == source_id)
    rows, next_cursor = await paginate_rows(
        session, stmt, table=source_chunks, limit=limit, cursor=cursor
    )
    return Page(items=[_from_row(row) for row in rows], next_cursor=next_cursor)
