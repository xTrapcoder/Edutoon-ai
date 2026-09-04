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
    citations: list[NewClaimCitation] | None = None,
) -> ClaimVerificationRecord:
    """Insert a verdict and (if any) the citations it relied on.

    Append-only - there is deliberately no ``update``: a re-verification of
    the same claim is a new row, not a mutation of an old one, so history is
    preserved (rule-4-adjacent, though this table has real FKs unlike the
    fully loose ``audit_logs``).
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


async def list_by_project(
    session: AsyncSession, project_id: UUID, *, limit: int = 50, cursor: str | None = None
) -> Page[ClaimVerificationRecord]:
    stmt = select(claim_verifications).where(claim_verifications.c.project_id == project_id)
    rows, next_cursor = await paginate_rows(
        session, stmt, table=claim_verifications, limit=limit, cursor=cursor
    )
    return Page(items=[_verification_from_row(row) for row in rows], next_cursor=next_cursor)


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
