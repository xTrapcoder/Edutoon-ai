"""What the training-module tests share.

Two kinds of source video:

* :func:`add_fake_render` - a ``render_outputs`` row plus a small object in the real local MinIO.
  The bytes are not a playable video (the publish code never decodes it; it copies and measures
  it), so this is fast and is what the rollback-style API tests use.
* :func:`render_real_video` - the real Pillow + FFmpeg renderer producing a real (tiny) MP4. The
  snapshot-independence tests use it, because they must change the bytes of a genuine re-render.

Committed tests (see ``test_training_committed.py``) need a real, committing database session, so
they create their own QA user and project and :func:`purge_qa` removes exactly what they made -
nothing else in the shared development database is touched.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from edutoon.core.config import get_settings
from edutoon.domain.render import output_storage_key
from edutoon.providers.storage import Storage
from edutoon.repositories import render as render_repo
from edutoon.repositories import script_segment_audio as audio_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories import uploaded_sources as uploaded_repo
from edutoon.repositories.projects import ProjectRecord
from edutoon.repositories.render import OutputScene, RenderOutputRecord
from edutoon.repositories.scripts import ScriptRecord

FAKE_MP4_HEAD = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def add_script(session: AsyncSession, project: ProjectRecord, *, approved: bool = True):
    script = await scripts_repo.create(session, project_id=project.id, llm_model="claude-sonnet-5")
    if approved:
        script = await scripts_repo.set_status(
            session, project_id=project.id, script_id=script.id, status="approved"
        )
    assert script is not None
    return script


async def add_fake_render(
    session: AsyncSession,
    storage: Storage,
    *,
    project: ProjectRecord,
    script: ScriptRecord,
    orientation: str = "horizontal",
    quality: str = "low",
    size: int = 4096,
    duration_ms: int = 13_500,
) -> tuple[RenderOutputRecord, bytes]:
    """A render output whose object really exists in storage. Returns the row and its bytes."""
    settings = get_settings()
    body = FAKE_MP4_HEAD + os.urandom(size)
    key = output_storage_key(project.id, script.id, orientation, quality)
    await storage.put_object(
        bucket=settings.BUCKET_OUTPUTS, key=key, body=body, content_type="video/mp4"
    )
    output = await render_repo.replace_output(
        session,
        project_id=project.id,
        script_id=script.id,
        job_id=None,
        orientation=orientation,
        quality=quality,
        output_hash=sha256_of(body),
        renderer_version="test",
        encoder="libx264",
        fps=30,
        width=640 if orientation == "horizontal" else 360,
        height=360 if orientation == "horizontal" else 640,
        duration_ms=duration_ms,
        byte_size=len(body),
        storage_bucket=settings.BUCKET_OUTPUTS,
        storage_key=key,
        probe={"test": True},
        scenes=[OutputScene(position=0, script_segment_id=uuid4(), scene_index=0, chunk_hash="x")],
    )
    return output, body


def render_body(project: ProjectRecord, script: ScriptRecord, **extra: Any) -> dict[str, Any]:
    return {
        "project_id": str(project.id),
        "script_id": str(script.id),
        "orientation": "horizontal",
        "quality": "low",
        **extra,
    }


async def object_bytes(storage: Storage, bucket: str, key: str) -> bytes:
    return (await storage.get_object(bucket=bucket, key=key)).body


async def object_exists(storage: Storage, bucket: str, key: str) -> bool:
    return await storage.object_exists(bucket=bucket, key=key)


async def list_keys(storage: Storage, bucket: str, prefix: str) -> list[str]:
    client = storage._client  # noqa: SLF001 - test clean-up/inspection lists a prefix
    listing = await asyncio.to_thread(client.list_objects_v2, Bucket=bucket, Prefix=prefix)
    return [item["Key"] for item in listing.get("Contents", [])]


# --- QA data that is really committed ------------------------------------------------------------


@dataclass(slots=True)
class QaRegistry:
    """Everything a committed test created, so :func:`purge_qa` can remove exactly that."""

    user_ids: list[UUID] = field(default_factory=list)
    project_ids: list[UUID] = field(default_factory=list)
    storage_keys: list[tuple[str, str]] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


async def purge_qa(
    sessionmaker: async_sessionmaker[AsyncSession], storage: Storage, registry: QaRegistry
) -> None:
    """Delete the QA courses (and so modules and enrolments), their stored videos, the QA projects
    (and their stored files) and finally the QA users. Only ids in ``registry`` are touched.

    ``audit_logs`` is append-only by design (a trigger rejects deleting a row), so the audit rows
    that name QA entities stay; they hold no data beyond ids, an action and a timestamp.
    """
    async with sessionmaker() as session:
        # Submitted attempts are append-only and RESTRICT the module, assessment and user, so
        # nothing that has one can be deleted - by design. The database these tests commit to is a
        # scratch one that is dropped with the session; only the stored files are cleaned up then.
        attempted = (
            await session.execute(
                text("SELECT 1 FROM assessment_attempts WHERE user_id = ANY(:ids) LIMIT 1"),
                {"ids": list(registry.user_ids)},
            )
        ).first()
        for user_id in registry.user_ids:
            rows = (
                await session.execute(
                    text(
                        "SELECT video_bucket, video_key FROM training_modules m "
                        "JOIN training_courses c ON c.id = m.course_id "
                        "WHERE c.created_by = :u AND m.video_key IS NOT NULL"
                    ),
                    {"u": user_id},
                )
            ).all()
            registry.storage_keys.extend((bucket, key) for bucket, key in rows)
            if attempted is None:
                await session.execute(
                    text("DELETE FROM training_courses WHERE created_by = :u"), {"u": user_id}
                )
        for project_id in registry.project_ids:
            registry.storage_keys.extend(
                (loc.bucket, loc.key)
                for loc in [
                    *await uploaded_repo.list_storage_locations(session, project_id),
                    *await render_repo.list_storage_locations_for_project(session, project_id),
                ]
            )
            registry.storage_keys.extend(
                (loc.bucket, loc.key)
                for loc in await audio_repo.list_storage_locations_for_project(session, project_id)
            )
            if attempted is None:
                await session.execute(text("DELETE FROM projects WHERE id = :p"), {"p": project_id})
        for user_id in registry.user_ids:
            if attempted is None:
                await session.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
        await session.commit()
    for bucket, key in registry.storage_keys:
        await storage.delete_object(bucket=bucket, key=key)


class RecordingLog:
    """Stands in for ``services.training.log``. (structlog caches a logger's processors on first
    use, so ``structlog.testing.capture_logs`` only works if nothing earlier in the run has
    logged; this does not depend on that.)"""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def _add(self, level: str, event: str, fields: dict[str, Any]) -> None:
        self.entries.append({"event": event, "log_level": level, **fields})

    def warning(self, event: str, **fields: Any) -> None:
        self._add("warning", event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._add("error", event, fields)

    def info(self, event: str, **fields: Any) -> None:
        self._add("info", event, fields)
