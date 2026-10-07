"""``scripts`` / ``script_segments`` / ``script_segment_claims`` - Script
Generation Engine persistence (SG-1 shape, SG-3 write path; Phase 7 changed
when :func:`supersede_active` runs - see below).

Persistence shape and the invariants the DB enforces:

- ``scripts`` is a status machine. :func:`set_status` is the *only* update
  path; :func:`supersede_active` is a bulk status write used to keep
  ``uq_scripts_project_id_active`` satisfiable before a script becomes
  ``draft`` or ``approved``.
- ``script_segments`` / ``script_segment_claims`` are append-only, exactly
  like ``claim_citations``: this module never issues UPDATE or DELETE on
  them. A regenerated script is a new ``scripts`` row.
- Every ``script_segment_claims`` row points at a real
  ``claim_verifications`` row, so a script's traceability back to the
  Evidence Engine is a database guarantee. The FK is ``ON DELETE CASCADE``
  since migration 0018: no code path deletes a single claim, and deleting a
  whole project must be able to remove its claims and the links to them.

**Phase 7 atomicity (migration 0013).** ``uq_scripts_project_id_active`` used
to cover ``pending`` / ``draft`` / ``approved``, so queuing a new generation
had to supersede the old ``approved`` script immediately, before the new one
existed or anything was validated - a failed generation then left the
project with nothing approved at all. The index now covers only ``draft`` /
``approved`` (two pieces of actual content still can never coexist); a fresh
``pending`` placeholder can coexist with the still-``approved`` script for as
long as generation takes. :func:`attach_draft` is therefore the one place
:func:`supersede_active` is called for a generation - inside the same
``SAVEPOINT`` that writes the new script's segments and flips it to
``draft`` - so the previous script is touched only in the same atomic step
that proves the new one is ready to replace it, never before.

Every function that names a script is project-scoped: a script belonging to
another project is invisible, exactly as if it did not exist (rule 9).
:func:`create_script` is the SG-3 entry point - it persists a validated
draft (supersede + script + segments + claim links) as one atomic unit, for
a caller with no pre-created pending row to fill.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import case, select
from sqlalchemy.engine import CursorResult, RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import raise_conflict_from_integrity_error
from edutoon.core.pagination import Page, paginate_rows
from edutoon.db.tables import script_segment_claims, script_segments, scripts
from edutoon.domain.script_provenance import ValidatedScriptDraft

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
    measured_duration_ms: int | None
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
        measured_duration_ms=row["measured_duration_ms"],
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


async def create_script(
    session: AsyncSession,
    *,
    project_id: UUID,
    llm_model: str,
    draft: ValidatedScriptDraft,
    supersede: bool = True,
) -> ScriptRecord:
    """Persist a validated script as one atomic unit.

    Inside a single SAVEPOINT: (optionally) supersede the project's current
    active script, insert the new ``scripts`` row, then its segments and
    their claim-verification links. Any failure - a FK violation, the
    ``uq_scripts_project_id_active`` unique index - rolls the whole tree
    back, including the supersede, so the project is left exactly as it was.

    ``draft`` comes straight from
    :func:`edutoon.domain.script_provenance.filter_and_validate`; segment
    positions and per-segment durations are taken from it verbatim. The
    script is created ``status='pending'`` - advance it with
    :func:`set_status`.
    """
    new_segments = [
        NewScriptSegment(
            segment_index=segment.segment_index,
            narration_text=segment.narration_text,
            estimated_duration_ms=segment.estimated_duration_ms,
            topic_id=segment.topic_id,
            claim_verification_ids=segment.claim_verification_ids,
        )
        for segment in draft.segments
    ]
    async with session.begin_nested():
        if supersede:
            await supersede_active(session, project_id=project_id)
        return await create(
            session,
            project_id=project_id,
            llm_model=llm_model,
            estimated_duration_ms=draft.estimated_duration_ms,
            segments=new_segments,
        )


async def attach_draft(
    session: AsyncSession,
    *,
    project_id: UUID,
    script_id: UUID,
    draft: ValidatedScriptDraft,
    llm_model: str | None = None,
) -> ScriptRecord | None:
    """Fill an existing *pending* script with a validated draft - its
    segments and their claim-verification links - and advance it to
    ``draft``, all inside one SAVEPOINT. ``llm_model``, when given, is the model
    that actually wrote the draft and replaces the one the script was queued with.

    This is the job-flow counterpart to :func:`create_script`: the pending
    ``scripts`` row was created up front by ``enqueue_script_generation`` (so
    the caller gets an id immediately), and the background job fills it in
    here. Returns ``None`` if the script does not exist, belongs to another
    project, or is no longer ``pending`` (rule 9 for the first two). Any
    failure rolls the whole attachment back and leaves the script
    ``pending``.

    Supersedes the project's other ``draft`` / ``approved`` script, if any, in
    this same SAVEPOINT (Phase 7, migration 0013) - the one and only moment a
    previously valid script is touched, and only once this one is already
    proven ready to replace it.
    """
    new_segments = [
        NewScriptSegment(
            segment_index=segment.segment_index,
            narration_text=segment.narration_text,
            estimated_duration_ms=segment.estimated_duration_ms,
            topic_id=segment.topic_id,
            claim_verification_ids=segment.claim_verification_ids,
        )
        for segment in draft.segments
    ]
    async with session.begin_nested():
        script = await get_by_id_for_project(session, script_id, project_id)
        if script is None or script.status != "pending":
            return None
        await supersede_active(session, project_id=project_id, except_id=script_id)
        await add_segments(session, script_id=script_id, segments=new_segments)
        return await set_status(
            session,
            project_id=project_id,
            script_id=script_id,
            status="draft",
            estimated_duration_ms=draft.estimated_duration_ms,
            llm_model=llm_model,
        )


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
    """The project's current script: its ``draft`` / ``approved`` one (there
    is at most one - ``uq_scripts_project_id_active``) if it has one, else
    its newest ``pending`` placeholder, if any.

    Phase 7 (migration 0013): a ``pending`` generation can now coexist with a
    still-``approved`` script while it runs, so unlike ``draft`` / ``approved``
    there can transiently be more than one ``pending`` row (e.g. two
    generations queued back to back) - ordering by status then recency, and
    taking the first, is what keeps this a single, well-defined answer instead
    of an ambiguous multi-row result.
    """
    stmt = (
        select(scripts)
        .where(scripts.c.project_id == project_id, scripts.c.status.in_(ACTIVE_STATUSES))
        .order_by(
            case((scripts.c.status.in_(("draft", "approved")), 0), else_=1),
            scripts.c.created_at.desc(),
        )
        .limit(1)
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
    session: AsyncSession, *, project_id: UUID, script_id: UUID
) -> list[ScriptSegmentRecord]:
    """All of a script's segments in narration order - unpaginated, since
    the count is small and bounded, same reasoning as
    ``claim_verifications.list_citations``. Project-scoped: a script from
    another project yields ``[]`` (rule 9), never rows.
    """
    stmt = (
        select(script_segments)
        .join(scripts, script_segments.c.script_id == scripts.c.id)
        .where(
            script_segments.c.script_id == script_id,
            scripts.c.project_id == project_id,
        )
        .order_by(script_segments.c.segment_index.asc())
    )
    rows = (await session.execute(stmt)).mappings().all()
    return [_segment_from_row(row) for row in rows]


async def list_segment_claims(
    session: AsyncSession, *, project_id: UUID, script_segment_id: UUID
) -> list[ScriptSegmentClaimRecord]:
    """The claim-verification links for one segment. Project-scoped through
    ``script_segments -> scripts``: a segment in another project's script
    yields ``[]`` (rule 9).
    """
    stmt = (
        select(script_segment_claims)
        .join(
            script_segments,
            script_segment_claims.c.script_segment_id == script_segments.c.id,
        )
        .join(scripts, script_segments.c.script_id == scripts.c.id)
        .where(
            script_segment_claims.c.script_segment_id == script_segment_id,
            scripts.c.project_id == project_id,
        )
    )
    rows = (await session.execute(stmt)).mappings().all()
    return [_segment_claim_from_row(row) for row in rows]


async def list_segment_claims_for_script(
    session: AsyncSession, *, project_id: UUID, script_id: UUID
) -> list[ScriptSegmentClaimRecord]:
    """Every ``script_segment_claims`` row for one whole script in a single
    query - the N+1-free counterpart to :func:`list_segment_claims`, for
    assembling a full script-detail view. Project-scoped through
    ``script_segments -> scripts``; a script from another project yields
    ``[]`` (rule 9).
    """
    stmt = (
        select(script_segment_claims)
        .join(
            script_segments,
            script_segment_claims.c.script_segment_id == script_segments.c.id,
        )
        .join(scripts, script_segments.c.script_id == scripts.c.id)
        .where(
            script_segments.c.script_id == script_id,
            scripts.c.project_id == project_id,
        )
    )
    rows = (await session.execute(stmt)).mappings().all()
    return [_segment_claim_from_row(row) for row in rows]


async def set_status(
    session: AsyncSession,
    *,
    project_id: UUID,
    script_id: UUID,
    status: str,
    estimated_duration_ms: int | None = None,
    llm_model: str | None = None,
) -> ScriptRecord | None:
    """The only update path on ``scripts`` - a status-machine transition
    (``pending -> draft -> approved`` / ``failed`` / ``superseded``).
    ``updated_at`` is stamped by the ``trg_scripts_set_updated_at`` trigger.
    Project-scoped: returns ``None`` if the script doesn't exist *or*
    belongs to another project (rule 9).

    ``llm_model`` is written on the transitions the generation job makes (to
    ``draft`` or ``failed``): a script is queued with the model it is *meant* to be
    written by, and the job then records the model it actually ran with.
    """
    values: dict[str, Any] = {"status": status}
    if estimated_duration_ms is not None:
        values["estimated_duration_ms"] = estimated_duration_ms
    if llm_model is not None:
        values["llm_model"] = llm_model

    stmt = (
        scripts.update()
        .where(scripts.c.id == script_id, scripts.c.project_id == project_id)
        .values(**values)
        .returning(*scripts.c)
    )
    try:
        result = await session.execute(stmt)
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)
    row = result.mappings().one_or_none()
    return _script_from_row(row) if row is not None else None


async def set_measured_duration(
    session: AsyncSession, *, project_id: UUID, script_id: UUID, measured_duration_ms: int
) -> ScriptRecord | None:
    """Set the script's authoritative, post-audio duration (Phase 8) -
    never ``estimated_duration_ms``, which stays exactly the Phase 7 WPM
    estimate this never touches. Callers only ever set this once every one
    of the script's segments has valid audio (``services.script_audio`` -
    a partial synthesis is never presented as complete), never
    incrementally. Project-scoped: returns ``None`` if the script doesn't
    exist or belongs to another project (rule 9).
    """
    stmt = (
        scripts.update()
        .where(scripts.c.id == script_id, scripts.c.project_id == project_id)
        .values(measured_duration_ms=measured_duration_ms)
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
