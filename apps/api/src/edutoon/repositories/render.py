"""Render artifacts (Phase 10, migration 0016): the chunk cache, the finished outputs and the
per-scene record of which chunk each output used.

Nothing here holds video bytes - every row names an object in object storage. A chunk row is
inserted only *after* its object is uploaded, so the row is the commit: a crash between the
upload and the insert leaves an orphan object at a deterministic key (harmless, overwritten by
the next attempt) and never a row that points at nothing.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.db.tables import render_chunks, render_output_scenes, render_outputs


@dataclass(frozen=True, slots=True)
class StorageLocation:
    """Where one render object is kept in object storage."""

    bucket: str
    key: str


@dataclass(frozen=True, slots=True)
class RenderChunkRecord:
    id: UUID
    project_id: UUID
    chunk_hash: str
    orientation: str
    quality: str
    renderer_version: str
    storage_bucket: str
    storage_key: str
    byte_size: int
    frame_count: int
    duration_ms: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RenderOutputRecord:
    id: UUID
    project_id: UUID
    script_id: UUID
    job_id: UUID | None
    orientation: str
    quality: str
    output_hash: str
    renderer_version: str
    encoder: str
    fps: int
    width: int
    height: int
    duration_ms: int
    byte_size: int
    storage_bucket: str
    storage_key: str
    probe: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OutputScene:
    position: int
    script_segment_id: UUID
    scene_index: int
    chunk_hash: str


def _chunk_from_row(row: RowMapping) -> RenderChunkRecord:
    return RenderChunkRecord(**{name: row[name] for name in RenderChunkRecord.__slots__})


def _output_from_row(row: RowMapping) -> RenderOutputRecord:
    return RenderOutputRecord(**{name: row[name] for name in RenderOutputRecord.__slots__})


# --- chunks ----------------------------------------------------------------------------


async def chunks_by_hashes(
    session: AsyncSession, *, project_id: UUID, hashes: Collection[str]
) -> dict[str, RenderChunkRecord]:
    """The stored chunks of ``project_id`` among ``hashes``, keyed by fingerprint."""
    if not hashes:
        return {}
    stmt = select(render_chunks).where(
        render_chunks.c.project_id == project_id, render_chunks.c.chunk_hash.in_(list(hashes))
    )
    rows = (await session.execute(stmt)).mappings().all()
    return {row["chunk_hash"]: _chunk_from_row(row) for row in rows}


async def add_chunk(
    session: AsyncSession,
    *,
    project_id: UUID,
    chunk_hash: str,
    orientation: str,
    quality: str,
    renderer_version: str,
    storage_bucket: str,
    storage_key: str,
    byte_size: int,
    frame_count: int,
    duration_ms: int,
) -> RenderChunkRecord:
    """Record an uploaded chunk. Idempotent: a chunk with this fingerprint already recorded
    (a retry that raced a slow worker) is updated in place rather than duplicated."""
    values = {
        "project_id": project_id,
        "chunk_hash": chunk_hash,
        "orientation": orientation,
        "quality": quality,
        "renderer_version": renderer_version,
        "storage_bucket": storage_bucket,
        "storage_key": storage_key,
        "byte_size": byte_size,
        "frame_count": frame_count,
        "duration_ms": duration_ms,
    }
    stmt = (
        pg_insert(render_chunks)
        .values(**values)
        .on_conflict_do_update(
            constraint="uq_render_chunks_project_id_chunk_hash",
            set_={k: v for k, v in values.items() if k not in ("project_id", "chunk_hash")},
        )
        .returning(*render_chunks.c)
    )
    row = (await session.execute(stmt)).mappings().one()
    return _chunk_from_row(row)


async def delete_chunks(
    session: AsyncSession, *, project_id: UUID, chunk_ids: Collection[UUID]
) -> None:
    if not chunk_ids:
        return
    await session.execute(
        delete(render_chunks).where(
            render_chunks.c.project_id == project_id, render_chunks.c.id.in_(list(chunk_ids))
        )
    )


async def list_unreferenced_chunks(
    session: AsyncSession,
    *,
    project_id: UUID,
    orientation: str,
    quality: str,
    keep_hashes: Collection[str],
) -> list[RenderChunkRecord]:
    """Chunks of ``project_id`` for one ``(orientation, quality)`` that no finished output
    uses and that are not in ``keep_hashes`` - what is safe to garbage-collect once a render of
    that configuration has succeeded. Chunks of other configurations (say, a half-finished
    vertical render) are never candidates."""
    used = (
        select(render_output_scenes.c.chunk_hash)
        .join(render_outputs, render_output_scenes.c.render_output_id == render_outputs.c.id)
        .where(render_outputs.c.project_id == project_id)
    )
    stmt = select(render_chunks).where(
        render_chunks.c.project_id == project_id,
        render_chunks.c.orientation == orientation,
        render_chunks.c.quality == quality,
        render_chunks.c.chunk_hash.not_in(used),
    )
    if keep_hashes:
        stmt = stmt.where(render_chunks.c.chunk_hash.not_in(list(keep_hashes)))
    return [_chunk_from_row(row) for row in (await session.execute(stmt)).mappings().all()]


# --- outputs -----------------------------------------------------------------------------


async def get_output(
    session: AsyncSession,
    *,
    project_id: UUID,
    script_id: UUID,
    orientation: str,
    quality: str,
) -> RenderOutputRecord | None:
    stmt = select(render_outputs).where(
        render_outputs.c.project_id == project_id,
        render_outputs.c.script_id == script_id,
        render_outputs.c.orientation == orientation,
        render_outputs.c.quality == quality,
    )
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _output_from_row(row) if row else None


async def list_outputs_for_script(
    session: AsyncSession, *, project_id: UUID, script_id: UUID
) -> list[RenderOutputRecord]:
    stmt = select(render_outputs).where(
        render_outputs.c.project_id == project_id, render_outputs.c.script_id == script_id
    )
    return [_output_from_row(row) for row in (await session.execute(stmt)).mappings().all()]


async def replace_output(
    session: AsyncSession,
    *,
    project_id: UUID,
    script_id: UUID,
    job_id: UUID | None,
    orientation: str,
    quality: str,
    output_hash: str,
    renderer_version: str,
    encoder: str,
    fps: int,
    width: int,
    height: int,
    duration_ms: int,
    byte_size: int,
    storage_bucket: str,
    storage_key: str,
    probe: dict[str, Any],
    scenes: Sequence[OutputScene],
) -> RenderOutputRecord:
    """Make this the current output for ``(script, orientation, quality)`` and record which
    chunk each scene used, in one SAVEPOINT - so the row and its scene list are never seen
    half-updated."""
    values = {
        "project_id": project_id,
        "script_id": script_id,
        "job_id": job_id,
        "orientation": orientation,
        "quality": quality,
        "output_hash": output_hash,
        "renderer_version": renderer_version,
        "encoder": encoder,
        "fps": fps,
        "width": width,
        "height": height,
        "duration_ms": duration_ms,
        "byte_size": byte_size,
        "storage_bucket": storage_bucket,
        "storage_key": storage_key,
        "probe": probe,
    }
    async with session.begin_nested():
        stmt = (
            pg_insert(render_outputs)
            .values(**values)
            .on_conflict_do_update(
                constraint="uq_render_outputs_script_id_orientation_quality",
                set_={
                    k: v
                    for k, v in values.items()
                    if k not in ("script_id", "orientation", "quality")
                },
            )
            .returning(*render_outputs.c)
        )
        row = (await session.execute(stmt)).mappings().one()
        record = _output_from_row(row)
        await session.execute(
            delete(render_output_scenes).where(render_output_scenes.c.render_output_id == record.id)
        )
        if scenes:
            await session.execute(
                render_output_scenes.insert().values(
                    [
                        {
                            "render_output_id": record.id,
                            "position": scene.position,
                            "script_segment_id": scene.script_segment_id,
                            "scene_index": scene.scene_index,
                            "chunk_hash": scene.chunk_hash,
                        }
                        for scene in scenes
                    ]
                )
            )
    return record


async def list_output_scenes(session: AsyncSession, *, output_id: UUID) -> list[OutputScene]:
    stmt = (
        select(render_output_scenes)
        .where(render_output_scenes.c.render_output_id == output_id)
        .order_by(render_output_scenes.c.position)
    )
    return [
        OutputScene(
            position=row["position"],
            script_segment_id=row["script_segment_id"],
            scene_index=row["scene_index"],
            chunk_hash=row["chunk_hash"],
        )
        for row in (await session.execute(stmt)).mappings().all()
    ]


async def list_storage_locations_for_project(
    session: AsyncSession, project_id: UUID
) -> list[StorageLocation]:
    """Every render object (chunks and finished videos) that ``project_id`` owns - called
    before the project's cascading delete commits so the caller can remove the objects from
    storage afterwards (``services.projects.remove_stored_files``)."""
    chunks = select(render_chunks.c.storage_bucket, render_chunks.c.storage_key).where(
        render_chunks.c.project_id == project_id
    )
    outputs = select(render_outputs.c.storage_bucket, render_outputs.c.storage_key).where(
        render_outputs.c.project_id == project_id
    )
    rows = [*(await session.execute(chunks)).all(), *(await session.execute(outputs)).all()]
    return [StorageLocation(bucket=bucket, key=key) for bucket, key in rows]
