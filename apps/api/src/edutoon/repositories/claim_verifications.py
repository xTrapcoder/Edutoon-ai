from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import raise_conflict_from_integrity_error
from edutoon.core.pagination import Page, paginate_rows
from edutoon.db.tables import claim_citations, claim_verifications


@dataclass(frozen=True, slots=True)
class NewClaimCitation:
    source_chunk_id: UUID
    similarity: float


@dataclass(frozen=True, slots=True)
class ClaimVerificationRecord:
    id: UUID
    project_id: UUID
    claim_text: str
    status: str
    rationale: str | None
    embedding_model: str
    llm_model: str
    topic_id: UUID | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ClaimCitationRecord:
    id: UUID
    claim_verification_id: UUID
    source_chunk_id: UUID
    similarity: float
    created_at: datetime


def _verification_from_row(row: RowMapping) -> ClaimVerificationRecord:
    return ClaimVerificationRecord(
        id=row["id"],
        project_id=row["project_id"],
        claim_text=row["claim_text"],
        status=row["status"],
        rationale=row["rationale"],
        embedding_model=row["embedding_model"],
        llm_model=row["llm_model"],
        topic_id=row["topic_id"],
        created_at=row["created_at"],
    )


def _citation_from_row(row: RowMapping) -> ClaimCitationRecord:
    return ClaimCitationRecord(
        id=row["id"],
        claim_verification_id=row["claim_verification_id"],
        source_chunk_id=row["source_chunk_id"],
        similarity=row["similarity"],
        created_at=row["created_at"],
    )


async def create(
    session: AsyncSession,
    *,
    project_id: UUID,
    claim_text: str,
    status: str,
    embedding_model: str,
    llm_model: str,
    rationale: str | None = None,
    topic_id: UUID | None = None,
    citations: list[NewClaimCitation] | None = None,
) -> ClaimVerificationRecord:
    """Insert a verdict and (if any) the citations it relied on.

    Append-only - there is deliberately no ``update``: a re-verification of
    the same claim is a new row, not a mutation of an old one, so history is
    preserved (rule-4-adjacent, though this table has real FKs unlike the
    fully loose ``audit_logs``).

    ``topic_id`` is ``None`` for a claim typed in by hand through the manual
    verification route (unchanged default); Phase 6's claim extraction passes
    the topic the candidate was drawn from.
    """
    stmt = (
        claim_verifications.insert()
        .values(
            project_id=project_id,
            claim_text=claim_text,
            status=status,
            rationale=rationale,
            embedding_model=embedding_model,
            llm_model=llm_model,
            topic_id=topic_id,
        )
        .returning(*claim_verifications.c)
    )
    try:
        result = await session.execute(stmt)
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)
    record = _verification_from_row(result.mappings().one())

    if citations:
        await add_citations(session, claim_verification_id=record.id, citations=citations)

    return record


async def add_citations(
    session: AsyncSession, *, claim_verification_id: UUID, citations: list[NewClaimCitation]
) -> list[ClaimCitationRecord]:
    """Bulk-insert citations for an existing verification (e.g. one PDF's
    worth of chunk references) in a single round trip.
    """
    if not citations:
        return []
    stmt = (
        claim_citations.insert()
        .values(
            [
                {
                    "claim_verification_id": claim_verification_id,
                    "source_chunk_id": citation.source_chunk_id,
                    "similarity": citation.similarity,
                }
                for citation in citations
            ]
        )
        .returning(*claim_citations.c)
    )
    try:
        result = await session.execute(stmt)
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)
    return [_citation_from_row(row) for row in result.mappings().all()]


async def get_by_id(
    session: AsyncSession, verification_id: UUID
) -> ClaimVerificationRecord | None:
    stmt = select(claim_verifications).where(claim_verifications.c.id == verification_id)
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _verification_from_row(row) if row is not None else None


async def get_by_id_for_project(
    session: AsyncSession, verification_id: UUID, project_id: UUID
) -> ClaimVerificationRecord | None:
    """Scoped fetch mirroring ``uploaded_sources.get_by_id_for_project``
    (rule 9): a verification that exists but belongs to a different project
    looks identical to one that doesn't exist at all.
    """
    stmt = select(claim_verifications).where(
        claim_verifications.c.id == verification_id,
        claim_verifications.c.project_id == project_id,
    )
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _verification_from_row(row) if row is not None else None


async def list_by_project(
    session: AsyncSession,
    project_id: UUID,
    *,
    topic_id: UUID | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Page[ClaimVerificationRecord]:
    """A project's claim verifications, newest first. ``topic_id`` narrows the
    page to claims extracted for that one topic (Phase 6); omitted, every
    verification of the project is returned, manual and extracted alike.
    """
    stmt = select(claim_verifications).where(claim_verifications.c.project_id == project_id)
    if topic_id is not None:
        stmt = stmt.where(claim_verifications.c.topic_id == topic_id)
    rows, next_cursor = await paginate_rows(
        session, stmt, table=claim_verifications, limit=limit, cursor=cursor
    )
    return Page(items=[_verification_from_row(row) for row in rows], next_cursor=next_cursor)


async def list_claim_texts_by_topic(session: AsyncSession, topic_id: UUID) -> list[str]:
    """Every ``claim_text`` already extracted for ``topic_id``, in no
    particular order - exactly what
    :func:`edutoon.domain.claim_extraction.validate_candidates` needs as
    ``existing_claim_texts`` so a replace/re-run never re-verifies a claim
    this topic already has.
    """
    stmt = select(claim_verifications.c.claim_text).where(
        claim_verifications.c.topic_id == topic_id
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_by_ids(
    session: AsyncSession, *, project_id: UUID, verification_ids: Sequence[UUID]
) -> list[ClaimVerificationRecord]:
    """Resolve a set of verification ids to their records, scoped to
    ``project_id`` - an id from another project is simply absent from the
    result (rule 9), never an error. Order is not guaranteed. Returns ``[]``
    for an empty ``verification_ids``. The N+1-free way to attach claim
    detail to a batch of script segments.
    """
    if not verification_ids:
        return []
    stmt = select(claim_verifications).where(
        claim_verifications.c.project_id == project_id,
        claim_verifications.c.id.in_(verification_ids),
    )
    rows = (await session.execute(stmt)).mappings().all()
    return [_verification_from_row(row) for row in rows]


async def list_supported_by_project(
    session: AsyncSession, project_id: UUID
) -> list[ClaimVerificationRecord]:
    """Every ``status='supported'`` verification for ``project_id``, oldest
    first - the pool a script's segments are allowed to cite.

    Deliberately strict: ``partially_supported`` is *not* included. A script
    segment may only rest on a claim that stood up in full. Unpaginated,
    like ``list_citations`` - the count is bounded by the topics worth
    covering, which the pipeline caps upstream.
    """
    stmt = (
        select(claim_verifications)
        .where(
            claim_verifications.c.project_id == project_id,
            claim_verifications.c.status == "supported",
        )
        .order_by(claim_verifications.c.created_at.asc(), claim_verifications.c.id.asc())
    )
    rows = (await session.execute(stmt)).mappings().all()
    return [_verification_from_row(row) for row in rows]


async def list_supported_by_topic(
    session: AsyncSession, *, project_id: UUID, topic_id: UUID
) -> list[ClaimVerificationRecord]:
    """Every ``status='supported'`` verification extracted for ``topic_id``,
    oldest first - the pool one Phase 7 section is allowed to cite. Project-
    scoped defensively (rule 9), though a topic id is already only ever
    resolved from within one project by every caller.

    Deliberately strict, like :func:`list_supported_by_project`:
    ``partially_supported`` is not included, and a hand-typed claim (whose
    ``topic_id`` is always ``NULL``) never appears here - it belongs to no
    topic's section.
    """
    stmt = (
        select(claim_verifications)
        .where(
            claim_verifications.c.project_id == project_id,
            claim_verifications.c.topic_id == topic_id,
            claim_verifications.c.status == "supported",
        )
        .order_by(claim_verifications.c.created_at.asc(), claim_verifications.c.id.asc())
    )
    rows = (await session.execute(stmt)).mappings().all()
    return [_verification_from_row(row) for row in rows]


async def list_citations(
    session: AsyncSession, claim_verification_id: UUID
) -> list[ClaimCitationRecord]:
    """All citations for one verification - unpaginated, since the count is
    small and bounded by the retrieval top-K, same reasoning already applied
    to ``source_chunks.list_by_source_in_order``.
    """
    stmt = select(claim_citations).where(
        claim_citations.c.claim_verification_id == claim_verification_id
    )
    rows = (await session.execute(stmt)).mappings().all()
    return [_citation_from_row(row) for row in rows]


async def list_citations_for(
    session: AsyncSession, claim_verification_ids: Sequence[UUID]
) -> dict[UUID, list[ClaimCitationRecord]]:
    """Citations for many verifications in one query, grouped by
    verification id - the list endpoint's N+1-free counterpart to
    :func:`list_citations`. Verifications with no citations are simply
    absent from the mapping (callers fall back to an empty list).
    """
    grouped: dict[UUID, list[ClaimCitationRecord]] = {}
    if not claim_verification_ids:
        return grouped
    stmt = select(claim_citations).where(
        claim_citations.c.claim_verification_id.in_(claim_verification_ids)
    )
    for row in (await session.execute(stmt)).mappings().all():
        citation = _citation_from_row(row)
        grouped.setdefault(citation.claim_verification_id, []).append(citation)
    return grouped
