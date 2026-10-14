"""Training courses and modules - the administrator's side (Step 2, Piece 2).

* Courses: create, list, read, edit the wording, publish (draft -> published), archive, delete.
* Modules: publish a finished render into a course **by snapshot**, republish (replace the
  snapshot), unpublish, delete, reorder, and stream the stored video for an administrator's preview.

**Publish by snapshot.** A module never serves the project's render. Publishing copies the MP4,
inside the storage server, to a key the module owns
(``training/modules/{module_id}/{publication}.mp4``, a fresh UUID every time, so a key is written
once and never overwritten) and records on the module what that copy is: size, duration, resolution,
frame rate and the render's fingerprint. After that the module depends on nothing of the project's:
a re-render overwrites the project's object, deleting the project removes the project's objects,
and ``source_render_output_id`` (``ON DELETE SET NULL``) is only a note of where the video came
from. :func:`open_module_video` is the one place a module's
video is opened, for the administrator's preview now and for employees once their routes exist.

**Files and the database (the order matters).** A database transaction cannot undo an object
write, so the two are sequenced instead:

1. a *new* object is written first (publish, republish) - it is unreachable until a row names it;
2. the rows change in one transaction;
3. only **after that commits** is an object that is no longer needed removed (the replaced
   snapshot, an unpublished or deleted module's video).

If anything fails before the commit, the new object is removed (best effort) and nothing else is
touched, so a rolled-back change never loses a file that a committed row still names. If a removal
fails after a commit, the committed change stands and the orphan is logged with its bucket and key.
:class:`StorageEffects` carries both lists; the router runs each mutation inside
:func:`storage_effects`, which does the clean-up on the right side of the commit. A key is only
ever deleted if it lies inside the module's own prefix (``belongs_to_module``).

**404s.** A missing course or module is always the same fixed message (``COURSE_NOT_FOUND`` /
``MODULE_NOT_FOUND``, no id), the very message a non-administrator gets (``api.policies``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.config import get_settings
from edutoon.core.errors import ConflictError, NotFoundError
from edutoon.core.pagination import Page
from edutoon.domain.source_storage import belongs_to_project
from edutoon.domain.training import (
    COURSE_ARCHIVED,
    COURSE_DELETE_REFUSED,
    COURSE_DRAFT,
    COURSE_NOT_FOUND,
    COURSE_PUBLISHED,
    MODULE_NOT_FOUND,
    MODULE_PUBLISHED,
    reorder_mismatch,
)
from edutoon.domain.training_storage import VIDEO_CONTENT_TYPE, belongs_to_module, module_video_key
from edutoon.providers.storage import (
    InvalidRangeError,
    ObjectNotFoundError,
    ObjectStream,
    Storage,
)
from edutoon.repositories import render as render_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories import training as training_repo
from edutoon.repositories.training import CourseRecord, ModuleRecord, VideoSnapshot
from edutoon.services import audit_logs as audit_logs_service
from edutoon.services import projects as projects_service

log = structlog.get_logger()

AUDIT_COURSE = "training_course"
AUDIT_MODULE = "training_module"

RENDER_NOT_FOUND = "No rendered video was found for that script, shape and quality."
MODULE_VIDEO_NOT_FOUND = "This module has no video to play."


# --- files: what to remove, and when ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModuleObject:
    """One stored object that belongs to one module."""

    module_id: UUID
    bucket: str
    key: str


@dataclass(slots=True)
class StorageEffects:
    """The object-storage side of one request, to be settled around its commit.

    ``created`` - objects written for this request. Removed if the request fails **before** its
    commit; kept once the commit has gone through.
    ``obsolete`` - objects the committed change no longer needs. Removed only **after** the
    commit succeeded; never touched if it did not.
    """

    storage: Storage
    created: list[ModuleObject] = field(default_factory=list)
    obsolete: list[ModuleObject] = field(default_factory=list)
    committed: bool = False

    def wrap_commit(self, commit: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
        """``commit`` that records that it went through (what decides which list is acted on)."""

        async def _commit() -> None:
            await commit()
            self.committed = True

        return _commit


async def remove_module_objects(
    storage: Storage, objects: Sequence[ModuleObject], *, reason: str
) -> None:
    """Remove objects from storage. Never raises.

    Each object is removed on its own; one that cannot be is logged (``orphaned=True``, with its
    bucket and key, so it can be cleaned by hand) and does not stop the rest. An object outside
    its module's own prefix is skipped and logged - never deleted. Removing an object that is
    already gone succeeds, so running this twice is harmless.
    """
    for item in objects:
        if not belongs_to_module(item.key, item.module_id):
            log.warning(
                "training.object_cleanup_skipped",
                module_id=str(item.module_id),
                bucket=item.bucket,
                key=item.key,
                reason="outside the module's storage prefix",
            )
            continue
        try:
            await storage.delete_object(bucket=item.bucket, key=item.key)
        except Exception as exc:  # noqa: BLE001 - clean-up after a decision must not undo it
            log.warning(
                "training.object_cleanup_failed",
                module_id=str(item.module_id),
                bucket=item.bucket,
                key=item.key,
                cleanup=reason,
                orphaned=True,
                error=str(exc),
            )


@asynccontextmanager
async def storage_effects(storage: Storage) -> AsyncIterator[StorageEffects]:
    """Wrap one mutating request so its files are settled on the right side of the commit.

    Pass ``effects.wrap_commit(session.commit)`` as the request's commit. On the way out:

    * clean exit (committed): ``obsolete`` objects are removed;
    * an error **before** the commit: ``created`` objects are removed, ``obsolete`` ones are left;
    * an error **after** the commit (e.g. recording the idempotent result failed): the change is
      durable, so it is treated as a success - ``obsolete`` removed, ``created`` kept.
    """
    effects = StorageEffects(storage)
    try:
        yield effects
    except BaseException:
        if effects.committed:
            await remove_module_objects(storage, effects.obsolete, reason="after commit")
        else:
            await remove_module_objects(storage, effects.created, reason="after failure")
        raise
    else:
        await remove_module_objects(storage, effects.obsolete, reason="after commit")


# --- helpers ----------------------------------------------------------------------------------


async def _audit(
    session: AsyncSession,
    *,
    actor_id: UUID,
    action: str,
    entity_type: str,
    entity_id: UUID,
    context: dict[str, Any] | None = None,
) -> None:
    await audit_logs_service.record_audit_log(
        session,
        action=action,
        entity_type=entity_type,
        actor_id=actor_id,
        entity_id=entity_id,
        context=context,
    )


async def _course_or_404(
    session: AsyncSession, course_id: UUID, *, for_update: bool = False
) -> CourseRecord:
    course = await training_repo.get_course(session, course_id, for_update=for_update)
    if course is None:
        raise NotFoundError(COURSE_NOT_FOUND)
    return course


def _require_open(course: CourseRecord) -> None:
    if course.status == COURSE_ARCHIVED:
        raise ConflictError("This course is archived, so its modules can no longer be changed.")


def _object_of(module: ModuleRecord) -> ModuleObject | None:
    if module.video_bucket is None or module.video_key is None:
        return None
    return ModuleObject(module.id, module.video_bucket, module.video_key)


async def _lock_module(session: AsyncSession, module_id: UUID) -> tuple[CourseRecord, ModuleRecord]:
    """Lock a module's course, then the module (always in that order), and return both.

    The module is read once to learn its course, then re-read under the locks, so a change that
    committed in between is seen.
    """
    first = await training_repo.get_module(session, module_id)
    if first is None:
        raise NotFoundError(MODULE_NOT_FOUND)
    course = await training_repo.get_course(session, first.course_id, for_update=True)
    module = await training_repo.get_module_locked(session, module_id)
    if course is None or module is None:
        raise NotFoundError(MODULE_NOT_FOUND)
    return course, module


# --- courses ----------------------------------------------------------------------------------


async def create_course(
    session: AsyncSession, *, actor_id: UUID, title: str, description: str | None
) -> CourseRecord:
    course = await training_repo.create_course(
        session, title=title, description=description, created_by=actor_id
    )
    await _audit(
        session,
        actor_id=actor_id,
        action="training.course.created",
        entity_type=AUDIT_COURSE,
        entity_id=course.id,
    )
    return course


async def list_courses(
    session: AsyncSession, *, status: str | None, limit: int, cursor: str | None
) -> Page[CourseRecord]:
    return await training_repo.list_courses(session, status=status, limit=limit, cursor=cursor)


async def get_course(session: AsyncSession, *, course_id: UUID) -> CourseRecord:
    return await _course_or_404(session, course_id)


async def update_course(
    session: AsyncSession, *, actor_id: UUID, course_id: UUID, fields: dict[str, Any]
) -> CourseRecord:
    """Edit the title and/or description. An archived course is frozen."""
    course = await _course_or_404(session, course_id, for_update=True)
    if course.status == COURSE_ARCHIVED:
        raise ConflictError("An archived course cannot be edited.")
    if fields:
        await training_repo.update_course(session, course_id, **fields)
        await _audit(
            session,
            actor_id=actor_id,
            action="training.course.updated",
            entity_type=AUDIT_COURSE,
            entity_id=course_id,
            context={"fields": sorted(fields)},
        )
    return await _course_or_404(session, course_id)


async def publish_course(session: AsyncSession, *, actor_id: UUID, course_id: UUID) -> CourseRecord:
    """Draft -> published. Needs at least one published module: an empty course is no use."""
    course = await _course_or_404(session, course_id, for_update=True)
    if course.status != COURSE_DRAFT:
        raise ConflictError("Only a draft course can be published.")
    if course.published_module_count < 1:
        raise ConflictError("Publish at least one module before publishing the course.")
    await training_repo.update_course(session, course_id, status=COURSE_PUBLISHED)
    await _audit(
        session,
        actor_id=actor_id,
        action="training.course.published",
        entity_type=AUDIT_COURSE,
        entity_id=course_id,
    )
    return await _course_or_404(session, course_id)


async def archive_course(session: AsyncSession, *, actor_id: UUID, course_id: UUID) -> CourseRecord:
    """Hide a course from employees and freeze it, keeping every module, enrolment and record."""
    course = await _course_or_404(session, course_id, for_update=True)
    if course.status == COURSE_ARCHIVED:
        raise ConflictError("This course is already archived.")
    await training_repo.update_course(session, course_id, status=COURSE_ARCHIVED)
    await _audit(
        session,
        actor_id=actor_id,
        action="training.course.archived",
        entity_type=AUDIT_COURSE,
        entity_id=course_id,
    )
    return await _course_or_404(session, course_id)


async def delete_course(
    session: AsyncSession, effects: StorageEffects, *, actor_id: UUID, course_id: UUID
) -> None:
    """Delete a course and its modules. Refused (409, ``COURSE_DELETE_REFUSED``) once anyone has
    been enrolled in it - a **withdrawn** enrolment counts, it is history - or, from Step 3, has any
    other learner history, which the database enforces with ``RESTRICT`` foreign keys that surface
    here the same way: such a course is *archived*, not deleted.

    Its modules' videos are queued for removal after the commit.
    """
    await _course_or_404(session, course_id, for_update=True)
    if await training_repo.count_enrolments(session, course_id) > 0:
        raise ConflictError(COURSE_DELETE_REFUSED)
    objects = [
        obj for obj in map(_object_of, await training_repo.list_modules(session, course_id)) if obj
    ]
    try:
        async with session.begin_nested():
            await training_repo.delete_course(session, course_id)
    except IntegrityError as exc:
        if training_repo.is_foreign_key_violation(exc):
            raise ConflictError(COURSE_DELETE_REFUSED) from exc
        raise
    await _audit(
        session,
        actor_id=actor_id,
        action="training.course.deleted",
        entity_type=AUDIT_COURSE,
        entity_id=course_id,
    )
    effects.obsolete.extend(objects)


# --- modules ----------------------------------------------------------------------------------


async def list_modules(session: AsyncSession, *, course_id: UUID) -> list[ModuleRecord]:
    await _course_or_404(session, course_id)
    return await training_repo.list_modules(session, course_id)


async def get_module(session: AsyncSession, *, module_id: UUID) -> ModuleRecord:
    module = await training_repo.get_module(session, module_id)
    if module is None:
        raise NotFoundError(MODULE_NOT_FOUND)
    return module


@dataclass(frozen=True, slots=True)
class RenderSource:
    """Which finished render to publish: the same identifiers the render API uses."""

    project_id: UUID
    script_id: UUID
    orientation: str
    quality: str


async def _snapshot_from_render(
    session: AsyncSession,
    effects: StorageEffects,
    *,
    actor_id: UUID,
    course_id: UUID,
    module_id: UUID,
    source: RenderSource,
) -> tuple[VideoSnapshot, str]:
    """Check the render may be published and copy it to ``module_id``'s own key.

    Returns the snapshot and the project's title. The project must belong to ``actor_id`` (rule 9:
    anything else is a 404), the script must be approved, the output must exist and its file must
    be the size recorded for it. The copy is registered in ``effects.created`` *before* it is made,
    so a copy that fails half-way is cleaned up like any other new object.
    """
    project = await projects_service.get_project(
        session, project_id=source.project_id, owner_id=actor_id
    )
    script = await scripts_repo.get_by_id_for_project(session, source.script_id, project.id)
    if script is None:
        raise NotFoundError(RENDER_NOT_FOUND)
    output = await render_repo.get_output(
        session,
        project_id=project.id,
        script_id=script.id,
        orientation=source.orientation,
        quality=source.quality,
    )
    if output is None or not belongs_to_project(output.storage_key, project.id):
        raise NotFoundError(RENDER_NOT_FOUND)
    if script.status != "approved":
        raise ConflictError("Approve the script before publishing its video.")

    duplicate = await training_repo.find_published_by_source(
        session, course_id=course_id, source_render_output_id=output.id
    )
    if duplicate is not None and duplicate.id != module_id:
        raise ConflictError("This rendered video is already published in this course.")

    storage = effects.storage
    try:
        source_size = await storage.object_size(
            bucket=output.storage_bucket, key=output.storage_key
        )
    except ObjectNotFoundError as exc:
        raise NotFoundError(RENDER_NOT_FOUND) from exc
    if source_size != output.byte_size:
        raise ConflictError("The rendered video file does not match its record. Render it again.")

    destination = ModuleObject(
        module_id,
        get_settings().BUCKET_OUTPUTS,
        module_video_key(module_id, uuid4()),
    )
    effects.created.append(destination)
    try:
        await storage.copy_object(
            source_bucket=output.storage_bucket,
            source_key=output.storage_key,
            dest_bucket=destination.bucket,
            dest_key=destination.key,
            content_type=VIDEO_CONTENT_TYPE,
        )
        copied_size = await storage.object_size(bucket=destination.bucket, key=destination.key)
    except ObjectNotFoundError as exc:
        raise NotFoundError(RENDER_NOT_FOUND) from exc
    if copied_size != output.byte_size:
        raise ConflictError("The video could not be copied completely. Try publishing again.")

    snapshot = VideoSnapshot(
        bucket=destination.bucket,
        key=destination.key,
        byte_size=output.byte_size,
        duration_ms=output.duration_ms,
        width=output.width,
        height=output.height,
        fps=output.fps,
        output_hash=output.output_hash,
        source_render_output_id=output.id,
    )
    return snapshot, project.title


async def publish_module(
    session: AsyncSession,
    effects: StorageEffects,
    *,
    actor_id: UUID,
    course_id: UUID,
    source: RenderSource,
    title: str | None,
    description: str | None,
) -> ModuleRecord:
    """Publish a render into the course as a new module, last in its order.

    Publishing the same render into a course twice is a 409, decided here before any file is
    copied and enforced again by the database (``uq_training_modules_course_source_render``).
    """
    course = await _course_or_404(session, course_id)
    _require_open(course)
    module_id = await training_repo.new_module_id(session)
    snapshot, project_title = await _snapshot_from_render(
        session,
        effects,
        actor_id=actor_id,
        course_id=course_id,
        module_id=module_id,
        source=source,
    )
    # The module list is changed under the course lock, taken only now so that it is not held
    # while the video is copied.
    course = await _course_or_404(session, course_id, for_update=True)
    _require_open(course)
    module = await training_repo.insert_module(
        session,
        module_id=module_id,
        course_id=course_id,
        position=await training_repo.next_position(session, course_id),
        title=title or project_title,
        description=description,
        snapshot=snapshot,
        published_by=actor_id,
    )
    await _audit(
        session,
        actor_id=actor_id,
        action="training.module.published",
        entity_type=AUDIT_MODULE,
        entity_id=module.id,
        context={
            "course_id": str(course_id),
            "source_render_output_id": str(snapshot.source_render_output_id),
            "video_key": snapshot.key,
        },
    )
    return module


async def republish_module(
    session: AsyncSession,
    effects: StorageEffects,
    *,
    actor_id: UUID,
    module_id: UUID,
    source: RenderSource,
) -> ModuleRecord:
    """Replace a module's video with a fresh snapshot of a render (and publish it again if it
    had been unpublished). The new snapshot gets a new key; the old object is removed only after
    the commit. Position, title and description are unchanged."""
    existing = await get_module(session, module_id=module_id)
    _require_open(await _course_or_404(session, existing.course_id))
    snapshot, _ = await _snapshot_from_render(
        session,
        effects,
        actor_id=actor_id,
        course_id=existing.course_id,
        module_id=module_id,
        source=source,
    )
    course, locked = await _lock_module(session, module_id)
    _require_open(course)
    previous = _object_of(locked)
    module = await training_repo.replace_snapshot(
        session, module_id=module_id, snapshot=snapshot, published_by=actor_id
    )
    await _audit(
        session,
        actor_id=actor_id,
        action="training.module.republished",
        entity_type=AUDIT_MODULE,
        entity_id=module_id,
        context={
            "course_id": str(module.course_id),
            "source_render_output_id": str(snapshot.source_render_output_id),
            "video_key": snapshot.key,
            "replaced_video_key": previous.key if previous else None,
        },
    )
    if previous is not None:
        effects.obsolete.append(previous)
    return module


async def unpublish_module(
    session: AsyncSession, effects: StorageEffects, *, actor_id: UUID, module_id: UUID
) -> ModuleRecord:
    """Take a module away from employees. Its video file is removed after the commit; its place
    in the course, its description and every enrolment and record stay."""
    _, locked = await _lock_module(session, module_id)
    if locked.status != MODULE_PUBLISHED:
        raise ConflictError("This module is already unpublished.")
    previous = _object_of(locked)
    module = await training_repo.mark_unpublished(session, module_id)
    await _audit(
        session,
        actor_id=actor_id,
        action="training.module.unpublished",
        entity_type=AUDIT_MODULE,
        entity_id=module_id,
        context={"course_id": str(module.course_id)},
    )
    if previous is not None:
        effects.obsolete.append(previous)
    return module


async def delete_module(
    session: AsyncSession, effects: StorageEffects, *, actor_id: UUID, module_id: UUID
) -> None:
    """Delete a module and close the gap it leaves in the course's order. Refused (409) if the
    database says something still refers to it (learner history, from Step 3): unpublish it."""
    _, locked = await _lock_module(session, module_id)
    previous = _object_of(locked)
    try:
        async with session.begin_nested():
            await training_repo.delete_module(session, module_id)
    except IntegrityError as exc:
        if training_repo.is_foreign_key_violation(exc):
            raise ConflictError(
                "This module has learner history, so it cannot be deleted. Unpublish it instead."
            ) from exc
        raise
    await training_repo.close_position_gap(
        session, course_id=locked.course_id, after=locked.position
    )
    await _audit(
        session,
        actor_id=actor_id,
        action="training.module.deleted",
        entity_type=AUDIT_MODULE,
        entity_id=module_id,
        context={"course_id": str(locked.course_id)},
    )
    if previous is not None:
        effects.obsolete.append(previous)


async def reorder_modules(
    session: AsyncSession, *, actor_id: UUID, course_id: UUID, module_ids: Sequence[UUID]
) -> list[ModuleRecord]:
    """Put the course's modules in the order given (positions 1..n), in one statement.

    ``module_ids`` must be every module of the course - published or not - exactly once; if the
    course changed since the caller last looked (409), nothing is written.
    """
    course = await _course_or_404(session, course_id, for_update=True)
    _require_open(course)
    current = await training_repo.list_modules(session, course_id)
    if reorder_mismatch([m.id for m in current], module_ids):
        raise ConflictError(
            "The course's modules have changed. Reload the list and send every module once."
        )
    if [m.id for m in current] != list(module_ids):
        await training_repo.apply_order(session, course_id=course_id, module_ids=module_ids)
        await _audit(
            session,
            actor_id=actor_id,
            action="training.module.reordered",
            entity_type=AUDIT_COURSE,
            entity_id=course_id,
            context={"module_ids": [str(i) for i in module_ids]},
        )
    return await training_repo.list_modules(session, course_id)


# --- the video itself ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModuleVideoOpen:
    module: ModuleRecord
    stream: ObjectStream | None
    unsatisfiable_size: int | None = None


async def open_module_video(
    session: AsyncSession,
    storage: Storage,
    *,
    module_id: UUID,
    range_header: str | None,
) -> ModuleVideoOpen:
    """Open a published module's **own** stored video (or one byte range of it) for streaming.

    The only place a module's video is opened. It reads the module's snapshot columns and nothing
    of the render it came from, and refuses a key outside the module's own prefix. Who may call
    it is the caller's business: the administrator's preview checks the role, the employee route
    (Piece 3) will apply ``decide_module_access`` first. Raises 404 for a module that does not
    exist (``MODULE_NOT_FOUND``) and for one with no video to play.
    """
    module = await get_module(session, module_id=module_id)
    location = _object_of(module)
    if module.status != MODULE_PUBLISHED or location is None:
        raise NotFoundError(MODULE_VIDEO_NOT_FOUND)
    if not belongs_to_module(location.key, module.id):
        log.error("training.module_video_key_foreign", module_id=str(module.id), key=location.key)
        raise NotFoundError(MODULE_VIDEO_NOT_FOUND)
    try:
        stream = await storage.open_object(
            bucket=location.bucket, key=location.key, range_header=range_header
        )
    except ObjectNotFoundError as exc:
        log.error("training.module_video_missing", module_id=str(module.id), key=location.key)
        raise NotFoundError(MODULE_VIDEO_NOT_FOUND) from exc
    except InvalidRangeError as exc:
        return ModuleVideoOpen(module=module, stream=None, unsatisfiable_size=exc.size)
    return ModuleVideoOpen(module=module, stream=stream)
