"""``topic_citations`` - which ``source_chunks`` a topic was drawn from
(migration 0009).

The evidence half of Phase 5's topic understanding, and deliberately the
mirror of ``claim_citations``: a topic is *traceable* to the pages it came
from. Two differences follow from topic evidence being derived and
regenerable rather than an audit trail:

- it cascades from ``source_chunks`` (removing material never blocks), and
- it may be cleared and rebuilt wholesale - :func:`delete_by_project` exists
  for an extraction re-run. Rows are still never UPDATEd.

Every write here is project-scoped *in the SQL itself*: a citation row is
only ever created for a topic and a chunk that both belong to the named
project. That is the same security boundary
``source_chunks.search_by_similarity`` draws for retrieval - a topic can never
cite another project's material, whatever ids a caller (or a misbehaving
model) hands in.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import CursorResult, RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import raise_conflict_from_integrity_error
from edutoon.db.tables import project_topics, source_chunks, topic_citations


@dataclass(frozen=True, slots=True)
class TopicCitationRecord:
    id: UUID
    topic_id: UUID
    source_chunk_id: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TopicEvidenceRecord:
    """A citation resolved to where it points: which source, which chunk,
    which pages. What the API layer needs to show "from p. 3-4 of notes.pdf"
    without a second lookup.
    """

    topic_id: UUID
    source_chunk_id: UUID
    source_id: UUID
    chunk_index: int
    page_from: int | None
    page_to: int | None


def _citation_from_row(row: RowMapping) -> TopicCitationRecord:
    return TopicCitationRecord(
        id=row["id"],
        topic_id=row["topic_id"],
        source_chunk_id=row["source_chunk_id"],
        created_at=row["created_at"],
    )


def _evidence_from_row(row: RowMapping) -> TopicEvidenceRecord:
    return TopicEvidenceRecord(
        topic_id=row["topic_id"],
        source_chunk_id=row["source_chunk_id"],
        source_id=row["source_id"],
        chunk_index=row["chunk_index"],
        page_from=row["page_from"],
        page_to=row["page_to"],
    )


async def add_many(
    session: AsyncSession,
    *,
    project_id: UUID,
    topic_id: UUID,
    source_chunk_ids: Sequence[UUID],
) -> list[TopicCitationRecord]:
    """Cite ``source_chunk_ids`` for ``topic_id`` in one round trip.

    Only chunks that belong to ``project_id`` are cited, and only if the
    topic does too; any id that is unknown or belongs to another project is
    silently left out - never an error, and never written. So the returned
    list can be *shorter* than the input: callers that must know every id
    landed compare lengths (the extraction service does, to reject a topic
    whose evidence turned out to be foreign). Duplicate ids in the input
    collapse to one citation.

    Raises :class:`~edutoon.core.errors.ConflictError` if a chunk is already
    cited by this topic (``uq_topic_citations_topic_id_source_chunk_id``).
    """
    unique_ids = list(dict.fromkeys(source_chunk_ids))
    if not unique_ids:
        return []

    candidates = (
        select(project_topics.c.id, source_chunks.c.id)
        .select_from(
            project_topics.join(
                source_chunks, source_chunks.c.project_id == project_topics.c.project_id
            )
        )
        .where(
            project_topics.c.id == topic_id,
            project_topics.c.project_id == project_id,
            source_chunks.c.id.in_(unique_ids),
        )
    )
    stmt = (
        topic_citations.insert()
        .from_select(["topic_id", "source_chunk_id"], candidates)
        .returning(*topic_citations.c)
    )
    try:
        result = await session.execute(stmt)
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)
    return [_citation_from_row(row) for row in result.mappings().all()]


async def list_evidence_for_topics(
    session: AsyncSession, *, project_id: UUID, topic_ids: Sequence[UUID]
) -> dict[UUID, list[TopicEvidenceRecord]]:
    """The resolved evidence for many topics in one query, grouped by topic
    id - the N+1-free way to attach evidence to a whole topic list, in the
    same spirit as ``claim_verifications.list_citations_for``. A topic with
    no evidence is simply absent from the mapping (callers fall back to an
    empty list). Each topic's evidence is in document order (source, then
    chunk). ``project_id`` is a hard filter on the chunk, so nothing from
    another project can appear even if ``topic_ids`` contained a foreign id.
    """
    grouped: dict[UUID, list[TopicEvidenceRecord]] = {}
    if not topic_ids:
        return grouped

    stmt = (
        select(
            topic_citations.c.topic_id,
            source_chunks.c.id.label("source_chunk_id"),
            source_chunks.c.source_id,
            source_chunks.c.chunk_index,
            source_chunks.c.page_from,
            source_chunks.c.page_to,
        )
        .select_from(
            topic_citations.join(
                source_chunks, topic_citations.c.source_chunk_id == source_chunks.c.id
            )
        )
        .where(
            topic_citations.c.topic_id.in_(topic_ids),
            source_chunks.c.project_id == project_id,
        )
        .order_by(source_chunks.c.source_id.asc(), source_chunks.c.chunk_index.asc())
    )
    for row in (await session.execute(stmt)).mappings().all():
        evidence = _evidence_from_row(row)
        grouped.setdefault(evidence.topic_id, []).append(evidence)
    return grouped


async def delete_by_project(session: AsyncSession, project_id: UUID) -> int:
    """Delete the evidence of every topic in ``project_id``. Returns the
    number of rows removed.

    The evidence of *user* topics is not removed by deleting derived topics
    (only their own rows cascade), so an extraction re-run clears it here and
    rebuilds it, keeping a single source of truth for what the latest run
    found.
    """
    stmt = topic_citations.delete().where(
        topic_citations.c.topic_id.in_(
            select(project_topics.c.id).where(project_topics.c.project_id == project_id)
        )
    )
    result = cast(CursorResult[Any], await session.execute(stmt))
    return result.rowcount
