"""Scene / Storyboard layer - service layer (Phase 9).

- ``enqueue_storyboard_generation`` - the caller-facing entry point. Verifies
  project ownership, that the script belongs to the project, is
  ``approved`` and has fully measured narration
  (``scripts.measured_duration_ms IS NOT NULL`` - the *sole*
  narration-completeness gate, never ``estimated_duration_ms`` or
  ``target_duration_ms``), recovers any stale ``generate_storyboard`` job,
  refuses a second concurrent one for the project, then - before any
  provider call - computes every segment's real, exact scene-count bound
  from its own measured audio duration
  (``domain.storyboard_timing.effective_scene_count``) and rejects the
  request if the segment count or the resulting total scene count would
  exceed the configured caps. Idempotent on the caller's
  ``Idempotency-Key``, and returns the worst-case provider-call estimate
  the frontend shows *before* generation runs.
- ``run_generate_storyboard_job`` - what the ``worker`` process calls after
  claiming the job. Processes every segment in narration order: a segment
  whose current scene set already matches what is expected (same count,
  every scene's ``source_audio_content_hash`` matching the segment's
  *current* ``script_segment_audio.content_hash``) is skipped outright
  (resumability); a segment whose measured audio is too short for even one
  scene (``effective_scene_count`` returning ``0``) gets its scene set
  cleared, with no provider call; everything else gets exactly one
  ``LLM.compose_storyboard_scenes`` call, validated (slot contract,
  provenance) and persisted as a whole-set replacement in its own
  transaction the moment it finishes - one segment's failure never discards
  another's success, and never stops the rest of the job. The job itself
  always ends ``succeeded`` unless *every* segment failed; whether the
  storyboard is *complete* is a separate question the read model answers
  fresh from current state, never from a job's historical result.
  A scene's optional procedural ``visual`` (Phase 11) is handled here too: a *structurally*
  invalid visual - or one too big for its template - is dropped, never repaired: the scene is
  kept (drawn by the deterministic fallback), the job continues, the drop is logged and marked
  on the scene (``illustration.visual_dropped``), and the read model counts every such scene
  (``dropped_visual_count``) so a reviewer is told. Provenance is a different matter and stays
  a hard failure: fabricated text or a fabricated number anywhere in the model's output - in a
  visual that is about to be dropped as well - rejects the whole segment.
- ``get_storyboard_status`` - the read model the API's status route uses:
  per-segment scene sets, staleness, completeness, and both the worst-case
  estimate and (once a job has run) the actual provider calls it made.

Architecture (CLAUDE.md rules 1, 2, 3, 9): this module is the only
storyboard layer allowed to touch ``providers.llm``; it never issues SQL
itself, going through ``repositories`` for every read and write; every path
is project + owner scoped and a resource the caller doesn't own is a 404.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from edutoon.core.config import get_settings
from edutoon.core.errors import (
    ConflictError,
    JobAbandonedError,
    JobError,
    NotFoundError,
    ValidationError,
)
from edutoon.domain.llm_usage import UsageReport
from edutoon.domain.storyboard_provenance import has_lexical_overlap, require_provenance
from edutoon.domain.storyboard_template import validate_scene_content
from edutoon.domain.storyboard_timing import (
    SegmentTimingInput,
    allocate_scene_durations,
    effective_scene_count,
    scene_timings,
    segment_absolute_starts,
    timeline_positions,
)
from edutoon.domain.visual import visual_problem, without_invalid_visual
from edutoon.providers.llm import LLM, StoryboardSegmentBrief
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import script_segment_audio as script_segment_audio_repo
from edutoon.repositories import script_segment_scenes as scenes_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories.jobs import JobRecord
from edutoon.repositories.script_segment_audio import ScriptSegmentAudioRecord
from edutoon.repositories.script_segment_scenes import NewScene, ScriptSegmentSceneRecord
from edutoon.repositories.scripts import ScriptRecord, ScriptSegmentRecord
from edutoon.services import audit_logs as audit_logs_service
from edutoon.services import projects as projects_service
from edutoon.services import script_audio as script_audio_service
from edutoon.services.job_errors import describe_job_failure, public_job_error
from edutoon.services.llm_usage import new_usage_report, record_call

log = structlog.get_logger()

GENERATE_STORYBOARD_JOB_KIND = "generate_storyboard"

AUDIT_ENTITY_TYPE = "storyboard"
AUDIT_ACTION_ENQUEUED = "storyboard.generation_enqueued"
AUDIT_ACTION_RECOVERED = "storyboard.generation_recovered"

STALE_STORYBOARD_ERROR = (
    "The worker stopped before finishing this storyboard, so it was marked as failed. "
    "Any segment already storyboarded keeps its scenes - generate again to cover the rest."
)


@dataclass(frozen=True, slots=True)
class StoryboardEnqueued:
    job: JobRecord
    replayed: bool
    estimated_provider_calls: int


@dataclass(frozen=True, slots=True)
class SceneDetail:
    scene: ScriptSegmentSceneRecord
    # Where the scene starts on the whole timeline, computed on read from the *current*
    # narration (``domain.storyboard_timing.timeline_positions``) - never the scene row's
    # stored ``start_ms``, which an earlier segment's re-narration leaves out of date.
    start_ms: int
    # The soft provenance signal (spec section 14), recomputed fresh at read
    # time - never persisted, never a rejection: True means "worth a
    # reviewer's attention", not "wrong".
    reviewer_flag: bool


@dataclass(frozen=True, slots=True)
class SegmentStoryboardDetail:
    segment: ScriptSegmentRecord
    audio: ScriptSegmentAudioRecord | None
    expected_scene_count: int
    scenes: tuple[SceneDetail, ...]
    is_stale: bool
    is_ready: bool


@dataclass(frozen=True, slots=True)
class StoryboardStatus:
    script: ScriptRecord
    job: JobRecord | None
    segments: tuple[SegmentStoryboardDetail, ...]
    ready_segment_count: int
    not_ready_segment_ids: tuple[UUID, ...]
    is_complete: bool
    # ``True`` only when every timing input is final: no narration is being made right now
    # and every segment's narration and scenes are current. ``False`` means the start
    # positions shown are a best estimate that may move once narration finishes.
    timings_authoritative: bool
    # How many current scenes had a model-written visual dropped as invalid (they are drawn by
    # the text fallback). Counted fresh from the scenes themselves, never from a job's result,
    # so a reviewer is always told, and it drops back to 0 once those scenes are regenerated.
    dropped_visual_count: int
    estimated_provider_calls: int
    actual_provider_calls: int | None


# --- enqueue -----------------------------------------------------------------------


async def enqueue_storyboard_generation(
    session: AsyncSession,
    *,
    project_id: UUID,
    owner_id: UUID,
    script_id: UUID,
    idempotency_key: str,
) -> StoryboardEnqueued:
    """Queue storyboard generation for ``script_id``.

    Raises :class:`~edutoon.core.errors.NotFoundError` if the project isn't
    owned by ``owner_id``, or the script doesn't belong to it (rule 9).
    Raises :class:`~edutoon.core.errors.ConflictError` if the script is not
    ``approved``, or another storyboard generation is already queued or
    running for the project. Raises
    :class:`~edutoon.core.errors.ValidationError` if the script's narration
    is not fully measured yet, if it has more segments than
    ``MAX_STORYBOARD_SEGMENTS``, or if the exact total scene count computed
    from every segment's own measured duration would exceed
    ``MAX_SCENES_PER_SCRIPT`` - all of this before any provider call is
    made. A repeat call with the same ``idempotency_key`` returns the
    original job with ``replayed=True``.
    """
    await projects_service.get_project(session, project_id=project_id, owner_id=owner_id)

    existing_job = await jobs_repo.get_by_idempotency_key(
        session,
        project_id=project_id,
        kind=GENERATE_STORYBOARD_JOB_KIND,
        idempotency_key=idempotency_key,
    )
    if existing_job is not None:
        estimated = _int_or_zero(existing_job.payload.get("estimated_provider_calls"))
        return StoryboardEnqueued(
            job=existing_job, replayed=True, estimated_provider_calls=estimated
        )

    await jobs_repo.lock_project_kind(
        session, project_id=project_id, kind=GENERATE_STORYBOARD_JOB_KIND
    )
    await _recover_stale_storyboards(session, project_id=project_id)
    active = await jobs_repo.get_active_by_project_kind(
        session, project_id=project_id, kind=GENERATE_STORYBOARD_JOB_KIND
    )
    if active is not None:
        raise ConflictError(
            "Storyboard generation is already queued or running for this project; "
            "wait for it to finish."
        )

    script = await scripts_repo.get_by_id_for_project(session, script_id, project_id)
    if script is None:
        raise NotFoundError(f"Script {script_id} not found.")
    if script.status != "approved":
        raise ConflictError(
            f"Only an approved script can get a storyboard; this one is '{script.status}'."
        )
    if script.measured_duration_ms is None:
        raise ValidationError(
            "This script's narration is not fully synthesised yet - every segment needs "
            "ready audio before a storyboard can be generated."
        )

    settings = get_settings()
    segments = await scripts_repo.list_segments(session, project_id=project_id, script_id=script_id)
    if not segments:
        raise ValidationError("This script has no segments to storyboard.")
    if len(segments) > settings.MAX_STORYBOARD_SEGMENTS:
        raise ValidationError(
            f"This script has {len(segments)} segments; a storyboard may cover at most "
            f"{settings.MAX_STORYBOARD_SEGMENTS}."
        )

    audio_by_segment = await _audio_by_segment(session, project_id=project_id, script_id=script_id)
    not_ready = [
        segment
        for segment in segments
        if audio_by_segment.get(segment.id) is None
        or audio_by_segment[segment.id].status != "ready"
    ]
    if not_ready:
        # Defence in depth: measured_duration_ms already implies every segment has
        # ready audio (services.script_audio only sets it once every one does).
        raise ValidationError(f"{len(not_ready)} segment(s) do not have ready narration audio yet.")

    expected_counts = [
        effective_scene_count(
            audio_by_segment[segment.id].measured_duration_ms or 0,
            max_scenes_per_segment=settings.MAX_SCENES_PER_SEGMENT,
            min_scene_duration_ms=settings.MIN_SCENE_DURATION_MS,
        )
        for segment in segments
    ]
    total_scenes = sum(expected_counts)
    if total_scenes > settings.MAX_SCENES_PER_SCRIPT:
        raise ValidationError(
            f"This script would need {total_scenes} scenes; a storyboard may cover at "
            f"most {settings.MAX_SCENES_PER_SCRIPT}."
        )

    estimated_provider_calls = sum(1 for count in expected_counts if count > 0)

    job = await jobs_repo.create(
        session,
        project_id=project_id,
        kind=GENERATE_STORYBOARD_JOB_KIND,
        payload={
            "script_id": str(script_id),
            "project_id": str(project_id),
            "estimated_provider_calls": estimated_provider_calls,
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
            "segment_count": len(segments),
            "estimated_provider_calls": estimated_provider_calls,
        },
    )
    return StoryboardEnqueued(
        job=job, replayed=False, estimated_provider_calls=estimated_provider_calls
    )


def _int_or_zero(value: Any) -> int:
    return value if isinstance(value, int) else 0


async def _audio_by_segment(
    session: AsyncSession, *, project_id: UUID, script_id: UUID
) -> dict[UUID, ScriptSegmentAudioRecord]:
    return {
        audio.script_segment_id: audio
        for audio in await script_segment_audio_repo.list_by_script(
            session, project_id=project_id, script_id=script_id
        )
    }


async def _recover_stale_storyboards(session: AsyncSession, *, project_id: UUID) -> None:
    """Fail any ``generate_storyboard`` job a crashed worker left ``running``.
    Mirrors ``script_audio._recover_stale_synthesis`` exactly: already-valid
    scene sets are untouched, and a fresh call simply resumes.
    """
    stale_after = get_settings().STORYBOARD_STALE_AFTER_SECONDS
    recovered = await jobs_repo.fail_stale_running(
        session,
        project_id=project_id,
        kind=GENERATE_STORYBOARD_JOB_KIND,
        stale_after=timedelta(seconds=stale_after),
        error=STALE_STORYBOARD_ERROR,
    )
    for job in recovered:
        log.warning(
            "storyboard.stale_recovered",
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


# --- run (background job) --------------------------------------------------------


async def run_generate_storyboard_job(
    session: AsyncSession,
    llm: LLM,
    *,
    job_id: UUID,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> JobRecord:
    """Process one queued ``generate_storyboard`` job end to end.

    Marks it running and commits that immediately, then storyboards every
    segment that needs it (skipping ones whose existing scene set is still
    valid), persisting each segment's result the moment it finishes. The job
    ``succeeded`` if at least one segment ended up with a valid scene set (or
    a valid empty one, for a segment too short for any scene) - its
    ``result`` always records ``ready_segment_count`` / ``failed_segment_ids``
    / ``outcome`` (``"complete"`` or ``"succeeded_with_failures"``), so a
    partial run is never mistaken for a clean one; ``failed`` only if every
    segment failed. Never raises for a generation failure - only
    :class:`~edutoon.core.errors.NotFoundError` for a ``job_id`` that
    doesn't exist at all (a caller bug).
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
    # Release this row's lock now, before the (potentially long) generation
    # work below - see services.script_audio's module docstring, "Progress
    # and transactions", which this mirrors exactly.
    await session.commit()

    project_id = UUID(running.payload["project_id"])
    script_id = UUID(running.payload["script_id"])

    report = new_usage_report()
    try:
        result = await _generate_storyboard(
            session,
            llm,
            project_id=project_id,
            script_id=script_id,
            job_id=running.id,
            payload=running.payload,
            sessionmaker=sessionmaker,
            report=report,
        )
    except Exception as exc:  # noqa: BLE001 - any setup failure fails the job, never raises
        error = describe_job_failure(exc, job_id=running.id, kind=GENERATE_STORYBOARD_JOB_KIND)
        return await _mark_job_failed(
            session, job_id=running.id, error=error, result={"usage": report.as_dict()}
        )

    if result["ready_segment_count"] == 0:
        return await _mark_job_failed(
            session,
            job_id=running.id,
            error="Every segment failed to storyboard; see each segment's own error for detail.",
            result=result,
        )
    return await _mark_job_succeeded(session, job_id=running.id, result=result)


async def _generate_storyboard(
    session: AsyncSession,
    llm: LLM,
    *,
    project_id: UUID,
    script_id: UUID,
    job_id: UUID,
    payload: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    report: UsageReport,
) -> dict[str, object]:
    settings = get_settings()

    project = await projects_repo.get_by_id(session, project_id)
    if project is None:
        raise NotFoundError(f"Project {project_id} not found.")
    script = await scripts_repo.get_by_id_for_project(session, script_id, project_id)
    if script is None:
        raise NotFoundError(f"Script {script_id} not found.")
    if script.status != "approved":
        # Defence in depth: enqueue already required this.
        raise JobError(f"Script {script_id} is no longer approved; cannot storyboard it.")
    if script.measured_duration_ms is None:
        raise JobError("This script's narration is no longer fully measured.")

    segments = await scripts_repo.list_segments(session, project_id=project_id, script_id=script_id)
    if not segments:
        raise JobError("This script has no segments to storyboard.")

    audio_by_segment = await _audio_by_segment(session, project_id=project_id, script_id=script_id)
    not_ready = [
        s
        for s in segments
        if audio_by_segment.get(s.id) is None or audio_by_segment[s.id].status != "ready"
    ]
    if not_ready:
        raise JobError(f"{len(not_ready)} segment(s) do not have ready narration audio.")

    segment_starts = segment_absolute_starts(
        [audio_by_segment[segment.id].measured_duration_ms or 0 for segment in segments]
    )

    await _report_progress(
        sessionmaker,
        job_id=job_id,
        base_payload=payload,
        stage="preparing",
        segment_count=len(segments),
    )

    ready_count = 0
    failed_segment_ids: list[str] = []
    provider_calls = 0
    dropped_visuals = 0

    for index, segment in enumerate(segments):
        await _report_progress(
            sessionmaker,
            job_id=job_id,
            base_payload=payload,
            stage="processing_segment",
            segment_index=index,
            segment_count=len(segments),
            script_segment_id=str(segment.id),
        )

        audio = audio_by_segment[segment.id]
        expected_count = effective_scene_count(
            audio.measured_duration_ms or 0,
            max_scenes_per_segment=settings.MAX_SCENES_PER_SEGMENT,
            min_scene_duration_ms=settings.MIN_SCENE_DURATION_MS,
        )

        existing = await scenes_repo.list_by_segment_id(session, segment.id)
        if _is_fresh(existing, expected_count=expected_count, content_hash=audio.content_hash):
            ready_count += 1
            continue  # already valid: skip (resumability)

        try:
            if expected_count == 0:
                # The degenerate short-segment case (spec section 11): no scene can
                # meet the minimum duration, so this segment gets none - no
                # provider call, just clearing any stale leftover scene set.
                await _replace_segment_scenes(
                    sessionmaker, session, script_segment_id=segment.id, scenes=[]
                )
            else:
                draft = await record_call(
                    report,
                    "storyboard",
                    llm.compose_storyboard_scenes(
                        StoryboardSegmentBrief(
                            segment_index=segment.segment_index,
                            narration_text=segment.narration_text,
                            orientation=project.orientation,
                            lesson_title=project.title,
                        ),
                        max_scenes=expected_count,
                    ),
                )
                provider_calls += 1

                # An invalid visual is dropped (the scene stays); everything else about the
                # scene must still meet its template's contract.
                prepared: list[tuple[str, dict[str, object]]] = []
                dropped_here = 0
                for scene in draft.scenes:
                    content, dropped = without_invalid_visual(scene.template, scene.content)
                    if dropped:
                        dropped_here += 1
                        log.warning(
                            "storyboard.visual_dropped",
                            job_id=str(job_id),
                            script_segment_id=str(segment.id),
                            template=scene.template,
                            reason=visual_problem(scene.template, scene.content),
                        )
                    validate_scene_content(scene.template, content)
                    prepared.append((scene.template, content))
                # Provenance is checked on what the model actually wrote - including the text of a
                # visual just dropped - and stays a hard failure for the whole segment.
                for scene in draft.scenes:
                    require_provenance(scene.content, segment.narration_text)

                durations = allocate_scene_durations(
                    audio.measured_duration_ms or 0, len(draft.scenes)
                )
                timings = scene_timings(segment_starts[index], durations)
                new_scenes = [
                    NewScene(
                        scene_index=timing.scene_index,
                        # Advisory only (correct *now*, out of date after an earlier
                        # segment is re-narrated); reads use timeline_positions.
                        start_ms=timing.start_ms,
                        duration_ms=timing.duration_ms,
                        template=template,
                        content=content,
                        source_audio_content_hash=audio.content_hash,
                    )
                    for (template, content), timing in zip(prepared, timings, strict=True)
                ]
                await _replace_segment_scenes(
                    sessionmaker, session, script_segment_id=segment.id, scenes=new_scenes
                )
                dropped_visuals += dropped_here
            ready_count += 1
        except Exception as exc:  # noqa: BLE001 - one segment's failure must not crash the job
            error = public_job_error(exc)
            failed_segment_ids.append(str(segment.id))
            log.warning(
                "storyboard.segment_failed",
                job_id=str(job_id),
                script_segment_id=str(segment.id),
                error_type=type(exc).__name__,
                shown_to_owner=error,
            )

    await _report_progress(sessionmaker, job_id=job_id, base_payload=payload, stage="finalising")

    outcome = "complete" if ready_count == len(segments) else "succeeded_with_failures"
    return {
        "script_id": str(script_id),
        "segment_count": len(segments),
        "ready_segment_count": ready_count,
        "failed_segment_ids": failed_segment_ids,
        "outcome": outcome,
        "provider_calls": provider_calls,
        "dropped_visual_count": dropped_visuals,
        "usage": report.as_dict(),
    }


def _visual_was_dropped(scene: ScriptSegmentSceneRecord) -> bool:
    illustration = scene.content.get("illustration")
    return isinstance(illustration, dict) and illustration.get("visual_dropped") is True


def _is_fresh(
    existing: list[ScriptSegmentSceneRecord], *, expected_count: int, content_hash: str
) -> bool:
    """A segment's current scene set is still valid.

    ``expected_count`` (``domain.storyboard_timing.effective_scene_count``)
    is only ever an *upper bound* the model was told not to exceed - it may
    legitimately return fewer scenes (spec: "prefer fewer, well-chosen
    scenes ... over padding to the maximum"), so freshness never compares
    the persisted count against it. For ``expected_count == 0`` (the
    degenerate short-segment case) freshness means the set is genuinely
    empty - a stale leftover set from before a duration change still needs
    a (call-free) replacement to clear it. Otherwise freshness means the
    set is non-empty and every scene was generated from the segment's
    *current* ``script_segment_audio.content_hash``.
    """
    if expected_count == 0:
        return len(existing) == 0
    if not existing:
        return False
    return all(scene.source_audio_content_hash == content_hash for scene in existing)


async def _replace_segment_scenes(
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    session: AsyncSession,
    *,
    script_segment_id: UUID,
    scenes: list[NewScene],
) -> list[ScriptSegmentSceneRecord]:
    """Write one segment's whole scene set in its own short transaction,
    committed immediately - independent of the job's main session and of
    every other segment. Mirrors ``services.script_audio._persist_segment_result``
    exactly: this is what makes retry after a partial failure real.
    """
    if sessionmaker is None:
        record = await scenes_repo.replace_for_segment(
            session, script_segment_id=script_segment_id, scenes=scenes
        )
        await session.commit()
        return record
    async with sessionmaker() as segment_session:
        record = await scenes_repo.replace_for_segment(
            segment_session, script_segment_id=script_segment_id, scenes=scenes
        )
        await segment_session.commit()
        return record


async def _report_progress(
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    *,
    job_id: UUID,
    base_payload: dict[str, Any],
    stage: str,
    **extra: Any,
) -> None:
    """Mirrors ``services.script_audio._report_progress`` exactly: a short,
    independent session, never the main storyboard session; a no-op when
    ``sessionmaker`` is ``None``.
    """
    if sessionmaker is None:
        return
    progress = {"stage": stage, "updated_at": datetime.now(UTC).isoformat(), **extra}
    async with sessionmaker() as progress_session:
        alive = await jobs_repo.record_progress(
            progress_session, job_id=job_id, payload={**base_payload, "progress": progress}
        )
        await progress_session.commit()
    if not alive:
        raise JobAbandonedError


async def _mark_job_succeeded(
    session: AsyncSession, *, job_id: UUID, result: dict[str, object]
) -> JobRecord:
    updated = await jobs_repo.update(
        session, job_id=job_id, status="succeeded", finished_at=datetime.now(UTC), result=result
    )
    assert updated is not None
    return updated


async def _mark_job_failed(
    session: AsyncSession, *, job_id: UUID, error: str, result: dict[str, object] | None = None
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


# --- read model --------------------------------------------------------------------


async def get_storyboard_status(
    session: AsyncSession, *, project_id: UUID, owner_id: UUID, script_id: UUID
) -> StoryboardStatus:
    """A script's storyboard status: every segment paired with its expected
    scene count, its current scene set, staleness and readiness, plus the
    most recent ``generate_storyboard`` job (whatever its status) and both
    the worst-case estimate and (once a job has recorded one) the actual
    provider calls made. Ownership-scoped (rule 9). Completeness is
    computed fresh from current state, never copied from a job's historical
    result (spec: completeness is separate from job status).
    """
    project = await projects_service.get_project(
        session, project_id=project_id, owner_id=owner_id
    )

    script = await scripts_repo.get_by_id_for_project(session, script_id, project_id)
    if script is None:
        raise NotFoundError(f"Script {script_id} not found.")

    settings = get_settings()
    segments = await scripts_repo.list_segments(session, project_id=project_id, script_id=script_id)
    audio_by_segment = await _audio_by_segment(session, project_id=project_id, script_id=script_id)
    scenes_by_segment: dict[UUID, list[ScriptSegmentSceneRecord]] = {}
    for scene in await scenes_repo.list_by_script(
        session, project_id=project_id, script_id=script_id
    ):
        scenes_by_segment.setdefault(scene.script_segment_id, []).append(scene)

    # Per segment: (expected scene count, stale, ready, settled, its scenes in order).
    prepared: list[tuple[int, bool, bool, bool, list[ScriptSegmentSceneRecord]]] = []
    for segment in segments:
        audio = audio_by_segment.get(segment.id)
        scenes = sorted(scenes_by_segment.get(segment.id, []), key=lambda s: s.scene_index)
        if audio is not None and audio.status == "ready":
            expected_count = effective_scene_count(
                audio.measured_duration_ms or 0,
                max_scenes_per_segment=settings.MAX_SCENES_PER_SEGMENT,
                min_scene_duration_ms=settings.MIN_SCENE_DURATION_MS,
            )
            is_stale = any(
                scene.source_audio_content_hash != audio.content_hash for scene in scenes
            )
            is_ready = _is_fresh(
                scenes, expected_count=expected_count, content_hash=audio.content_hash
            )
        else:
            expected_count = 0
            is_stale = len(scenes) > 0
            is_ready = False
        # Settled: nothing about this segment's timing can still move - its narration is the
        # current text in the current voice, and its scenes were made from that narration.
        settled = is_ready and script_audio_service.audio_matches_current_narration(
            audio, segment, language=project.language
        )
        prepared.append((expected_count, is_stale, is_ready, settled, scenes))

    # Positions are computed here, on read, from the current narration - the same rule the
    # render plan uses. The ``start_ms`` stored on a scene row is advisory and never read: an
    # earlier segment's re-narration leaves it out of date (see ``domain.storyboard_timing``).
    timeline = timeline_positions(
        [
            SegmentTimingInput(
                audio_duration_ms=(
                    audio_by_segment[segment.id].measured_duration_ms
                    if segment.id in audio_by_segment
                    and audio_by_segment[segment.id].status == "ready"
                    else None
                ),
                scene_durations_ms=[scene.duration_ms for scene in scenes],
                settled=settled,
            )
            for segment, (_, _, _, settled, scenes) in zip(segments, prepared, strict=True)
        ],
        narration_in_flight=await script_audio_service.is_synthesis_in_flight(
            session, project_id=project_id
        ),
    )

    details: list[SegmentStoryboardDetail] = []
    for index, (segment, (expected_count, is_stale, is_ready, _, scenes)) in enumerate(
        zip(segments, prepared, strict=True)
    ):
        scene_details = tuple(
            SceneDetail(
                scene=scene,
                start_ms=timeline.scene_starts_ms[index][position],
                reviewer_flag=not has_lexical_overlap(scene.content, segment.narration_text),
            )
            for position, scene in enumerate(scenes)
        )
        details.append(
            SegmentStoryboardDetail(
                segment=segment,
                audio=audio_by_segment.get(segment.id),
                expected_scene_count=expected_count,
                scenes=scene_details,
                is_stale=is_stale,
                is_ready=is_ready,
            )
        )

    ready_segment_count = sum(1 for detail in details if detail.is_ready)
    not_ready_segment_ids = tuple(detail.segment.id for detail in details if not detail.is_ready)

    latest_job = await jobs_repo.get_latest_by_project_kind(
        session, project_id=project_id, kind=GENERATE_STORYBOARD_JOB_KIND
    )
    if latest_job is not None:
        estimated_provider_calls = _int_or_zero(latest_job.payload.get("estimated_provider_calls"))
    else:
        expected_counts = [detail.expected_scene_count for detail in details]
        estimated_provider_calls = sum(1 for count in expected_counts if count > 0)

    actual_provider_calls = (
        _int_or_zero(latest_job.result.get("provider_calls"))
        if latest_job is not None and latest_job.result is not None
        else None
    )

    return StoryboardStatus(
        script=script,
        job=latest_job,
        segments=tuple(details),
        ready_segment_count=ready_segment_count,
        not_ready_segment_ids=not_ready_segment_ids,
        is_complete=len(not_ready_segment_ids) == 0,
        timings_authoritative=timeline.authoritative,
        dropped_visual_count=sum(
            1 for detail in details for scene in detail.scenes if _visual_was_dropped(scene.scene)
        ),
        estimated_provider_calls=estimated_provider_calls,
        actual_provider_calls=actual_provider_calls,
    )
