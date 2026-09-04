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

    Embeddings are set later, via :func:`update_embedding`, once the
    Evidence Engine's indexing step runs against the source.
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


async def update_embedding(
    session: AsyncSession, *, chunk_id: UUID, embedding: list[float], embedding_model: str
) -> SourceChunkRecord | None:
    """Set a chunk's embedding vector. Always writes both columns together -
    ``ck_source_chunks_embedding_has_model`` requires ``embedding_model`` to
    be set whenever ``embedding`` is, so there is no partial-write path here.
    Returns ``None`` if the chunk doesn't exist.
    """
    stmt = (
        source_chunks.update()
        .where(source_chunks.c.id == chunk_id)
        .values(embedding=embedding, embedding_model=embedding_model)
        .returning(*source_chunks.c)
    )
    result = await session.execute(stmt)
    row = result.mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def list_missing_embedding_by_source(
    session: AsyncSession, source_id: UUID
) -> list[SourceChunkRecord]:
    """A source's chunks that don't have an embedding yet, in document
    order. Indexing calls this rather than fetching every chunk, so a
    re-run after a partial failure only touches what's still missing.
    """
    stmt = (
        select(source_chunks)
        .where(source_chunks.c.source_id == source_id, source_chunks.c.embedding.is_(None))
        .order_by(source_chunks.c.chunk_index.asc())
    )
    rows = (await session.execute(stmt)).mappings().all()
    return [_from_row(row) for row in rows]


async def list_by_source_in_order(
    session: AsyncSession, source_id: UUID
) -> list[SourceChunkRecord]:
    """All of a source's chunks, in document order (``chunk_index`` ASC).

    Unlike :func:`list_by_source`, this deliberately isn't keyset-paginated
    by ``(created_at, id)`` - that ordering suits "most recent first" lists
    (projects, audit logs), not a single document's reading order, and a
    bulk insert can even give several chunks the same ``created_at``,
    making that order unstable. A source's chunk count is bounded by
    ``MAX_PDF_PAGES`` (a few hundred at most), so returning them all
    unpaginated is safe.
    """
    stmt = (
        select(source_chunks)
        .where(source_chunks.c.source_id == source_id)
        .order_by(source_chunks.c.chunk_index.asc())
    )
    rows = (await session.execute(stmt)).mappings().all()
    return [_from_row(row) for row in rows]
