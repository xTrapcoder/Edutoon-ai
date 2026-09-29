"""Video rendering - service layer (Phase 10).

- ``enqueue_render`` - the caller-facing entry point. Verifies project ownership, refuses a
  second concurrent render for the project (409), then runs the **prerequisite gate** before
  any expensive work: the script is approved, every segment has ready, current (not stale)
  narration audio, ``measured_duration_ms`` is set, the storyboard is complete and fresh for
  every segment, every scene's timing and content are valid, and the video is within the
  maximum length. It also checks there is disk space for the work. Every refusal names what to
  do about it. Idempotent on the caller's ``Idempotency-Key``.
- ``retry_render`` - queue the same render again after a failed one; chunks already rendered
  are reused, so a retry only does the work that was left.
- ``run_render_video_job`` - what the ``worker`` process calls after claiming the job. It plans
  the render (``domain.render.build_render_plan``), keeps every stored chunk that is still
  valid, renders the rest - one chunk per scene, each uploaded and recorded in its own
  short transaction the moment it finishes - and, only when every chunk exists, assembles the
  final MP4, verifies it with ``ffprobe`` and publishes it. A chunk that fails does not stop
  the others or discard any finished work; the job then ends ``failed`` naming how many
  sections need another attempt, and a retry renders only those.
- ``get_render_status`` - the read model: per-scene state (ready / needs render / stale /
  failed), completeness, the current video, the latest job and its real progress.
- ``open_render_output`` - streams the finished MP4 (with HTTP ``Range`` support) without
  reading it into memory.

**Why partial re-rendering is a property of the design, not a feature.** A chunk is stored
under a fingerprint of exactly the inputs that decide its bytes (``domain.render``): editing one
scene, re-narrating one segment, or changing one topic's content changes only the fingerprints
of the scenes it touches, so the next render finds every other chunk already stored and skips
it. Narration is never re-synthesised here, and the audio track is rebuilt from the segments'
existing audio at assembly time.

Architecture (CLAUDE.md rules 1, 2, 3, 9): this module is the only layer allowed to drive the
renderer and storage for videos; it never issues SQL itself, going through ``repositories``;
every path is project + owner scoped and a resource the caller does not own is a 404. Storage
keys are built from the project id and validated values only, and every key read back from a
row is checked to lie inside the project's own prefix before it is used.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from edutoon.core.config import get_settings
from edutoon.core.errors import (
    ConflictError,
    InsufficientStorageError,
    JobError,
    NotFoundError,
    ValidationError,
)
from edutoon.domain import render as render_domain
from edutoon.domain.generation_settings import ORIENTATIONS
from edutoon.domain.render import PlannedChunk, RenderPlan, RenderPlanError, SceneState
from edutoon.domain.script_audio import compute_content_hash
from edutoon.domain.source_storage import belongs_to_project
from edutoon.providers import tts as tts_provider
from edutoon.providers.renderer import (
    AssemblySpec,
    ChunkFile,
    ChunkSpec,
    NarrationPlacement,
    Renderer,
)
from edutoon.providers.storage import (
    InvalidRangeError,
    ObjectNotFoundError,
    ObjectStream,
    Storage,
)
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import render as render_repo
from edutoon.repositories import script_segment_audio as script_segment_audio_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories.jobs import JobRecord
from edutoon.repositories.projects import ProjectRecord
from edutoon.repositories.render import RenderOutputRecord
from edutoon.repositories.script_segment_audio import ScriptSegmentAudioRecord
from edutoon.repositories.scripts import ScriptRecord
from edutoon.services import audit_logs as audit_logs_service
from edutoon.services import projects as projects_service
from edutoon.services import script_audio as script_audio_service
from edutoon.services import storyboard as storyboard_service
from edutoon.services.job_errors import describe_job_failure, public_job_error

log = structlog.get_logger()

RENDER_VIDEO_JOB_KIND = "render_video"

AUDIT_ENTITY_TYPE = "render"
AUDIT_ACTION_ENQUEUED = "render.enqueued"
AUDIT_ACTION_RECOVERED = "render.recovered"

VIDEO_CONTENT_TYPE = "video/mp4"

STALE_RENDER_ERROR = (
    "The worker stopped before finishing this render, so it was marked as failed. "
    "Every section already rendered is kept - render again to finish the rest."
)

# How many stored chunks are checked against object storage at once when a render starts.
_VERIFY_PARALLELISM = 16


# --- read/write models -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RenderEstimate:
    """What a render will involve, shown *before* it is queued. ``chunks_to_render`` is what
    is not already stored; ``chunks_reusable`` is what a re-render would skip."""

    chunk_count: int
    unique_chunk_count: int
    chunks_reusable: int
    chunks_to_render: int
    total_ms: int
    width: int
    height: int
    fps: int
    estimated_output_bytes: int
    required_free_bytes: int


@dataclass(frozen=True, slots=True)
class RenderEnqueued:
    job: JobRecord
    replayed: bool
    estimate: RenderEstimate


@dataclass(frozen=True, slots=True)
class RenderSceneStatus:
    position: int
    segment_id: UUID
    segment_index: int
    scene_index: int
    scene_count: int
    template: str
    start_ms: int
    duration_ms: int
    chunk_hash: str
    state: SceneState


@dataclass(frozen=True, slots=True)
class RenderStatus:
    script: ScriptRecord
    orientation: str
    quality: str
    job: JobRecord | None
    blockers: tuple[str, ...]
    scenes: tuple[RenderSceneStatus, ...]
    ready_count: int
    needs_render_count: int
    stale_count: int
    failed_count: int
    output: RenderOutputRecord | None
    output_is_current: bool
    estimate: RenderEstimate | None
    total_ms: int | None
    is_complete: bool


@dataclass(frozen=True, slots=True)
class RenderVideoOpen:
    """The result of opening the finished video: the stream (or, for a byte range past the
    end of the file, ``unsatisfiable_size`` - the file's real length, for the 416 response)."""

    output: RenderOutputRecord
    stream: ObjectStream | None
    unsatisfiable_size: int | None = None


@dataclass(frozen=True, slots=True)
class _Blocker:
    kind: str  # "conflict" | "validation"
    message: str

    def as_error(self) -> ConflictError | ValidationError:
        return (
            ConflictError(self.message)
            if self.kind == "conflict"
            else ValidationError(self.message)
        )


@dataclass(frozen=True, slots=True)
class _Evaluation:
    plan: RenderPlan | None
    blocker: _Blocker | None
    audio: dict[UUID, ScriptSegmentAudioRecord]
    scene_counts: dict[str, int]


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else plural or singular + 's'}"


# --- the prerequisite gate ---------------------------------------------------------------


async def _evaluate(
    session: AsyncSession,
    *,
    project: ProjectRecord,
    script: ScriptRecord,
    orientation: str,
    quality: str,
) -> _Evaluation:
    """Check every render prerequisite and, if they all hold, build the plan. Reads only; the
    first thing that is wrong comes back as a :class:`_Blocker` written for the owner."""
    settings = get_settings()
    profile = render_domain.profile_for(quality)

    def blocked(
        kind: str, message: str, audio: dict[UUID, ScriptSegmentAudioRecord] | None = None
    ) -> _Evaluation:
        return _Evaluation(None, _Blocker(kind, message), audio or {}, {})

    if script.status != "approved":
        return blocked(
            "conflict", f"Only an approved script can be rendered; this one is '{script.status}'."
        )
    if script.measured_duration_ms is None:
        return blocked(
            "validation",
            "This script's narration is not fully recorded yet - narrate every section before "
            "rendering.",
        )

    segments = await scripts_repo.list_segments(session, project_id=project.id, script_id=script.id)
    if not segments:
        return blocked("validation", "This script has no sections to render.")

    audio_by_segment = {
        audio.script_segment_id: audio
        for audio in await script_segment_audio_repo.list_by_script(
            session, project_id=project.id, script_id=script.id
        )
    }

    voice = script_audio_service.voice_selection_from_settings(settings, project.language)
    engine_version = tts_provider.engine_version()
    missing: list[int] = []
    stale: list[int] = []
    unsupported: list[int] = []
    for segment in segments:
        audio = audio_by_segment.get(segment.id)
        if (
            audio is None
            or audio.status != "ready"
            or not audio.storage_bucket
            or not audio.storage_key
            or not audio.measured_duration_ms
            or not belongs_to_project(audio.storage_key, project.id)
        ):
            missing.append(segment.segment_index + 1)
            continue
        expected_hash = compute_content_hash(
            narration_text=segment.narration_text,
            voice_model=voice.voice_model,
            engine_version=engine_version,
            sample_rate=audio.sample_rate or 0,
        )
        if audio.content_hash != expected_hash:
            stale.append(segment.segment_index + 1)
        elif (audio.sample_rate, audio.channels) != (
            profile.audio_sample_rate,
            profile.audio_channels,
        ):
            unsupported.append(segment.segment_index + 1)
    if missing:
        return blocked(
            "validation",
            f"{_plural(len(missing), 'section')} ({_numbers(missing)}) "
            f"{'has' if len(missing) == 1 else 'have'} no narration audio yet - narrate the "
            "script first.",
            audio_by_segment,
        )
    if stale:
        return blocked(
            "validation",
            f"The narration for {_plural(len(stale), 'section')} ({_numbers(stale)}) is out of "
            "date - its text or voice changed after it was recorded. Narrate the script again.",
            audio_by_segment,
        )
    if unsupported:
        return blocked(
            "validation",
            f"The narration for {_plural(len(unsupported), 'section')} ({_numbers(unsupported)}) "
            f"is not mono {profile.audio_sample_rate} Hz audio, which is what rendering uses. "
            "Narrate the script again.",
            audio_by_segment,
        )
    total_audio_ms = sum(audio_by_segment[s.id].measured_duration_ms or 0 for s in segments)
    if total_audio_ms != script.measured_duration_ms:
        return blocked(
            "validation",
            "The script's recorded narration length no longer matches its sections - narrate "
            "the script again.",
            audio_by_segment,
        )

    storyboard = await storyboard_service.get_storyboard_status(
        session, project_id=project.id, owner_id=project.owner_id, script_id=script.id
    )
    if not storyboard.is_complete:
        count = len(storyboard.not_ready_segment_ids)
        return blocked(
            "validation",
            f"The storyboard is incomplete: {_plural(count, 'section')} still "
            f"{'needs' if count == 1 else 'need'} scenes (or are stale after re-narration). "
            "Generate the storyboard first.",
            audio_by_segment,
        )

    inputs = [
        render_domain.SegmentInput(
            segment_id=str(detail.segment.id),
            segment_index=detail.segment.segment_index,
            audio_duration_ms=detail.audio.measured_duration_ms or 0 if detail.audio else 0,
            audio_content_hash=detail.audio.content_hash if detail.audio else "",
            scenes=[
                render_domain.SceneInput(
                    scene_index=scene.scene.scene_index,
                    start_ms=scene.scene.start_ms,
                    duration_ms=scene.scene.duration_ms,
                    template=scene.scene.template,
                    content=scene.scene.content,
                )
                for scene in detail.scenes
            ],
        )
        for detail in storyboard.segments
    ]
    scene_counts = {str(detail.segment.id): len(detail.scenes) for detail in storyboard.segments}
    try:
        plan = render_domain.build_render_plan(
            inputs,
            orientation=orientation,
            quality=quality,
            encoder=settings.RENDER_VIDEO_ENCODER,
        )
    except RenderPlanError as exc:
        return blocked("validation", str(exc), audio_by_segment)
    if plan.total_ms > settings.MAX_RENDER_DURATION_MS:
        limit_minutes = settings.MAX_RENDER_DURATION_MS / 60_000
        return blocked(
            "validation",
            f"This video runs {plan.total_ms / 60_000:.1f} minutes; the current maximum for one "
            f"render is {limit_minutes:g} minutes. Shorten the script or split the project.",
            audio_by_segment,
        )
    return _Evaluation(plan, None, audio_by_segment, scene_counts)


def _numbers(indices: list[int]) -> str:
    shown = ", ".join(str(i) for i in indices[:5])
    return shown + (", ..." if len(indices) > 5 else "")


def _estimate(plan: RenderPlan, reusable_hashes: set[str]) -> RenderEstimate:
    to_render = render_domain.chunks_to_render(plan, reusable_hashes)
    unique = len(set(plan.chunk_hashes))
    return RenderEstimate(
        chunk_count=len(plan.chunks),
        unique_chunk_count=unique,
        chunks_reusable=unique - len(to_render),
        chunks_to_render=len(to_render),
        total_ms=plan.total_ms,
        width=plan.width,
        height=plan.height,
        fps=plan.fps,
        estimated_output_bytes=render_domain.estimate_video_bytes(plan.total_ms, plan.quality),
        required_free_bytes=render_domain.estimate_work_bytes(plan),
    )


def _work_dir_root() -> Path:
    configured = get_settings().RENDER_WORK_DIR
    root = Path(configured) if configured else Path(tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ensure_disk_space(required_bytes: int) -> None:
    """Refuse cleanly, before any work, when the scratch disk cannot hold the render."""
    settings = get_settings()
    needed = required_bytes + settings.RENDER_MIN_FREE_DISK_MB * 1024 * 1024
    free = shutil.disk_usage(_work_dir_root()).free
    if free < needed:
        raise InsufficientStorageError(
            "There is not enough free disk space on the video server to render this right now "
            f"(about {needed // (1024 * 1024)} MB needed, {free // (1024 * 1024)} MB free). "
            "Try again once space has been freed."
        )


# --- enqueue -------------------------------------------------------------------------------


async def enqueue_render(
    session: AsyncSession,
    *,
    project_id: UUID,
    owner_id: UUID,
    script_id: UUID,
    orientation: str,
    quality: str,
    idempotency_key: str,
) -> RenderEnqueued:
    """Queue a render of ``script_id`` in ``orientation`` at ``quality``.

    Raises :class:`~edutoon.core.errors.NotFoundError` if the project isn't owned by
    ``owner_id`` or the script doesn't belong to it (rule 9);
    :class:`~edutoon.core.errors.ConflictError` if a render is already queued or running for the
    project, or the script is not approved; :class:`~edutoon.core.errors.ValidationError` if any
    other prerequisite is missing (narration, storyboard, timings, length);
    :class:`~edutoon.core.errors.InsufficientStorageError` if there is not room to render. A
    repeat call with the same ``idempotency_key`` returns the original job with
    ``replayed=True``.
    """
    project = await projects_service.get_project(session, project_id=project_id, owner_id=owner_id)

    existing_job = await jobs_repo.get_by_idempotency_key(
        session,
        project_id=project_id,
        kind=RENDER_VIDEO_JOB_KIND,
        idempotency_key=idempotency_key,
    )
    if existing_job is not None:
        return RenderEnqueued(
            job=existing_job, replayed=True, estimate=_estimate_from_payload(existing_job.payload)
        )

    await jobs_repo.lock_project_kind(session, project_id=project_id, kind=RENDER_VIDEO_JOB_KIND)
    await _recover_stale_renders(session, project_id=project_id)
    active = await jobs_repo.get_active_by_project_kind(
        session, project_id=project_id, kind=RENDER_VIDEO_JOB_KIND
    )
    if active is not None:
        raise ConflictError(
            "A video render is already queued or running for this project; wait for it to finish."
        )

    if orientation not in ORIENTATIONS:
        raise ValidationError(f"Unknown orientation {orientation!r}.")
    if quality not in render_domain.QUALITIES:
        raise ValidationError(f"Unknown quality {quality!r}.")

    script = await scripts_repo.get_by_id_for_project(session, script_id, project_id)
    if script is None:
        raise NotFoundError(f"Script {script_id} not found.")

    evaluation = await _evaluate(
        session, project=project, script=script, orientation=orientation, quality=quality
    )
    if evaluation.blocker is not None:
        raise evaluation.blocker.as_error()
    plan = evaluation.plan
    assert plan is not None

    stored = await render_repo.chunks_by_hashes(
        session, project_id=project_id, hashes=set(plan.chunk_hashes)
    )
    estimate = _estimate(plan, set(stored))
    _ensure_disk_space(estimate.required_free_bytes)

    job = await jobs_repo.create(
        session,
        project_id=project_id,
        kind=RENDER_VIDEO_JOB_KIND,
        payload={
            "script_id": str(script_id),
            "project_id": str(project_id),
            "orientation": orientation,
            "quality": quality,
            "estimate": _estimate_to_payload(estimate),
        },
        idempotency_key=idempotency_key,
    )
    await audit_logs_service.record_audit_log(
        session,
        action=AUDIT_ACTION_ENQUEUED,
        entity_type=AUDIT_ENTITY_TYPE,
        entity_id=script_id,
        project_id=project_id,
        actor_id=owner_id,
        context={
            "job_id": str(job.id),
            "orientation": orientation,
            "quality": quality,
            "chunks_to_render": estimate.chunks_to_render,
        },
    )
    return RenderEnqueued(job=job, replayed=False, estimate=estimate)


def _estimate_to_payload(estimate: RenderEstimate) -> dict[str, int]:
    return {name: getattr(estimate, name) for name in RenderEstimate.__slots__}


def _estimate_from_payload(payload: dict[str, Any]) -> RenderEstimate:
    raw = payload.get("estimate")
    data = raw if isinstance(raw, dict) else {}
    return RenderEstimate(
        **{
            name: data.get(name, 0) if isinstance(data.get(name, 0), int) else 0
            for name in RenderEstimate.__slots__
        }
    )


async def retry_render(
    session: AsyncSession, *, project_id: UUID, owner_id: UUID, idempotency_key: str
) -> RenderEnqueued:
    """Queue the render that last failed, again. Finished chunks are reused, so this does only
    the work that was left. Raises :class:`~edutoon.core.errors.ConflictError` if the latest
    render did not fail (there is nothing to retry)."""
    await projects_service.get_project(session, project_id=project_id, owner_id=owner_id)
    latest = await jobs_repo.get_latest_by_project_kind(
        session, project_id=project_id, kind=RENDER_VIDEO_JOB_KIND
    )
    if latest is None or latest.status != "failed":
        raise ConflictError("There is no failed render to retry.")
    try:
        script_id = UUID(str(latest.payload["script_id"]))
        orientation = str(latest.payload["orientation"])
        quality = str(latest.payload["quality"])
    except (KeyError, ValueError) as exc:
        raise ConflictError("The last render can't be retried; start a new one.") from exc
    return await enqueue_render(
        session,
        project_id=project_id,
        owner_id=owner_id,
        script_id=script_id,
        orientation=orientation,
        quality=quality,
        idempotency_key=idempotency_key,
    )


async def _recover_stale_renders(session: AsyncSession, *, project_id: UUID) -> None:
    """Fail any ``render_video`` job a crashed worker left ``running``. Chunks it had finished
    are untouched (each was committed as it completed), so the next render resumes."""
    stale_after = get_settings().RENDER_STALE_AFTER_SECONDS
    recovered = await jobs_repo.fail_stale_running(
        session,
        project_id=project_id,
        kind=RENDER_VIDEO_JOB_KIND,
        stale_after=timedelta(seconds=stale_after),
        error=STALE_RENDER_ERROR,
    )
    for job in recovered:
        log.warning(
            "render.stale_recovered",
            job_id=str(job.id),
            project_id=str(project_id),
            started_at=job.started_at.isoformat() if job.started_at else None,
            stale_after_seconds=stale_after,
        )
        await audit_logs_service.record_audit_log(
            session,
            action=AUDIT_ACTION_RECOVERED,
            entity_type=AUDIT_ENTITY_TYPE,
            entity_id=job.id,
            project_id=project_id,
            actor_type="system",
            context={"job_id": str(job.id), "stale_after_seconds": stale_after},
        )


# --- run (background job) ------------------------------------------------------------------


async def run_render_video_job(
    session: AsyncSession,
    storage: Storage,
    renderer: Renderer,
    *,
    job_id: UUID,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> JobRecord:
    """Process one queued ``render_video`` job end to end.

    Marks it running and commits that at once (releasing the row's lock before the long work),
    renders whatever chunks are missing, assembles and publishes the video, and ends the job
    ``succeeded`` - or ``failed`` with a message written for the owner and a ``result`` that
    says what is left to do. Never raises for a render failure - only
    :class:`~edutoon.core.errors.NotFoundError` for a ``job_id`` that doesn't exist at all.
    """
    job = await jobs_repo.get_by_id(session, job_id)
    if job is None:
        raise NotFoundError(f"Job {job_id} not found.")

    running = await jobs_repo.update(
        session,
        job_id=job.id,
        status="running",
        started_at=datetime.now(UTC),
        attempts=job.attempts + 1,
    )
    assert running is not None  # just fetched by this id
    await session.commit()

    try:
        project_id = UUID(str(running.payload["project_id"]))
        script_id = UUID(str(running.payload["script_id"]))
        orientation = str(running.payload["orientation"])
        quality = str(running.payload["quality"])
        result = await _render(
            session,
            storage,
            renderer,
            project_id=project_id,
            script_id=script_id,
            orientation=orientation,
            quality=quality,
            job_id=running.id,
            payload=running.payload,
            sessionmaker=sessionmaker,
        )
    except Exception as exc:  # noqa: BLE001 - any setup failure fails the job, never raises
        await session.rollback()
        error = describe_job_failure(exc, job_id=running.id, kind=RENDER_VIDEO_JOB_KIND)
        return await _mark_job_failed(session, job_id=running.id, error=error)

    if result["outcome"] == "complete":
        return await _mark_job_succeeded(session, job_id=running.id, result=result)
    return await _mark_job_failed(
        session, job_id=running.id, error=str(result["error"]), result=result
    )


class _Db:
    """Short, independent database transactions for the render loop. With a ``sessionmaker``
    every write gets its own session and commit (so finished chunks survive whatever happens
    next); without one (tests) the job's own session is used. All access is serialised by a
    lock, so concurrent chunk tasks never share a session mid-operation."""

    def __init__(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession] | None
    ) -> None:
        self._session = session
        self._sessionmaker = sessionmaker
        self._lock = asyncio.Lock()

    async def run(self, fn: Any) -> Any:
        async with self._lock:
            if self._sessionmaker is None:
                result = await fn(self._session)
                await self._session.commit()
                return result
            async with self._sessionmaker() as short:
                result = await fn(short)
                await short.commit()
                return result


async def _render(
    session: AsyncSession,
    storage: Storage,
    renderer: Renderer,
    *,
    project_id: UUID,
    script_id: UUID,
    orientation: str,
    quality: str,
    job_id: UUID,
    payload: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession] | None,
) -> dict[str, Any]:
    db = _Db(session, sessionmaker)

    project = await projects_repo.get_by_id(session, project_id)
    if project is None:
        raise NotFoundError(f"Project {project_id} not found.")
    script = await scripts_repo.get_by_id_for_project(session, script_id, project_id)
    if script is None:
        raise NotFoundError(f"Script {script_id} not found.")

    evaluation = await _evaluate(
        session, project=project, script=script, orientation=orientation, quality=quality
    )
    if evaluation.blocker is not None:
        raise JobError(evaluation.blocker.message)
    plan = evaluation.plan
    assert plan is not None
    profile = render_domain.profile_for(quality)
    scene_counts = evaluation.scene_counts
    progress = _Progress(sessionmaker, job_id, payload, plan, scene_counts)

    await progress.report("checking_stored_sections")
    valid = await _valid_stored_chunks(db, storage, project_id, plan)
    to_render = render_domain.chunks_to_render(plan, set(valid))
    progress.start(
        to_render_count=len(to_render), reused=len(set(plan.chunk_hashes)) - len(to_render)
    )

    current = await db.run(
        lambda s: render_repo.get_output(
            s,
            project_id=project_id,
            script_id=script_id,
            orientation=orientation,
            quality=quality,
        )
    )
    if current is not None and current.output_hash == plan.output_hash and not to_render:
        try:
            size = await storage.object_size(bucket=current.storage_bucket, key=current.storage_key)
        except ObjectNotFoundError:
            size = -1
        if size == current.byte_size and belongs_to_project(current.storage_key, project_id):
            # Nothing has changed since this video was made and it is still in storage:
            # there is no work to do, and the answer is the video that already exists.
            return {
                **progress.summary(),
                "outcome": "complete",
                "output_reused": True,
                "duration_ms": current.duration_ms,
                "output_bytes": current.byte_size,
                "probe": current.probe,
            }

    _ensure_disk_space(render_domain.estimate_work_bytes(plan))

    workdir = _work_dir_root() / f"edutoon-render-{job_id}"
    await asyncio.to_thread(workdir.mkdir, parents=True, exist_ok=True)
    try:
        local: dict[str, Path] = {}
        failures = await _render_chunks(
            db, storage, renderer, plan, to_render, workdir, local, progress, project_id
        )
        if failures:
            count = len(failures)
            return {
                **progress.summary(),
                "outcome": "chunks_failed",
                "error": f"{_plural(count, 'render section')} "
                f"{'needs' if count == 1 else 'need'} another attempt. Everything already "
                "rendered is kept - try again to finish.",
                "failed_chunks": failures,
                "failed_chunk_hashes": sorted({f["chunk_hash"] for f in failures}),
            }

        await progress.report("assembling")
        assembly_started = time.perf_counter()
        try:
            summary = await _assemble_and_publish(
                db,
                storage,
                renderer,
                plan,
                evaluation.audio,
                workdir,
                local,
                profile,
                project_id=project_id,
                script_id=script_id,
                job_id=job_id,
                progress=progress,
            )
        except Exception as exc:  # noqa: BLE001 - chunks are safe; only assembly failed
            message = public_job_error(exc)
            log.error(
                "render.assembly_failed",
                job_id=str(job_id),
                error_type=type(exc).__name__,
                error=str(exc)[:2000],
            )
            return {
                **progress.summary(),
                "outcome": "assembly_failed",
                "error": f"The video could not be assembled ({message}) Every rendered "
                "section is kept - try again to assemble it.",
                "failed_chunks": [],
                "failed_chunk_hashes": [],
            }
        progress.finish_assembly()
        await progress.report("finalising")
        await _collect_garbage(db, storage, project_id, plan)
        return {
            **progress.summary(),
            "outcome": "complete",
            "assembly_seconds": round(time.perf_counter() - assembly_started, 2),
            **summary,
        }
    finally:
        await asyncio.to_thread(shutil.rmtree, workdir, True)


async def _valid_stored_chunks(
    db: _Db, storage: Storage, project_id: UUID, plan: RenderPlan
) -> dict[str, render_repo.RenderChunkRecord]:
    """The stored chunks that are genuinely usable: the row exists, its key lies inside the
    project's prefix, and the object is there with the size that was recorded. A row whose
    object is missing or the wrong size is forgotten so the chunk is rendered again - a damaged
    artifact is never treated as valid."""
    stored: dict[str, render_repo.RenderChunkRecord] = await db.run(
        lambda s: render_repo.chunks_by_hashes(
            s, project_id=project_id, hashes=set(plan.chunk_hashes)
        )
    )
    semaphore = asyncio.Semaphore(_VERIFY_PARALLELISM)

    async def check(record: render_repo.RenderChunkRecord) -> bool:
        if not belongs_to_project(record.storage_key, project_id):
            return False
        async with semaphore:
            try:
                size = await storage.object_size(
                    bucket=record.storage_bucket, key=record.storage_key
                )
            except ObjectNotFoundError:
                return False
        return size == record.byte_size

    verdicts = await asyncio.gather(*(check(record) for record in stored.values()))
    invalid = [record for record, ok in zip(stored.values(), verdicts, strict=True) if not ok]
    if invalid:
        log.warning(
            "render.invalid_chunks_forgotten", project_id=str(project_id), count=len(invalid)
        )
        await db.run(
            lambda s: render_repo.delete_chunks(
                s, project_id=project_id, chunk_ids=[r.id for r in invalid]
            )
        )
    bad = {record.chunk_hash for record in invalid}
    return {h: record for h, record in stored.items() if h not in bad}


async def _render_chunks(
    db: _Db,
    storage: Storage,
    renderer: Renderer,
    plan: RenderPlan,
    to_render: list[PlannedChunk],
    workdir: Path,
    local: dict[str, Path],
    progress: _Progress,
    project_id: UUID,
) -> list[dict[str, Any]]:
    """Render every missing chunk. Each is uploaded and recorded the moment it finishes; one
    failing never stops the others. Returns the failures (empty when all succeeded)."""
    settings = get_settings()
    concurrency = settings.RENDER_CONCURRENCY
    semaphore = asyncio.Semaphore(concurrency)
    profile = render_domain.profile_for(plan.quality)
    failures: list[dict[str, Any]] = []

    aborted = asyncio.Event()

    async def one(chunk: PlannedChunk) -> None:
        async with semaphore:
            if aborted.is_set():
                return  # the worker is going down: start nothing new
            await progress.chunk_started(chunk)
            dest = workdir / f"{chunk.chunk_hash}.mp4"
            try:
                rendered = await renderer.render_chunk(
                    ChunkSpec(
                        template=chunk.template,
                        content=chunk.content,
                        orientation=plan.orientation,
                        width=plan.width,
                        height=plan.height,
                        fps=plan.fps,
                        duration_ms=chunk.duration_ms,
                        canonical_frames=chunk.canonical_frame_count,
                        encoder=plan.encoder,
                        profile=profile,
                    ),
                    dest,
                )
                key = render_domain.chunk_storage_key(
                    project_id, plan.orientation, plan.quality, chunk.chunk_hash
                )
                await storage.upload_file(
                    bucket=settings.BUCKET_SEGMENTS,
                    key=key,
                    path=dest,
                    content_type=VIDEO_CONTENT_TYPE,
                )
                await db.run(
                    lambda s: render_repo.add_chunk(
                        s,
                        project_id=project_id,
                        chunk_hash=chunk.chunk_hash,
                        orientation=plan.orientation,
                        quality=plan.quality,
                        renderer_version=render_domain.RENDERER_VERSION,
                        storage_bucket=settings.BUCKET_SEGMENTS,
                        storage_key=key,
                        byte_size=rendered.byte_size,
                        frame_count=rendered.frame_count,
                        duration_ms=chunk.duration_ms,
                    )
                )
                local[chunk.chunk_hash] = dest
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    aborted.set()  # cancellation or process shutdown: let nothing else begin
                    dest.unlink(missing_ok=True)
                    raise
                dest.unlink(missing_ok=True)
                failures.append(
                    {
                        "position": chunk.position,
                        "segment_id": chunk.segment_id,
                        "scene_index": chunk.scene_index,
                        "chunk_hash": chunk.chunk_hash,
                        "error": public_job_error(exc),
                    }
                )
                log.warning(
                    "render.chunk_failed",
                    position=chunk.position,
                    error_type=type(exc).__name__,
                    error=str(exc)[:1000],
                )
            await progress.chunk_finished()

    await progress.report("rendering")
    tasks = [asyncio.ensure_future(one(chunk)) for chunk in to_render]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        # The worker is being stopped (or something outside a chunk failed): do not leave the
        # other chunk tasks running unsupervised.
        for task in tasks:
            task.cancel()
        raise
    failures.sort(key=lambda f: f["position"])
    return failures


async def _assemble_and_publish(
    db: _Db,
    storage: Storage,
    renderer: Renderer,
    plan: RenderPlan,
    audio: dict[UUID, ScriptSegmentAudioRecord],
    workdir: Path,
    local: dict[str, Path],
    profile: render_domain.EncodingProfile,
    *,
    project_id: UUID,
    script_id: UUID,
    job_id: UUID,
    progress: _Progress,
) -> dict[str, Any]:
    settings = get_settings()

    async def fetch(bucket: str, key: str, dest: Path) -> None:
        if not belongs_to_project(key, project_id):
            raise JobError("A stored file is outside this project.")
        await storage.download_file(bucket=bucket, key=key, dest=dest)

    stored = await db.run(
        lambda s: render_repo.chunks_by_hashes(
            s, project_id=project_id, hashes=set(plan.chunk_hashes)
        )
    )
    downloads = asyncio.Semaphore(_VERIFY_PARALLELISM)

    async def fetch_chunk(chunk_hash: str) -> None:
        record = stored.get(chunk_hash)
        if record is None:
            raise JobError("A rendered section went missing before assembly.")
        dest = workdir / f"{chunk_hash}.mp4"
        async with downloads:
            await fetch(record.storage_bucket, record.storage_key, dest)
        local[chunk_hash] = dest

    await asyncio.gather(
        *(fetch_chunk(h) for h in dict.fromkeys(plan.chunk_hashes) if h not in local)
    )

    placements: list[NarrationPlacement] = []
    audio_by_id = {str(segment_id): record for segment_id, record in audio.items()}

    async def fetch_narration(narration: render_domain.PlannedNarration) -> NarrationPlacement:
        record = audio_by_id[narration.segment_id]
        assert record.storage_bucket and record.storage_key
        dest = workdir / f"narration-{narration.segment_index}.wav"
        async with downloads:
            await fetch(record.storage_bucket, record.storage_key, dest)
        return NarrationPlacement(
            dest, narration.start_ms, narration.start_ms + narration.duration_ms
        )

    placements = list(await asyncio.gather(*(fetch_narration(n) for n in plan.narration)))

    final = workdir / "final.mp4"
    await renderer.assemble(
        AssemblySpec(
            chunks=[
                ChunkFile(local[c.chunk_hash], c.frame_count, c.canonical_frame_count)
                for c in plan.chunks
            ],
            narration=placements,
            total_ms=plan.total_ms,
            fps=plan.fps,
            encoder=plan.encoder,
            profile=profile,
        ),
        final,
    )

    await progress.report("verifying")
    probe = await renderer.probe(final)
    problems = render_domain.check_output(probe.summary(), plan)
    if problems:
        log.error("render.output_failed_verification", job_id=str(job_id), problems=problems)
        raise JobError("The finished video did not pass its checks.")

    await progress.report("publishing")
    key = render_domain.output_storage_key(project_id, script_id, plan.orientation, plan.quality)
    await storage.upload_file(
        bucket=settings.BUCKET_OUTPUTS, key=key, path=final, content_type=VIDEO_CONTENT_TYPE
    )
    summary = probe.summary()
    await db.run(
        lambda s: render_repo.replace_output(
            s,
            project_id=project_id,
            script_id=script_id,
            job_id=job_id,
            orientation=plan.orientation,
            quality=plan.quality,
            output_hash=plan.output_hash,
            renderer_version=render_domain.RENDERER_VERSION,
            encoder=plan.encoder,
            fps=plan.fps,
            width=plan.width,
            height=plan.height,
            duration_ms=plan.total_ms,
            byte_size=probe.size_bytes,
            storage_bucket=settings.BUCKET_OUTPUTS,
            storage_key=key,
            probe=summary,
            scenes=[
                render_repo.OutputScene(
                    position=c.position,
                    script_segment_id=UUID(c.segment_id),
                    scene_index=c.scene_index,
                    chunk_hash=c.chunk_hash,
                )
                for c in plan.chunks
            ],
        )
    )
    return {
        "output_hash": plan.output_hash,
        "duration_ms": plan.total_ms,
        "output_bytes": probe.size_bytes,
        "probe": summary,
    }


async def _collect_garbage(db: _Db, storage: Storage, project_id: UUID, plan: RenderPlan) -> None:
    """Forget chunks of this configuration that no finished video uses any more (best effort:
    a failure here never fails a render that has already succeeded)."""
    try:
        stale = await db.run(
            lambda s: render_repo.list_unreferenced_chunks(
                s,
                project_id=project_id,
                orientation=plan.orientation,
                quality=plan.quality,
                keep_hashes=set(plan.chunk_hashes),
            )
        )
        removable = [c for c in stale if belongs_to_project(c.storage_key, project_id)]
        for chunk in removable:
            try:
                await storage.delete_object(bucket=chunk.storage_bucket, key=chunk.storage_key)
            except Exception as exc:  # noqa: BLE001 - leave the row; it is retried next time
                log.warning("render.gc_delete_failed", key=chunk.storage_key, error=str(exc))
                continue
            await db.run(
                lambda s, chunk=chunk: render_repo.delete_chunks(
                    s, project_id=project_id, chunk_ids=[chunk.id]
                )
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("render.gc_failed", project_id=str(project_id), error=str(exc)[:500])


class _Progress:
    """Real progress for one render job, written through short independent sessions (never the
    job's own session) so a reader sees it while the job runs. Percentages are honest work
    units: each chunk still to render, plus one for assembly."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession] | None,
        job_id: UUID,
        base_payload: dict[str, Any],
        plan: RenderPlan,
        scene_counts: dict[str, int],
    ) -> None:
        self._sessionmaker = sessionmaker
        self._job_id = job_id
        self._base = base_payload
        self._plan = plan
        self._scene_counts = scene_counts
        self._segment_count = len(plan.narration)
        self._total = len(set(plan.chunk_hashes))
        self._to_render = 0
        self._reused = 0
        self._done = 0
        self._assembled = False
        self._started_at = time.monotonic()
        self._current: PlannedChunk | None = None
        self._stage = "preparing"

    def start(self, *, to_render_count: int, reused: int) -> None:
        self._to_render = to_render_count
        self._reused = reused
        self._started_at = time.monotonic()

    def finish_assembly(self) -> None:
        self._assembled = True

    def _percent(self) -> int:
        units = self._to_render + 1
        return round(100 * (self._done + (1 if self._assembled else 0)) / units)

    def _eta(self) -> int | None:
        if self._done < 2 or self._done >= self._to_render:
            return None
        elapsed = time.monotonic() - self._started_at
        return round(elapsed / self._done * (self._to_render - self._done))

    def snapshot(self) -> dict[str, Any]:
        current = self._current
        return {
            "stage": self._stage,
            "chunks_total": self._total,
            "chunks_reused": self._reused,
            "chunks_to_render": self._to_render,
            "chunks_done": self._done,
            "chunks_complete": self._reused + self._done,
            "segment_index": current.segment_index if current else None,
            "segment_count": self._segment_count,
            "scene_index": current.scene_index if current else None,
            "scene_count": self._scene_counts.get(current.segment_id) if current else None,
            "percent": self._percent(),
            "eta_seconds": self._eta(),
            "updated_at": datetime.now(UTC).isoformat(),
        }

    def summary(self) -> dict[str, Any]:
        return {
            "chunk_total": self._total,
            "chunks_reused": self._reused,
            "chunks_rendered": self._done,
            "script_id": self._base.get("script_id"),
            "orientation": self._plan.orientation,
            "quality": self._plan.quality,
            "output_hash": self._plan.output_hash,
        }

    async def report(self, stage: str) -> None:
        self._stage = stage
        await self._write()

    async def chunk_started(self, chunk: PlannedChunk) -> None:
        self._current = chunk
        await self._write()

    async def chunk_finished(self) -> None:
        self._done += 1
        await self._write()

    async def _write(self) -> None:
        if self._sessionmaker is None:
            return
        async with self._sessionmaker() as short:
            await jobs_repo.update(
                short,
                job_id=self._job_id,
                payload={**self._base, "progress": self.snapshot()},
            )
            await short.commit()


async def _mark_job_succeeded(
    session: AsyncSession, *, job_id: UUID, result: dict[str, Any]
) -> JobRecord:
    updated = await jobs_repo.update(
        session, job_id=job_id, status="succeeded", finished_at=datetime.now(UTC), result=result
    )
    assert updated is not None
    return updated


async def _mark_job_failed(
    session: AsyncSession, *, job_id: UUID, error: str, result: dict[str, Any] | None = None
) -> JobRecord:
    updated = await jobs_repo.update(
        session,
        job_id=job_id,
        status="failed",
        finished_at=datetime.now(UTC),
        error=error,
        **({"result": result} if result is not None else {}),
    )
    assert updated is not None
    return updated


# --- read model ------------------------------------------------------------------------------


async def get_render_status(
    session: AsyncSession,
    *,
    project_id: UUID,
    owner_id: UUID,
    script_id: UUID,
    orientation: str,
    quality: str,
) -> RenderStatus:
    """A script's render status for one ``(orientation, quality)``: every scene's state, the
    current video and whether it is up to date, and the latest render job. Ownership-scoped
    (rule 9). Completeness is computed fresh from fingerprints - never copied from a job's own
    historical result - so the workspace can never show a finished-looking video that is out of
    date or missing sections."""
    project = await projects_service.get_project(session, project_id=project_id, owner_id=owner_id)
    script = await scripts_repo.get_by_id_for_project(session, script_id, project_id)
    if script is None:
        raise NotFoundError(f"Script {script_id} not found.")
    if orientation not in ORIENTATIONS or quality not in render_domain.QUALITIES:
        raise ValidationError("Unknown orientation or quality.")

    latest_job = await jobs_repo.get_latest_by_project_kind(
        session, project_id=project_id, kind=RENDER_VIDEO_JOB_KIND
    )
    output = await render_repo.get_output(
        session,
        project_id=project_id,
        script_id=script_id,
        orientation=orientation,
        quality=quality,
    )

    evaluation = await _evaluate(
        session, project=project, script=script, orientation=orientation, quality=quality
    )
    plan = evaluation.plan
    if plan is None:
        return RenderStatus(
            script=script,
            orientation=orientation,
            quality=quality,
            job=latest_job,
            blockers=(evaluation.blocker.message,) if evaluation.blocker else (),
            scenes=(),
            ready_count=0,
            needs_render_count=0,
            stale_count=0,
            failed_count=0,
            output=output,
            output_is_current=False,
            estimate=None,
            total_ms=None,
            is_complete=False,
        )

    stored = await render_repo.chunks_by_hashes(
        session, project_id=project_id, hashes=set(plan.chunk_hashes)
    )
    last_by_scene: dict[tuple[UUID, int], str] = {}
    if output is not None:
        for scene in await render_repo.list_output_scenes(session, output_id=output.id):
            last_by_scene[(scene.script_segment_id, scene.scene_index)] = scene.chunk_hash
    failed_hashes: set[str] = set()
    if latest_job is not None and latest_job.status == "failed" and latest_job.result:
        raw = latest_job.result.get("failed_chunk_hashes")
        if isinstance(raw, list):
            failed_hashes = {h for h in raw if isinstance(h, str)}

    scenes: list[RenderSceneStatus] = []
    for chunk in plan.chunks:
        state = render_domain.classify_scene(
            current_hash=chunk.chunk_hash,
            chunk_is_ready=chunk.chunk_hash in stored,
            last_output_hash=last_by_scene.get((UUID(chunk.segment_id), chunk.scene_index)),
            failed=chunk.chunk_hash in failed_hashes,
        )
        scenes.append(
            RenderSceneStatus(
                position=chunk.position,
                segment_id=UUID(chunk.segment_id),
                segment_index=chunk.segment_index,
                scene_index=chunk.scene_index,
                scene_count=evaluation.scene_counts.get(chunk.segment_id, 0),
                template=chunk.template,
                start_ms=chunk.start_ms,
                duration_ms=chunk.duration_ms,
                chunk_hash=chunk.chunk_hash,
                state=state,
            )
        )
    counts = {
        state: sum(1 for s in scenes if s.state == state)
        for state in ("ready", "needs_render", "stale", "failed")
    }
    output_is_current = output is not None and output.output_hash == plan.output_hash
    return RenderStatus(
        script=script,
        orientation=orientation,
        quality=quality,
        job=latest_job,
        blockers=(),
        scenes=tuple(scenes),
        ready_count=counts["ready"],
        needs_render_count=counts["needs_render"],
        stale_count=counts["stale"],
        failed_count=counts["failed"],
        output=output,
        output_is_current=output_is_current,
        estimate=_estimate(plan, set(stored)),
        total_ms=plan.total_ms,
        is_complete=output_is_current,
    )


async def open_render_output(
    session: AsyncSession,
    storage: Storage,
    *,
    project_id: UUID,
    owner_id: UUID,
    script_id: UUID,
    orientation: str,
    quality: str,
    range_header: str | None,
) -> RenderVideoOpen:
    """The finished video (or one byte range of it), opened for streaming. Raises
    :class:`~edutoon.core.errors.NotFoundError` if the project isn't owned by ``owner_id``, the
    script isn't the project's, or no video has been rendered (rule 9: all look identical). A
    range that starts past the end of the file comes back as ``unsatisfiable_size`` (the router
    answers 416)."""
    await projects_service.get_project(session, project_id=project_id, owner_id=owner_id)
    script = await scripts_repo.get_by_id_for_project(session, script_id, project_id)
    if script is None:
        raise NotFoundError(f"Script {script_id} not found.")
    output = await render_repo.get_output(
        session,
        project_id=project_id,
        script_id=script_id,
        orientation=orientation,
        quality=quality,
    )
    if output is None or not belongs_to_project(output.storage_key, project_id):
        raise NotFoundError("No rendered video is available for this script yet.")
    try:
        stream = await storage.open_object(
            bucket=output.storage_bucket, key=output.storage_key, range_header=range_header
        )
    except ObjectNotFoundError as exc:
        raise NotFoundError("No rendered video is available for this script yet.") from exc
    except InvalidRangeError as exc:
        return RenderVideoOpen(output=output, stream=None, unsatisfiable_size=exc.size)
    return RenderVideoOpen(output=output, stream=stream)


async def get_render_progress(
    session: AsyncSession, *, project_id: UUID, owner_id: UUID
) -> JobRecord | None:
    """The project's latest ``render_video`` job (whatever its status), whose ``payload`` carries
    live progress. A single cheap query - the workspace polls this while a render runs, and
    reads the full :func:`get_render_status` only when the job changes state."""
    await projects_service.get_project(session, project_id=project_id, owner_id=owner_id)
    return await jobs_repo.get_latest_by_project_kind(
        session, project_id=project_id, kind=RENDER_VIDEO_JOB_KIND
    )


__all__ = [
    "RENDER_VIDEO_JOB_KIND",
    "RenderEnqueued",
    "RenderEstimate",
    "RenderSceneStatus",
    "RenderStatus",
    "RenderVideoOpen",
    "enqueue_render",
    "get_render_progress",
    "get_render_status",
    "open_render_output",
    "retry_render",
    "run_render_video_job",
]
