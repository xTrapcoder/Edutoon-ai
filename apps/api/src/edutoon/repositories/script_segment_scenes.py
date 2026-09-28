"""``script_segment_scenes`` - one narration segment's timed, templated scene
set (Phase 9).

Unlike ``script_segments``/``script_segment_claims``, this table is **not**
append-only: :func:`replace_for_segment` replaces a segment's *whole* scene
set - DELETE every existing row for the segment, then INSERT the new set -
inside one SAVEPOINT, so a partially-written set (some old scenes, some new)
can never be observed, and ``SUM(scene.duration_ms)`` always equals the
segment's measured audio duration the instant a replacement commits. This is
what makes a retry that changes the scene count (three scenes becoming five)
safe: there is no per-row UPDATE trying to reconcile old rows with new ones.

A segment whose measured audio is too short to support even one scene at the
configured minimum (``domain.storyboard_timing.effective_scene_count``
returning ``0``) simply has no rows here - that is a deliberate, permanent,
recomputable outcome, not a "not yet attempted" state, so no special marker
row is written for it (``services.storyboard`` decides completeness from the
same domain function, not from row presence).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.db.tables import script_segment_scenes, script_segments, scripts


@dataclass(frozen=True, slots=True)
class NewScene:
    scene_index: int
    start_ms: int
    duration_ms: int
    template: str
    content: dict[str, Any]
    source_audio_content_hash: str


@dataclass(frozen=True, slots=True)
class ScriptSegmentSceneRecord:
    id: UUID
    script_segment_id: UUID
    scene_index: int
    start_ms: int
    duration_ms: int
    template: str
    content: dict[str, Any]
    source_audio_content_hash: str
    created_at: datetime
    updated_at: datetime


def _from_row(row: RowMapping) -> ScriptSegmentSceneRecord:
    return ScriptSegmentSceneRecord(
        id=row["id"],
        script_segment_id=row["script_segment_id"],
        scene_index=row["scene_index"],
        start_ms=row["start_ms"],
        duration_ms=row["duration_ms"],
        template=row["template"],
        content=row["content"],
        source_audio_content_hash=row["source_audio_content_hash"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def replace_for_segment(
    session: AsyncSession, *, script_segment_id: UUID, scenes: Sequence[NewScene]
) -> list[ScriptSegmentSceneRecord]:
    """Replace ``script_segment_id``'s whole scene set with ``scenes``, inside
    one SAVEPOINT: DELETE every existing row for the segment, then INSERT the
    given set (which may be empty, clearing the segment's scenes outright).
    Committed by the caller in its own short transaction - the same
    per-segment isolation ``script_segment_audio.upsert`` gives narration
    synthesis, so one segment's replacement is durable the instant it
    finishes, independent of every other segment's outcome.

    Raises the database's own :class:`~sqlalchemy.exc.IntegrityError`
    unwrapped for a CHECK/UNIQUE violation, exactly like
    ``script_segment_audio.upsert`` - every value here is already validated
    upstream (``domain.storyboard_timing`` for the durations, a validated
    LLM draft for the template/content, sequential positions for
    ``scene_index``), so a violation reaching this far is a defence-in-depth
    signal, not a caller mistake to translate into a friendly message.
    """
    async with session.begin_nested():
        await session.execute(
            delete(script_segment_scenes).where(
                script_segment_scenes.c.script_segment_id == script_segment_id
            )
        )
        if not scenes:
            return []
        stmt = (
            script_segment_scenes.insert()
            .values(
                [
                    {
                        "script_segment_id": script_segment_id,
                        "scene_index": scene.scene_index,
                        "start_ms": scene.start_ms,
                        "duration_ms": scene.duration_ms,
                        "template": scene.template,
                        "content": scene.content,
                        "source_audio_content_hash": scene.source_audio_content_hash,
                    }
                    for scene in scenes
                ]
            )
            .returning(*script_segment_scenes.c)
        )
        result = await session.execute(stmt)
        rows = [_from_row(row) for row in result.mappings().all()]

    return sorted(rows, key=lambda row: row.scene_index)


async def list_by_segment_id(
    session: AsyncSession, script_segment_id: UUID
) -> list[ScriptSegmentSceneRecord]:
    """One segment's scenes, in scene order - not itself ownership-scoped
    (mirrors ``script_segment_audio_repo.get_by_segment_id``): callers that
    already resolved the segment through an ownership-scoped path (the
    storyboard job, working segment by segment) use this directly; the API's
    read model uses :func:`list_by_script` instead.
    """
    stmt = (
        select(script_segment_scenes)
        .where(script_segment_scenes.c.script_segment_id == script_segment_id)
        .order_by(script_segment_scenes.c.scene_index.asc())
    )
    rows = (await session.execute(stmt)).mappings().all()
    return [_from_row(row) for row in rows]


async def list_by_script(
    session: AsyncSession, *, project_id: UUID, script_id: UUID
) -> list[ScriptSegmentSceneRecord]:
    """Every scene for one script's segments, project-scoped (rule 9): a
    script from another project yields ``[]``. Ordered by the owning
    segment's narration order, then scene order within it - unpaginated,
    bounded by ``MAX_SCENES_PER_SCRIPT``, the same reasoning as
    ``scripts_repo.list_segments``.
    """
    stmt = (
        select(script_segment_scenes)
        .join(
            script_segments,
            script_segment_scenes.c.script_segment_id == script_segments.c.id,
        )
        .join(scripts, script_segments.c.script_id == scripts.c.id)
        .where(
            script_segments.c.script_id == script_id,
            scripts.c.project_id == project_id,
        )
        .order_by(
            script_segments.c.segment_index.asc(),
            script_segment_scenes.c.scene_index.asc(),
        )
    )
    rows = (await session.execute(stmt)).mappings().all()
    return [_from_row(row) for row in rows]
