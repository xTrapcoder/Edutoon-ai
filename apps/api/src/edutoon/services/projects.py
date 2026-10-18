from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.config import get_settings
from edutoon.core.errors import NotFoundError, ValidationError
from edutoon.core.pagination import Page
from edutoon.domain.generation_settings import (
    DEFAULT_LANGUAGE,
    DEFAULT_ORIENTATION,
    DEFAULT_TARGET_DURATION_MS,
    Language,
    Orientation,
)
from edutoon.domain.source_storage import belongs_to_project
from edutoon.domain.topic import (
    INSTRUCTIONS_UNSUPPORTED,
    KEYWORD_ONLY_UNSUPPORTED,
    PPTX_UNSUPPORTED,
)
from edutoon.providers.storage import Storage
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import render as render_repo
from edutoon.repositories import script_segment_audio as script_segment_audio_repo
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories.projects import ProjectRecord as ProjectRecord
from edutoon.repositories.uploaded_sources import StorageLocation as StorageLocation

log = structlog.get_logger()


async def create_project(
    session: AsyncSession,
    *,
    owner_id: UUID,
    title: str,
    source_type: str,
    description: str | None = None,
    language: Language = DEFAULT_LANGUAGE,
    orientation: Orientation = DEFAULT_ORIENTATION,
    target_duration_ms: int = DEFAULT_TARGET_DURATION_MS,
    instructions: str | None = None,
) -> ProjectRecord:
    settings = get_settings()
    if source_type == "topic" and not settings.KEYWORD_ONLY_PROJECTS_ENABLED:
        raise ValidationError(KEYWORD_ONLY_UNSUPPORTED)
    if source_type == "pptx" and not settings.PPTX_PROJECTS_ENABLED:
        raise ValidationError(PPTX_UNSUPPORTED)
    if instructions and instructions.strip() and not settings.INSTRUCTIONS_ENABLED:
        raise ValidationError(INSTRUCTIONS_UNSUPPORTED)
    return await projects_repo.create(
        session,
        owner_id=owner_id,
        title=title,
        source_type=source_type,
        description=description,
        language=language,
        orientation=orientation,
        target_duration_ms=target_duration_ms,
        instructions=instructions,
    )


async def get_project_or_404(session: AsyncSession, project_id: UUID) -> ProjectRecord:
    project = await projects_repo.get_by_id(session, project_id)
    if project is None:
        raise NotFoundError(f"Project {project_id} not found.")
    return project


async def list_projects_for_owner(
    session: AsyncSession, owner_id: UUID, *, limit: int = 50, cursor: str | None = None
) -> Page[ProjectRecord]:
    return await projects_repo.list_by_owner(session, owner_id, limit=limit, cursor=cursor)


# --- Authenticated, ownership-scoped API (rule 9: not-owned looks like missing) ---


async def get_project(session: AsyncSession, *, project_id: UUID, owner_id: UUID) -> ProjectRecord:
    project = await projects_repo.get_by_id_for_owner(session, project_id, owner_id)
    if project is None:
        raise NotFoundError(f"Project {project_id} not found.")
    return project


async def list_projects(
    session: AsyncSession, *, owner_id: UUID, limit: int = 50, cursor: str | None = None
) -> Page[ProjectRecord]:
    return await projects_repo.list_by_owner(session, owner_id, limit=limit, cursor=cursor)


async def update_project(
    session: AsyncSession, *, project_id: UUID, owner_id: UUID, **fields: Any
) -> ProjectRecord:
    new_instructions = fields.get("instructions")
    if (
        isinstance(new_instructions, str)
        and new_instructions.strip()
        and not get_settings().INSTRUCTIONS_ENABLED
    ):
        raise ValidationError(INSTRUCTIONS_UNSUPPORTED)
    project = await projects_repo.update(
        session, project_id=project_id, owner_id=owner_id, **fields
    )
    if project is None:
        raise NotFoundError(f"Project {project_id} not found.")
    return project


async def delete_project(
    session: AsyncSession, *, project_id: UUID, owner_id: UUID
) -> list[StorageLocation]:
    """Delete the project and everything the database holds under it (sources, topics, jobs,
    scripts, script_segment_audio - all ``ON DELETE CASCADE``), and return where its uploaded
    files, synthesised segment audio (Phase 8) and rendered video chunks and outputs (Phase 10)
    were stored.

    The files themselves are **not** touched here. Removing them is a separate step,
    :func:`remove_stored_files`, that the caller runs *after* this deletion is committed: the
    database is the record of what exists, so it must be the thing that changes first. The
    other order risks a project whose deletion fails or rolls back but whose PDF is already
    gone; this order can at worst leave an orphaned file, which harms no one.

    Raises :class:`~edutoon.core.errors.NotFoundError` if the project is missing or not owned
    (rule 9). The deletion is scoped to the owner, so a caller who does not own the project
    deletes nothing, and no file locations are returned for anyone to remove.
    """
    files = await uploaded_sources_repo.list_storage_locations(session, project_id)
    audio_files = await script_segment_audio_repo.list_storage_locations_for_project(
        session, project_id
    )
    render_files = await render_repo.list_storage_locations_for_project(session, project_id)
    files = [
        *files,
        *(StorageLocation(bucket=a.bucket, key=a.key) for a in audio_files),
        *(StorageLocation(bucket=r.bucket, key=r.key) for r in render_files),
    ]

    deleted = await projects_repo.delete(session, project_id=project_id, owner_id=owner_id)
    if not deleted:
        raise NotFoundError(f"Project {project_id} not found.")
    return files


async def remove_stored_files(
    storage: Storage, *, project_id: UUID, files: Sequence[StorageLocation]
) -> None:
    """Remove a deleted project's uploaded files from object storage. Never raises.

    Called after the project's deletion has been committed, so the project is gone whatever
    happens here. Each file is removed on its own: one that cannot be removed is logged (with
    its bucket and key, so it can be cleaned up by hand) and does not stop the rest. Removing
    an object that is already gone succeeds, so running this twice is harmless.

    Only files inside the project's own storage prefix are touched (``belongs_to_project``):
    a row that somehow points elsewhere is skipped and logged, never deleted. Nothing is ever
    found by listing a prefix; only the exact objects this project's rows named are removed.
    """
    for file in files:
        if not belongs_to_project(file.key, project_id):
            log.warning(
                "project.file_cleanup_skipped",
                project_id=str(project_id),
                bucket=file.bucket,
                key=file.key,
                reason="outside the project's storage prefix",
            )
            continue
        try:
            await storage.delete_object(bucket=file.bucket, key=file.key)
        except Exception as exc:  # noqa: BLE001 - cleanup after a committed delete must not fail it
            log.warning(
                "project.file_cleanup_failed",
                project_id=str(project_id),
                bucket=file.bucket,
                key=file.key,
                error=str(exc),
            )
