"""``scripts`` / ``script_segments`` / ``script_segment_claims`` - Script
Generation Engine, SG-1 (repository + migration only; no service or API
layer yet).

Persistence shape and the invariants the DB enforces:

- ``scripts`` is a status machine. :func:`set_status` is the *only* update
  path; :func:`supersede_active` is a bulk status write used to keep the
  ``uq_scripts_project_id_active`` partial-unique invariant (at most one
  ``pending`` / ``draft`` / ``approved`` script per project) satisfiable
  before inserting a fresh one.
- ``script_segments`` / ``script_segment_claims`` are append-only, exactly
  like ``claim_citations``: this module never issues UPDATE or DELETE on
  them. A regenerated script is a new ``scripts`` row.
- Every ``script_segment_claims`` row points at a real
  ``claim_verifications`` row (FK ``ON DELETE RESTRICT``), so a script's
  traceability back to the Evidence Engine cannot silently disappear.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import CursorResult, RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import raise_conflict_from_integrity_error
from edutoon.core.pagination import Page, paginate_rows
from edutoon.db.tables import script_segment_claims, script_segments, scripts

ACTIVE_STATUSES: tuple[str, ...] = ("pending", "draft", "approved")


@dataclass(frozen=True, slots=True)
class NewScriptSegment:
    segment_index: int
    narration_text: str
    estimated_duration_ms: int
    topic_id: UUID | None = None
    claim_verification_ids: tuple[UUID, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ScriptRecord:
    id: UUID
    project_id: UUID
    status: str
    llm_model: str
    estimated_duration_ms: int | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ScriptSegmentRecord:
    id: UUID
    script_id: UUID
    segment_index: int
    narration_text: str
    estimated_duration_ms: int
    topic_id: UUID | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ScriptSegmentClaimRecord:
    id: UUID
    script_segment_id: UUID
    claim_verification_id: UUID
    created_at: datetime


def _script_from_row(row: RowMapping) -> ScriptRecord:
    return ScriptRecord(
        id=row["id"],
        project_id=row["project_id"],
        status=row["status"],
        llm_model=row["llm_model"],
        estimated_duration_ms=row["estimated_duration_ms"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _segment_from_row(row: RowMapping) -> ScriptSegmentRecord:
    return ScriptSegmentRecord(
        id=row["id"],
        script_id=row["script_id"],
        segment_index=row["segment_index"],
        narration_text=row["narration_text"],
        estimated_duration_ms=row["estimated_duration_ms"],
        topic_id=row["topic_id"],
        created_at=row["created_at"],
    )


def _segment_claim_from_row(row: RowMapping) -> ScriptSegmentClaimRecord:
    return ScriptSegmentClaimRecord(
        id=row["id"],
        script_segment_id=row["script_segment_id"],
        claim_verification_id=row["claim_verification_id"],
        created_at=row["created_at"],
    )


async def create(
    session: AsyncSession,
    *,
    project_id: UUID,
    llm_model: str,
    estimated_duration_ms: int | None = None,
    segments: Sequence[NewScriptSegment] | None = None,
) -> ScriptRecord:
    """Insert a ``scripts`` row (always ``status='pending'`` - use
    :func:`set_status` to advance it) and, if given, its segments together
    with each segment's claim citations.

    Raises :class:`~edutoon.core.errors.ConflictError` if the project
    already has an active script (``uq_scripts_project_id_active``); callers
    that intend to replace the previous one call :func:`supersede_active`
    first, in the same transaction.
    """
    stmt = (
        scripts.insert()
        .values(
            project_id=project_id,
            llm_model=llm_model,
            estimated_duration_ms=estimated_duration_ms,
        )
        .returning(*scripts.c)
    )
    try:
        result = await session.execute(stmt)
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)
    record = _script_from_row(result.mappings().one())

    if segments:
        await add_segments(session, script_id=record.id, segments=segments)

    return record


async def add_segments(
    session: AsyncSession, *, script_id: UUID, segments: Sequence[NewScriptSegment]
) -> list[ScriptSegmentRecord]:
    """Bulk-insert a script's segments and their claim citations in one
    round trip each. Append-only: there is no update/delete counterpart.
    """
    if not segments:
        return []

    stmt = (
        script_segments.insert()
        .values(
            [
                {
                    "script_id": script_id,
                    "segment_index": segment.segment_index,
                    "narration_text": segment.narration_text,
                    "estimated_duration_ms": segment.estimated_duration_ms,
                    "topic_id": segment.topic_id,
                }
                for segment in segments
            ]
        )
        .returning(*script_segments.c)
    )
    try:
        result = await session.execute(stmt)
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)
    rows = [_segment_from_row(row) for row in result.mappings().all()]

    # RETURNING order is not guaranteed for a multi-row insert, so map the
    # generated ids back to their input by segment_index (unique per script).
    id_by_index = {row.segment_index: row.id for row in rows}
    links = [
        {
            "script_segment_id": id_by_index[segment.segment_index],
            "claim_verification_id": claim_verification_id,
        }
        for segment in segments
        for claim_verification_id in segment.claim_verification_ids
    ]
    if links:
        try:
            await session.execute(script_segment_claims.insert().values(links))
        except IntegrityError as exc:
            raise_conflict_from_integrity_error(exc)

    return sorted(rows, key=lambda row: row.segment_index)


async def get_by_id(session: AsyncSession, script_id: UUID) -> ScriptRecord | None:
    stmt = select(scripts).where(scripts.c.id == script_id)
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _script_from_row(row) if row is not None else None


async def get_by_id_for_project(
    session: AsyncSession, script_id: UUID, project_id: UUID
) -> ScriptRecord | None:
    """Ownership-scoped fetch (rule 9): a script that exists but belongs to
    a different project looks identical to one that doesn't exist.
    """
    stmt = select(scripts).where(
        scripts.c.id == script_id, scripts.c.project_id == project_id
    )
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _script_from_row(row) if row is not None else None


async def get_active(session: AsyncSession, project_id: UUID) -> ScriptRecord | None:
    """The project's current ``pending`` / ``draft`` / ``approved`` script,
    if any - there is at most one (``uq_scripts_project_id_active``).
    """
    stmt = select(scripts).where(
        scripts.c.project_id == project_id, scripts.c.status.in_(ACTIVE_STATUSES)
    )
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _script_from_row(row) if row is not None else None


async def list_by_project(
    session: AsyncSession, project_id: UUID, *, limit: int = 50, cursor: str | None = None
) -> Page[ScriptRecord]:
    stmt = select(scripts).where(scripts.c.project_id == project_id)
    rows, next_cursor = await paginate_rows(
        session, stmt, table=scripts, limit=limit, cursor=cursor
    )
    return Page(items=[_script_from_row(row) for row in rows], next_cursor=next_cursor)


async def list_segments(
    session: AsyncSession, script_id: UUID
) -> list[ScriptSegmentRecord]:
    """All of a script's segments in narration order - unpaginated, since
    the count is small and bounded, same reasoning as
    ``claim_verifications.list_citations``.
    """
    stmt = (
        select(script_segments)
        .where(script_segments.c.script_id == script_id)
        .order_by(script_segments.c.segment_index.asc())
    )
    rows = (await session.execute(stmt)).mappings().all()
    return [_segment_from_row(row) for row in rows]


async def list_segment_claims(
    session: AsyncSession, script_segment_id: UUID
) -> list[ScriptSegmentClaimRecord]:
    stmt = select(script_segment_claims).where(
        script_segment_claims.c.script_segment_id == script_segment_id
    )
    rows = (await session.execute(stmt)).mappings().all()
    return [_segment_claim_from_row(row) for row in rows]


async def set_status(
    session: AsyncSession,
    *,
    script_id: UUID,
    status: str,
    estimated_duration_ms: int | None = None,
) -> ScriptRecord | None:
    """The only update path on ``scripts`` - a status-machine transition
    (``pending -> draft -> approved`` / ``failed`` / ``superseded``).
    ``updated_at`` is stamped by the ``trg_scripts_set_updated_at`` trigger.
    Returns ``None`` if the script doesn't exist.
    """
    values: dict[str, Any] = {"status": status}
    if estimated_duration_ms is not None:
        values["estimated_duration_ms"] = estimated_duration_ms

    stmt = (
        scripts.update()
        .where(scripts.c.id == script_id)
        .values(**values)
        .returning(*scripts.c)
    )
    try:
        result = await session.execute(stmt)
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)
    row = result.mappings().one_or_none()
    return _script_from_row(row) if row is not None else None


async def supersede_active(
    session: AsyncSession, *, project_id: UUID, except_id: UUID | None = None
) -> int:
    """Mark every active (``pending`` / ``draft`` / ``approved``) script for
    ``project_id`` as ``superseded``, optionally sparing ``except_id``.
    Returns the number of rows changed. Callers run this immediately before
    inserting a new script so ``uq_scripts_project_id_active`` stays
    satisfiable.
    """
    where = [
        scripts.c.project_id == project_id,
        scripts.c.status.in_(ACTIVE_STATUSES),
    ]
    if except_id is not None:
        where.append(scripts.c.id != except_id)

    stmt = scripts.update().where(*where).values(status="superseded")
    result = cast(CursorResult[Any], await session.execute(stmt))
    return result.rowcount
