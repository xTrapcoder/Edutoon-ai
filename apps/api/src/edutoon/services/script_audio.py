"""Local narration synthesis - service layer (Phase 8).

- ``enqueue_synthesis`` - the caller-facing entry point. Verifies project
  ownership, that the script belongs to the project and is ``approved``
  (only an approved script may be synthesised - a draft is refused),
  recovers any stale ``synthesise_voice`` job, refuses a second concurrent
  one for the project, checks the script's total and per-segment character
  caps, then creates the job. Idempotent on the caller's ``Idempotency-Key``.
- ``run_synthesise_voice_job`` - what the ``worker`` process calls after
  claiming the job. Selects the project's voice once (Piper: local, free, no
  company API key - see apps/api/README.md, "Text-to-speech", for the
  candidates investigated and the en-IN substitution this names explicitly),
  then processes every segment in narration order: existing audio whose
  content hash still matches this segment's current text/voice/settings is
  skipped; everything else is (re)synthesised, measured from its own written
  WAV file, uploaded to ``BUCKET_SEGMENTS`` at a deterministic key, and its
  one current ``script_segment_audio`` row is written - each in its own
  short, independent transaction the moment it finishes. That is what makes
  retry after a partial failure real: an already-valid segment is never
  touched again, and one segment's failure never stops the rest. The
  script's own ``measured_duration_ms`` is set only once every one of its
  segments has valid audio - never on a partial run, so an incomplete
  synthesis can never be read back as complete.
- ``get_synthesis_status`` / ``get_segment_audio_for_playback`` - the read
  models the API's status and audio-streaming routes use.

**Progress and transactions.** Mirrors ``services.scripts`` (Phase 7)
exactly: the job's "running" transition is committed immediately, releasing
its row's claim lock before synthesis begins, and every subsequent write -
progress, and each segment's own audio row - goes through its own short
session opened from ``sessionmaker``, never the long-lived session the job
started with. Piper's synthesis itself never runs inside an open database
transaction.

Architecture (CLAUDE.md rules 1, 2, 3, 9): this module is the only layer
allowed to touch ``providers.tts``; it never issues SQL itself, going
through ``repositories`` for every read and write; every path is project +
owner scoped and a resource the caller doesn't own is a 404.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from edutoon.core.config import Settings, get_settings
from edutoon.core.errors import (
    ConflictError,
    JobAbandonedError,
    JobError,
    NotFoundError,
    ValidationError,
)
from edutoon.domain.script_audio import (
    chunk_narration_for_tts,
    compute_content_hash,
    segment_audio_storage_key,
)
from edutoon.domain.tts_voice import VoiceSelection, select_voice
from edutoon.providers import tts as tts_provider
from edutoon.providers.storage import ObjectData, ObjectNotFoundError, Storage
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import script_segment_audio as script_segment_audio_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories.jobs import JobRecord
from edutoon.repositories.script_segment_audio import ScriptSegmentAudioRecord
from edutoon.repositories.scripts import ScriptRecord, ScriptSegmentRecord
from edutoon.services import audit_logs as audit_logs_service
from edutoon.services import projects as projects_service
from edutoon.services.job_errors import describe_job_failure, public_job_error

log = structlog.get_logger()

SYNTHESISE_VOICE_JOB_KIND = "synthesise_voice"

AUDIT_ENTITY_TYPE = "script_audio"
AUDIT_ACTION_ENQUEUED = "synthesis.enqueued"
AUDIT_ACTION_RECOVERED = "synthesis.recovered"

STALE_SYNTHESIS_ERROR = (
    "The worker stopped before finishing this synthesis, so it was marked as failed. "
    "Any segment already synthesised keeps its audio - run synthesis again to cover "
    "the rest."
)

AUDIO_FORMAT = "wav"
AUDIO_CONTENT_TYPE = "audio/wav"


@dataclass(frozen=True, slots=True)
class SynthesisEnqueued:
    job: JobRecord
    replayed: bool


@dataclass(frozen=True, slots=True)
class SegmentAudioDetail:
    segment: ScriptSegmentRecord
    audio: ScriptSegmentAudioRecord | None


@dataclass(frozen=True, slots=True)
class SynthesisStatus:
    script: ScriptRecord
    job: JobRecord | None
    segments: tuple[SegmentAudioDetail, ...]


def voice_selection_from_settings(settings: Settings, language: str) -> VoiceSelection:
    return select_voice(
        language,  # type: ignore[arg-type]  # validated to Language by ck_projects_language
        en_gb_voice_model=settings.PIPER_VOICE_EN_GB,
        en_in_voice_model=settings.PIPER_VOICE_EN_IN,
        en_in_is_temporary_substitute=settings.PIPER_EN_IN_IS_TEMPORARY_SUBSTITUTE,
    )


def audio_matches_current_narration(
    audio: ScriptSegmentAudioRecord | None, segment: ScriptSegmentRecord, *, language: str
) -> bool:
    """Whether ``audio`` is the ready narration of ``segment``'s text in the voice and engine
    that would be used *today*. ``False`` for no audio, audio that is not ``ready``, or audio a
    changed voice, engine or sample rate has made out of date - such a segment will be
    re-synthesised, so its measured duration is not final. Same fingerprint the synthesis job
    and the render gate use (:func:`~edutoon.domain.script_audio.compute_content_hash`)."""
    if audio is None or audio.status != "ready":
        return False
    voice = voice_selection_from_settings(get_settings(), language)
    expected = compute_content_hash(
        narration_text=segment.narration_text,
        voice_model=voice.voice_model,
        engine_version=tts_provider.engine_version(),
        sample_rate=audio.sample_rate or 0,
    )
    return audio.content_hash == expected


async def is_synthesis_in_flight(session: AsyncSession, *, project_id: UUID) -> bool:
    """Whether a ``synthesise_voice`` job is queued or running for the project - narration is
    being (re)made right now, so measured durations are about to change."""
    active = await jobs_repo.get_active_by_project_kind(
        session, project_id=project_id, kind=SYNTHESISE_VOICE_JOB_KIND
    )
    return active is not None


# --- enqueue -----------------------------------------------------------------------


async def enqueue_synthesis(
    session: AsyncSession,
    *,
    project_id: UUID,
    owner_id: UUID,
    script_id: UUID,
    idempotency_key: str,
) -> SynthesisEnqueued:
    """Queue narration synthesis for ``script_id``.

    Raises :class:`~edutoon.core.errors.NotFoundError` if the project isn't
    owned by ``owner_id``, or the script doesn't belong to it (rule 9).
    Raises :class:`~edutoon.core.errors.ConflictError` if the script is not
    ``approved``, or another synthesis is already queued or running for the
    project. Raises :class:`~edutoon.core.errors.ValidationError` if the
    script's narration exceeds the configured character caps. A repeat call
    with the same ``idempotency_key`` returns the original job with
    ``replayed=True``.
    """
    await projects_service.get_project(session, project_id=project_id, owner_id=owner_id)

    existing_job = await jobs_repo.get_by_idempotency_key(
        session,
        project_id=project_id,
        kind=SYNTHESISE_VOICE_JOB_KIND,
        idempotency_key=idempotency_key,
    )
    if existing_job is not None:
        return SynthesisEnqueued(job=existing_job, replayed=True)

    await jobs_repo.lock_project_kind(
        session, project_id=project_id, kind=SYNTHESISE_VOICE_JOB_KIND
    )
    await _recover_stale_synthesis(session, project_id=project_id)
    active = await jobs_repo.get_active_by_project_kind(
        session, project_id=project_id, kind=SYNTHESISE_VOICE_JOB_KIND
    )
    if active is not None:
        raise ConflictError(
            "Synthesis is already queued or running for this project; wait for it to finish."
        )

    script = await scripts_repo.get_by_id_for_project(session, script_id, project_id)
    if script is None:
        raise NotFoundError(f"Script {script_id} not found.")
    if script.status != "approved":
        raise ConflictError(
            f"Only an approved script can be synthesised; this one is '{script.status}'."
        )

    settings = get_settings()
    segments = await scripts_repo.list_segments(session, project_id=project_id, script_id=script_id)
    if not segments:
        raise ValidationError("This script has no segments to synthesise.")

    total_chars = sum(len(segment.narration_text) for segment in segments)
    if total_chars > settings.MAX_TTS_CHARS_PER_SCRIPT:
        raise ValidationError(
            f"This script has {total_chars} characters of narration; synthesis allows at "
            f"most {settings.MAX_TTS_CHARS_PER_SCRIPT}."
        )
    oversized = [
        segment
        for segment in segments
        if len(segment.narration_text) > settings.MAX_TTS_CHARS_PER_SEGMENT
    ]
    if oversized:
        raise ValidationError(
            f"{len(oversized)} segment(s) exceed the {settings.MAX_TTS_CHARS_PER_SEGMENT}-"
            "character per-segment synthesis limit."
        )

    job = await jobs_repo.create(
        session,
        project_id=project_id,
        kind=SYNTHESISE_VOICE_JOB_KIND,
        payload={"script_id": str(script_id), "project_id": str(project_id)},
        idempotency_key=idempotency_key,
    )
    await audit_logs_service.record_audit_log(
        session,
        action=AUDIT_ACTION_ENQUEUED,
        entity_type=AUDIT_ENTITY_TYPE,
        entity_id=script_id,
        project_id=project_id,
        actor_id=owner_id,
        context={"job_id": str(job.id), "segment_count": len(segments)},
    )
    return SynthesisEnqueued(job=job, replayed=False)


async def _recover_stale_synthesis(session: AsyncSession, *, project_id: UUID) -> None:
    """Fail any ``synthesise_voice`` job a crashed worker left ``running``.
    Mirrors ``topic_extraction``/``claim_extraction``/``scripts`` stale
    recovery exactly - this handler also releases its job row's claim lock
    immediately (see the module docstring, "Progress and transactions").
    Unlike ``generate_script``, synthesis never changes the script's own
    status, so there is nothing else here to repair: already-``ready``
    segment audio is untouched, and a fresh synthesis call simply resumes.
    """
    stale_after = get_settings().SYNTHESIS_STALE_AFTER_SECONDS
    recovered = await jobs_repo.fail_stale_running(
        session,
        project_id=project_id,
        kind=SYNTHESISE_VOICE_JOB_KIND,
        stale_after=timedelta(seconds=stale_after),
        error=STALE_SYNTHESIS_ERROR,
    )
    for job in recovered:
        log.warning(
            "synthesis.stale_recovered",
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


async def run_synthesise_voice_job(
    session: AsyncSession,
    storage: Storage,
    *,
    job_id: UUID,
    voices_dir: Path,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> JobRecord:
    """Process one queued ``synthesise_voice`` job end to end.

    Marks it running and commits that immediately, then synthesises every
    segment that needs it (skipping ones whose existing audio is still
    valid), persisting each result the moment it finishes. The job
    ``succeeded`` if at least one segment ended up with valid audio - its
    ``result`` always lists exactly how many, and which failed, so a partial
    run is never mistaken for a clean one; ``failed`` only if every segment
    failed. Never raises for a synthesis failure - only
    :class:`~edutoon.core.errors.NotFoundError` for a ``job_id`` that doesn't
    exist at all (a caller bug).
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
    # Release this row's lock now, before the synthesis work below - see the
    # module docstring, "Progress and transactions".
    await session.commit()

    project_id = UUID(running.payload["project_id"])
    script_id = UUID(running.payload["script_id"])

    try:
        result = await _synthesise_script(
            session,
            storage,
            project_id=project_id,
            script_id=script_id,
            job_id=running.id,
            payload=running.payload,
            voices_dir=voices_dir,
            sessionmaker=sessionmaker,
        )
    except Exception as exc:  # noqa: BLE001 - any setup failure fails the job, never raises
        error = describe_job_failure(exc, job_id=running.id, kind=SYNTHESISE_VOICE_JOB_KIND)
        return await _mark_job_failed(session, job_id=running.id, error=error)

    if result["ready_segment_count"] == 0:
        return await _mark_job_failed(
            session,
            job_id=running.id,
            error="Every segment failed to synthesise; see each segment's own error for detail.",
        )
    return await _mark_job_succeeded(session, job_id=running.id, result=result)


async def _synthesise_script(
    session: AsyncSession,
    storage: Storage,
    *,
    project_id: UUID,
    script_id: UUID,
    job_id: UUID,
    payload: dict[str, Any],
    voices_dir: Path,
    sessionmaker: async_sessionmaker[AsyncSession] | None,
) -> dict[str, object]:
    settings = get_settings()

    project = await projects_repo.get_by_id(session, project_id)
    if project is None:
        raise NotFoundError(f"Project {project_id} not found.")
    script = await scripts_repo.get_by_id_for_project(session, script_id, project_id)
    if script is None:
        raise NotFoundError(f"Script {script_id} not found.")
    if script.status != "approved":
        # Defence in depth: enqueue already required this, but the script could in
        # principle have moved on by the time the worker gets to it.
        raise JobError(f"Script {script_id} is no longer approved; cannot synthesise it.")

    segments = await scripts_repo.list_segments(session, project_id=project_id, script_id=script_id)
    if not segments:
        raise JobError("This script has no segments to synthesise.")

    voice_selection = voice_selection_from_settings(settings, project.language)

    await _report_progress(
        sessionmaker,
        job_id=job_id,
        base_payload=payload,
        stage="preparing",
        segment_count=len(segments),
    )

    # Loaded once and reused for every segment (providers.tts also caches by
    # voice_model, but loading here surfaces a missing-voice-file failure
    # before any segment work starts, and gives us the voice's sample rate).
    voice = tts_provider.load_voice(voices_dir, voice_selection.voice_model)
    sample_rate = int(voice.config.sample_rate)
    engine_version = tts_provider.engine_version()

    ready_count = 0
    failed_segment_ids: list[str] = []

    for index, segment in enumerate(segments):
        await _report_progress(
            sessionmaker,
            job_id=job_id,
            base_payload=payload,
            stage="synthesising_segment",
            segment_index=index,
            segment_count=len(segments),
            script_segment_id=str(segment.id),
        )

        content_hash = compute_content_hash(
            narration_text=segment.narration_text,
            voice_model=voice_selection.voice_model,
            engine_version=engine_version,
            sample_rate=sample_rate,
        )

        existing = await script_segment_audio_repo.get_by_segment_id(session, segment.id)
        if (
            existing is not None
            and existing.status == "ready"
            and existing.content_hash == content_hash
        ):
            ready_count += 1
            continue  # already valid: skip (resumability)

        try:
            await _synthesise_one_segment(
                storage,
                voices_dir=voices_dir,
                voice_selection=voice_selection,
                project_id=project_id,
                script_id=script_id,
                segment=segment,
                content_hash=content_hash,
                bucket=settings.BUCKET_SEGMENTS,
                timeout_seconds=settings.TTS_SEGMENT_TIMEOUT_SECONDS,
                max_chars_per_call=settings.MAX_TTS_CHARS_PER_CALL,
                sessionmaker=sessionmaker,
                session=session,
            )
            ready_count += 1
        except Exception as exc:  # noqa: BLE001 - one segment's failure must not crash the job
            error = public_job_error(exc)
            await _persist_segment_result(
                sessionmaker,
                session,
                script_segment_id=segment.id,
                status="failed",
                voice_model=voice_selection.voice_model,
                is_temporary_voice_substitute=voice_selection.is_temporary_substitute,
                content_hash=content_hash,
                error=error,
            )
            failed_segment_ids.append(str(segment.id))
            log.warning(
                "synthesis.segment_failed",
                job_id=str(job_id),
                script_segment_id=str(segment.id),
                error_type=type(exc).__name__,
                shown_to_owner=error,
            )

    await _report_progress(sessionmaker, job_id=job_id, base_payload=payload, stage="finalising")

    measured_total_ms: int | None = None
    if ready_count == len(segments):
        all_audio = await script_segment_audio_repo.list_by_script(
            session, project_id=project_id, script_id=script_id
        )
        measured_total_ms = sum(
            audio.measured_duration_ms or 0 for audio in all_audio if audio.status == "ready"
        )
        await scripts_repo.set_measured_duration(
            session,
            project_id=project_id,
            script_id=script_id,
            measured_duration_ms=measured_total_ms,
        )

    return {
        "script_id": str(script_id),
        "segment_count": len(segments),
        "ready_segment_count": ready_count,
        "failed_segment_ids": failed_segment_ids,
        "measured_duration_ms": measured_total_ms,
        "voice_model": voice_selection.voice_model,
        "is_temporary_voice_substitute": voice_selection.is_temporary_substitute,
    }


async def _synthesise_one_segment(
    storage: Storage,
    *,
    voices_dir: Path,
    voice_selection: VoiceSelection,
    project_id: UUID,
    script_id: UUID,
    segment: ScriptSegmentRecord,
    content_hash: str,
    bucket: str,
    timeout_seconds: int,
    max_chars_per_call: int,
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    session: AsyncSession,
) -> ScriptSegmentAudioRecord:
    chunks = chunk_narration_for_tts(segment.narration_text, max_chars_per_call=max_chars_per_call)
    if not chunks:
        raise JobError(f"Segment {segment.id} has no narration text to synthesise.")

    with tempfile.TemporaryDirectory(prefix="edutoon-tts-") as tmp_dir:
        wav_path = Path(tmp_dir) / f"{segment.id}.wav"
        # asyncio.wait_for cancels the *awaiting* coroutine on a timeout; the
        # underlying synthesis thread (asyncio.to_thread has no way to force-stop
        # a running thread) keeps running to completion in the background, but
        # its result is simply never used - the segment is already reported failed.
        result = await asyncio.wait_for(
            tts_provider.synthesize_to_wav_file(
                voices_dir=voices_dir,
                voice_model=voice_selection.voice_model,
                chunks=chunks,
                wav_path=wav_path,
            ),
            timeout=timeout_seconds,
        )

    key = segment_audio_storage_key(
        project_id=project_id, script_id=script_id, segment_id=segment.id
    )
    await storage.put_object(
        bucket=bucket, key=key, body=result.audio_bytes, content_type=AUDIO_CONTENT_TYPE
    )

    return await _persist_segment_result(
        sessionmaker,
        session,
        script_segment_id=segment.id,
        status="ready",
        storage_bucket=bucket,
        storage_key=key,
        format=AUDIO_FORMAT,
        sample_rate=result.sample_rate,
        channels=result.channels,
        measured_duration_ms=result.duration_ms,
        voice_model=voice_selection.voice_model,
        is_temporary_voice_substitute=voice_selection.is_temporary_substitute,
        content_hash=content_hash,
        error=None,
    )


async def _persist_segment_result(
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    session: AsyncSession,
    **fields: Any,
) -> ScriptSegmentAudioRecord:
    """Write one segment's final audio result (``ready`` or ``failed``) in its
    own short transaction, committed immediately - independent of the job's
    main session and of every other segment. This is what makes retry after
    a partial failure real: an already-written result is durable the instant
    it happens, never held open behind the rest of the job. Falls back to the
    given ``session`` (still committed) when no ``sessionmaker`` is available
    (a direct unit test with only a bare session).
    """
    if sessionmaker is None:
        record = await script_segment_audio_repo.upsert(session, **fields)
        await session.commit()
        return record
    async with sessionmaker() as segment_session:
        record = await script_segment_audio_repo.upsert(segment_session, **fields)
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
    """Report interim progress through a short, independent session - never
    the main synthesis session. Mirrors
    ``services.scripts._report_progress`` exactly. A no-op when
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


async def _mark_job_failed(session: AsyncSession, *, job_id: UUID, error: str) -> JobRecord:
    updated = await jobs_repo.update(
        session, job_id=job_id, status="failed", finished_at=datetime.now(UTC), error=error
    )
    assert updated is not None
    return updated


# --- read model --------------------------------------------------------------------


async def get_synthesis_status(
    session: AsyncSession, *, project_id: UUID, owner_id: UUID, script_id: UUID
) -> SynthesisStatus:
    """A script's synthesis status: every segment paired with its current
    audio (if any), and the most recent ``synthesise_voice`` job for the
    project, whatever its status. Ownership-scoped (rule 9).
    """
    await projects_service.get_project(session, project_id=project_id, owner_id=owner_id)

    script = await scripts_repo.get_by_id_for_project(session, script_id, project_id)
    if script is None:
        raise NotFoundError(f"Script {script_id} not found.")

    segments = await scripts_repo.list_segments(session, project_id=project_id, script_id=script_id)
    audio_by_segment = {
        audio.script_segment_id: audio
        for audio in await script_segment_audio_repo.list_by_script(
            session, project_id=project_id, script_id=script_id
        )
    }
    details = tuple(
        SegmentAudioDetail(segment=segment, audio=audio_by_segment.get(segment.id))
        for segment in segments
    )

    latest_job = await jobs_repo.get_latest_by_project_kind(
        session, project_id=project_id, kind=SYNTHESISE_VOICE_JOB_KIND
    )
    return SynthesisStatus(script=script, job=latest_job, segments=details)


async def get_segment_audio_for_playback(
    session: AsyncSession,
    storage: Storage,
    *,
    project_id: UUID,
    owner_id: UUID,
    script_id: UUID,
    segment_id: UUID,
    range_header: str | None,
) -> tuple[ObjectData, ScriptSegmentAudioRecord]:
    """The bytes (or, with ``range_header``, one byte range) of one segment's
    current audio, for the streaming playback route.

    Raises :class:`~edutoon.core.errors.NotFoundError` if the project isn't
    owned by ``owner_id``, the script doesn't belong to it, the segment
    doesn't belong to the script, or its audio isn't ``ready`` (rule 9: any
    of these looks identical to the resource not existing).
    """
    await projects_service.get_project(session, project_id=project_id, owner_id=owner_id)

    script = await scripts_repo.get_by_id_for_project(session, script_id, project_id)
    if script is None:
        raise NotFoundError(f"Script {script_id} not found.")

    audio = await script_segment_audio_repo.get_by_segment_id_for_script(
        session, project_id=project_id, script_id=script_id, script_segment_id=segment_id
    )
    if (
        audio is None
        or audio.status != "ready"
        or audio.storage_bucket is None
        or audio.storage_key is None
    ):
        raise NotFoundError(f"Audio for segment {segment_id} not found.")

    try:
        data = await storage.get_object(
            bucket=audio.storage_bucket, key=audio.storage_key, range_header=range_header
        )
    except ObjectNotFoundError as exc:
        raise NotFoundError(f"Audio for segment {segment_id} not found.") from exc
    return data, audio
