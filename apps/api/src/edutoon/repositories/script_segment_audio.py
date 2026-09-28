"""``script_segment_audio`` - one narration segment's synthesised audio
(Phase 8).

Unlike ``script_segments``/``script_segment_claims``, this table is **not**
append-only: :func:`upsert` writes the one *current* row for a segment via
``INSERT ... ON CONFLICT (script_segment_id) DO UPDATE``, so a re-synthesis
(after a text, voice or settings change) replaces the previous attempt's row
rather than accumulating history. A row exists only once an attempt has
*finished* - ``ready`` or ``failed`` - never a separate "pending" status; a
segment with no row yet is simply not started.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.db.tables import script_segment_audio, script_segments, scripts


@dataclass(frozen=True, slots=True)
class ScriptSegmentAudioRecord:
    id: UUID
    script_segment_id: UUID
    status: str
    storage_bucket: str | None
    storage_key: str | None
    format: str | None
    sample_rate: int | None
    channels: int | None
    measured_duration_ms: int | None
    voice_model: str
    is_temporary_voice_substitute: bool
    content_hash: str
    error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StorageLocation:
    """Where one segment's audio object is kept in object storage."""

    bucket: str
    key: str


def _from_row(row: RowMapping) -> ScriptSegmentAudioRecord:
    return ScriptSegmentAudioRecord(
        id=row["id"],
        script_segment_id=row["script_segment_id"],
        status=row["status"],
        storage_bucket=row["storage_bucket"],
        storage_key=row["storage_key"],
        format=row["format"],
        sample_rate=row["sample_rate"],
        channels=row["channels"],
        measured_duration_ms=row["measured_duration_ms"],
        voice_model=row["voice_model"],
        is_temporary_voice_substitute=row["is_temporary_voice_substitute"],
        content_hash=row["content_hash"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def upsert(
    session: AsyncSession,
    *,
    script_segment_id: UUID,
    status: str,
    voice_model: str,
    is_temporary_voice_substitute: bool,
    content_hash: str,
    storage_bucket: str | None = None,
    storage_key: str | None = None,
    format: str | None = None,  # noqa: A002 - matches the column name
    sample_rate: int | None = None,
    channels: int | None = None,
    measured_duration_ms: int | None = None,
    error: str | None = None,
) -> ScriptSegmentAudioRecord:
    """Write ``script_segment_id``'s one current audio row, replacing
    whatever was there before (a prior successful attempt, a prior failure,
    or nothing). Committed by the caller in its own short transaction - this
    is exactly what makes per-segment resumability real: each segment's
    result is durable the moment it finishes, independent of any other
    segment's outcome.
    """
    values = {
        "script_segment_id": script_segment_id,
        "status": status,
        "storage_bucket": storage_bucket,
        "storage_key": storage_key,
        "format": format,
        "sample_rate": sample_rate,
        "channels": channels,
        "measured_duration_ms": measured_duration_ms,
        "voice_model": voice_model,
        "is_temporary_voice_substitute": is_temporary_voice_substitute,
        "content_hash": content_hash,
        "error": error,
    }
    stmt = (
        pg_insert(script_segment_audio)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[script_segment_audio.c.script_segment_id],
            set_=values,
        )
        .returning(*script_segment_audio.c)
    )
    result = await session.execute(stmt)
    return _from_row(result.mappings().one())


async def get_by_segment_id(
    session: AsyncSession, script_segment_id: UUID
) -> ScriptSegmentAudioRecord | None:
    stmt = select(script_segment_audio).where(
        script_segment_audio.c.script_segment_id == script_segment_id
    )
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def get_by_segment_id_for_script(
    session: AsyncSession, *, project_id: UUID, script_id: UUID, script_segment_id: UUID
) -> ScriptSegmentAudioRecord | None:
    """Ownership-scoped fetch, joined through
    ``script_segments -> scripts`` and scoped to *both* the project and the
    specific script (rule 9): audio for a segment belonging to another
    project, or to a different script within the same project (a project can
    have more than one script over time), looks identical to audio that
    doesn't exist.
    """
    stmt = (
        select(script_segment_audio)
        .join(
            script_segments,
            script_segment_audio.c.script_segment_id == script_segments.c.id,
        )
        .join(scripts, script_segments.c.script_id == scripts.c.id)
        .where(
            script_segment_audio.c.script_segment_id == script_segment_id,
            script_segments.c.script_id == script_id,
            scripts.c.project_id == project_id,
        )
    )
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def list_by_script(
    session: AsyncSession, *, project_id: UUID, script_id: UUID
) -> list[ScriptSegmentAudioRecord]:
    """Every audio row for one script's segments, project-scoped. Unpaginated
    (bounded by ``MAX_SCRIPT_SEGMENTS``, the same reasoning as
    ``scripts.list_segments``). A script from another project yields ``[]``
    (rule 9).
    """
    stmt = (
        select(script_segment_audio)
        .join(
            script_segments,
            script_segment_audio.c.script_segment_id == script_segments.c.id,
        )
        .join(scripts, script_segments.c.script_id == scripts.c.id)
        .where(
            script_segments.c.script_id == script_id,
            scripts.c.project_id == project_id,
        )
    )
    rows = (await session.execute(stmt)).mappings().all()
    return [_from_row(row) for row in rows]


async def list_storage_locations_for_project(
    session: AsyncSession, project_id: UUID
) -> list[StorageLocation]:
    """Where every ``ready`` segment audio object of ``project_id`` is
    stored, across every one of its scripts (current and superseded alike -
    deleting the project removes all of them). Mirrors
    ``uploaded_sources_repo.list_storage_locations``: called before the
    project's cascading delete commits, so the caller can remove the objects
    from storage afterwards.
    """
    stmt = (
        select(script_segment_audio.c.storage_bucket, script_segment_audio.c.storage_key)
        .join(
            script_segments,
            script_segment_audio.c.script_segment_id == script_segments.c.id,
        )
        .join(scripts, script_segments.c.script_id == scripts.c.id)
        .where(
            scripts.c.project_id == project_id,
            script_segment_audio.c.status == "ready",
        )
    )
    rows = (await session.execute(stmt)).all()
    return [
        StorageLocation(bucket=bucket, key=key) for bucket, key in rows if bucket and key
    ]
