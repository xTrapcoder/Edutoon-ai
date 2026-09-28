"""``services.script_audio`` - narration synthesis (Phase 8).

Unlike the LLM/embeddings-backed services, this file runs **real** local
Piper synthesis throughout - it is never mocked for the core pipeline (per
apps/api/README.md, "Testing"). Only individual, narrowly-scoped failure
injections (a forced timeout, a missing voice file) stand in for a specific
failure mode; everything else is the genuine engine producing genuine audio,
uploaded to and read back from the real local MinIO these tests share with
``test_project_deletion_storage.py``.
"""

from __future__ import annotations

import wave
from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from edutoon.core.config import get_settings
from edutoon.core.errors import ConflictError, NotFoundError, ValidationError
from edutoon.providers.storage import Storage, get_storage_client
from edutoon.providers.tts import synthesize_to_wav_file as real_synthesize_to_wav_file
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import script_segment_audio as audio_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.scripts import NewScriptSegment
from edutoon.services import script_audio as service

MODEL = "claude-sonnet-5"
VOICES_DIR = Path(__file__).resolve().parents[1] / "var" / "piper-voices"
VOICE_ID = "en_GB-cori-medium"

pytestmark = pytest.mark.skipif(
    not (VOICES_DIR / f"{VOICE_ID}.onnx").exists(),
    reason=f"Piper voice {VOICE_ID} not downloaded - run `make voices` first",
)


@pytest.fixture
def storage() -> Storage:
    settings = get_settings()
    return get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )


@pytest.fixture
async def cleanup(storage) -> AsyncIterator[list[tuple[str, str]]]:
    created: list[tuple[str, str]] = []
    yield created
    for bucket, key in created:
        await storage.delete_object(bucket=bucket, key=key)


BUCKET = get_settings().BUCKET_SEGMENTS


async def _project(db_session, *, language: str = "en-GB", target_duration_ms: int = 60_000):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    return await projects_repo.create(
        db_session,
        owner_id=owner.id,
        title="Photosynthesis",
        source_type="topic",
        language=language,
        target_duration_ms=target_duration_ms,
    )


async def _approved_script(db_session, project, texts: list[str]):
    script = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)
    segments = await scripts_repo.add_segments(
        db_session,
        script_id=script.id,
        segments=[
            NewScriptSegment(segment_index=i, narration_text=text, estimated_duration_ms=1000)
            for i, text in enumerate(texts)
        ],
    )
    approved = await scripts_repo.set_status(
        db_session, project_id=project.id, script_id=script.id, status="approved"
    )
    assert approved is not None
    return approved, segments


# --- approved-script-only enforcement -------------------------------------------------------


async def test_only_an_approved_script_may_be_synthesised(db_session):
    project = await _project(db_session)
    script, _ = await _approved_script(db_session, project, ["Hello world."])
    await scripts_repo.set_status(
        db_session, project_id=project.id, script_id=script.id, status="draft"
    )

    with pytest.raises(ConflictError, match="approved"):
        await service.enqueue_synthesis(
            db_session,
            project_id=project.id,
            owner_id=project.owner_id,
            script_id=script.id,
            idempotency_key="k",
        )


@pytest.mark.parametrize("status", ["pending", "draft", "failed", "superseded"])
async def test_every_non_approved_status_is_rejected(db_session, status):
    project = await _project(db_session)
    script, _ = await _approved_script(db_session, project, ["Hello world."])
    await scripts_repo.set_status(
        db_session, project_id=project.id, script_id=script.id, status=status
    )

    with pytest.raises(ConflictError):
        await service.enqueue_synthesis(
            db_session,
            project_id=project.id,
            owner_id=project.owner_id,
            script_id=script.id,
            idempotency_key="k",
        )


async def test_a_missing_script_is_a_404(db_session):
    project = await _project(db_session)
    with pytest.raises(NotFoundError):
        await service.enqueue_synthesis(
            db_session,
            project_id=project.id,
            owner_id=project.owner_id,
            script_id=uuid4(),
            idempotency_key="k",
        )


async def test_someone_elses_project_is_a_404_not_a_403(db_session):
    project = await _project(db_session)
    script, _ = await _approved_script(db_session, project, ["Hello world."])
    intruder = await users_repo.create(db_session, email=f"{uuid4()}@example.com")

    with pytest.raises(NotFoundError):
        await service.enqueue_synthesis(
            db_session,
            project_id=project.id,
            owner_id=intruder.id,
            script_id=script.id,
            idempotency_key="k",
        )


# --- ownership on the read/streaming paths --------------------------------------------------


async def test_synthesis_status_is_ownership_scoped(db_session):
    project = await _project(db_session)
    script, _ = await _approved_script(db_session, project, ["Hello world."])
    intruder = await users_repo.create(db_session, email=f"{uuid4()}@example.com")

    with pytest.raises(NotFoundError):
        await service.get_synthesis_status(
            db_session, project_id=project.id, owner_id=intruder.id, script_id=script.id
        )


async def test_playback_is_ownership_scoped(db_session, storage):
    project = await _project(db_session)
    script, [segment] = await _approved_script(db_session, project, ["Hello world."])
    intruder = await users_repo.create(db_session, email=f"{uuid4()}@example.com")

    with pytest.raises(NotFoundError):
        await service.get_segment_audio_for_playback(
            db_session,
            storage,
            project_id=project.id,
            owner_id=intruder.id,
            script_id=script.id,
            segment_id=segment.id,
            range_header=None,
        )


async def test_playback_of_audio_that_was_never_synthesised_is_a_404(db_session, storage):
    project = await _project(db_session)
    script, [segment] = await _approved_script(db_session, project, ["Hello world."])

    with pytest.raises(NotFoundError):
        await service.get_segment_audio_for_playback(
            db_session,
            storage,
            project_id=project.id,
            owner_id=project.owner_id,
            script_id=script.id,
            segment_id=segment.id,
            range_header=None,
        )


# --- idempotency and concurrency guard -------------------------------------------------------


async def test_a_repeat_idempotency_key_replays_the_original_job(db_session):
    project = await _project(db_session)
    script, _ = await _approved_script(db_session, project, ["Hello world."])

    first = await service.enqueue_synthesis(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="same-key",
    )
    second = await service.enqueue_synthesis(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="same-key",
    )

    assert first.replayed is False
    assert second.replayed is True
    assert second.job.id == first.job.id


async def test_a_second_concurrent_synthesis_for_the_project_is_refused(db_session):
    project = await _project(db_session)
    script, _ = await _approved_script(db_session, project, ["Hello world."])

    await service.enqueue_synthesis(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="first",
    )

    with pytest.raises(ConflictError, match="already queued or running"):
        await service.enqueue_synthesis(
            db_session,
            project_id=project.id,
            owner_id=project.owner_id,
            script_id=script.id,
            idempotency_key="second",
        )


# --- character caps ---------------------------------------------------------------------------


async def test_a_script_with_no_segments_is_refused(db_session):
    project = await _project(db_session)
    script = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)
    await scripts_repo.set_status(
        db_session, project_id=project.id, script_id=script.id, status="approved"
    )

    with pytest.raises(ValidationError, match="no segments"):
        await service.enqueue_synthesis(
            db_session,
            project_id=project.id,
            owner_id=project.owner_id,
            script_id=script.id,
            idempotency_key="k",
        )


async def test_the_per_script_character_cap_is_enforced(db_session, monkeypatch):
    settings = get_settings().model_copy(update={"MAX_TTS_CHARS_PER_SCRIPT": 10})
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    project = await _project(db_session)
    script, _ = await _approved_script(
        db_session, project, ["This is far more than ten characters."]
    )

    with pytest.raises(ValidationError, match="characters of narration"):
        await service.enqueue_synthesis(
            db_session,
            project_id=project.id,
            owner_id=project.owner_id,
            script_id=script.id,
            idempotency_key="k",
        )


async def test_the_per_segment_character_cap_is_enforced(db_session, monkeypatch):
    settings = get_settings().model_copy(update={"MAX_TTS_CHARS_PER_SEGMENT": 10})
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    project = await _project(db_session)
    script, _ = await _approved_script(
        db_session, project, ["This is far more than ten characters."]
    )

    with pytest.raises(ValidationError, match="per-segment synthesis limit"):
        await service.enqueue_synthesis(
            db_session,
            project_id=project.id,
            owner_id=project.owner_id,
            script_id=script.id,
            idempotency_key="k",
        )


# --- real synthesis: the core pipeline (never mocked) ---------------------------------------


async def _enqueue_and_run(db_session, storage, project, script, *, sessionmaker=None, key="k"):
    enqueued = await service.enqueue_synthesis(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key=key,
    )
    await db_session.commit()
    job = await service.run_synthesise_voice_job(
        db_session,
        storage,
        job_id=enqueued.job.id,
        voices_dir=VOICES_DIR,
        sessionmaker=sessionmaker,
    )
    return job


async def _wav_duration_ms(data: bytes) -> int:
    with wave.open(BytesIO(data), "rb") as wav_file:
        return round(wav_file.getnframes() / wav_file.getframerate() * 1000)


async def test_a_segment_is_really_synthesised_and_uploaded(db_session, storage, cleanup):
    project = await _project(db_session)
    script, [segment] = await _approved_script(db_session, project, ["Hello world."])

    job = await _enqueue_and_run(db_session, storage, project, script)

    assert job.status == "succeeded"
    audio = await audio_repo.get_by_segment_id(db_session, segment.id)
    assert audio is not None and audio.status == "ready"
    cleanup.append((audio.storage_bucket, audio.storage_key))
    assert await storage.object_exists(bucket=audio.storage_bucket, key=audio.storage_key) is True
    assert audio.measured_duration_ms is not None and audio.measured_duration_ms > 0
    assert audio.format == "wav"
    assert audio.sample_rate == 22050
    assert audio.channels == 1


async def test_en_gb_voice_is_never_flagged_a_substitute(db_session, storage, cleanup):
    project = await _project(db_session, language="en-GB")
    script, [segment] = await _approved_script(db_session, project, ["Good morning."])

    await _enqueue_and_run(db_session, storage, project, script)

    audio = await audio_repo.get_by_segment_id(db_session, segment.id)
    assert audio is not None
    cleanup.append((audio.storage_bucket, audio.storage_key))
    assert audio.voice_model == get_settings().PIPER_VOICE_EN_GB
    assert audio.is_temporary_voice_substitute is False


async def test_en_in_voice_is_flagged_as_a_temporary_substitute(db_session, storage, cleanup):
    project = await _project(db_session, language="en-IN")
    script, [segment] = await _approved_script(db_session, project, ["Good morning."])

    await _enqueue_and_run(db_session, storage, project, script)

    audio = await audio_repo.get_by_segment_id(db_session, segment.id)
    assert audio is not None
    cleanup.append((audio.storage_bucket, audio.storage_key))
    assert audio.voice_model == get_settings().PIPER_VOICE_EN_IN
    assert audio.is_temporary_voice_substitute == get_settings().PIPER_EN_IN_IS_TEMPORARY_SUBSTITUTE


async def test_the_storage_key_is_deterministic(db_session, storage, cleanup):
    from edutoon.domain.script_audio import segment_audio_storage_key

    project = await _project(db_session)
    script, [segment] = await _approved_script(db_session, project, ["Testing storage keys."])

    await _enqueue_and_run(db_session, storage, project, script)

    audio = await audio_repo.get_by_segment_id(db_session, segment.id)
    assert audio is not None
    cleanup.append((audio.storage_bucket, audio.storage_key))
    expected = segment_audio_storage_key(
        project_id=project.id, script_id=script.id, segment_id=segment.id
    )
    assert audio.storage_key == expected
    assert audio.storage_bucket == BUCKET


async def test_measured_duration_matches_the_real_stored_wav_file(db_session, storage, cleanup):
    project = await _project(db_session)
    script, [segment] = await _approved_script(
        db_session, project, ["This is a somewhat longer sentence to narrate for timing."]
    )

    await _enqueue_and_run(db_session, storage, project, script)

    audio = await audio_repo.get_by_segment_id(db_session, segment.id)
    assert audio is not None
    cleanup.append((audio.storage_bucket, audio.storage_key))
    downloaded = await storage.get_object(bucket=audio.storage_bucket, key=audio.storage_key)
    real_duration_ms = await _wav_duration_ms(downloaded.body)
    assert audio.measured_duration_ms == real_duration_ms


async def test_estimated_duration_is_never_touched_by_synthesis(db_session, storage, cleanup):
    project = await _project(db_session)
    script, [segment] = await _approved_script(db_session, project, ["Hello world."])
    before = await scripts_repo.get_by_id_for_project(db_session, script.id, project.id)
    assert before is not None

    await _enqueue_and_run(db_session, storage, project, script)

    audio = await audio_repo.get_by_segment_id(db_session, segment.id)
    assert audio is not None
    cleanup.append((audio.storage_bucket, audio.storage_key))
    after = await scripts_repo.get_by_id_for_project(db_session, script.id, project.id)
    assert after is not None
    assert after.estimated_duration_ms == before.estimated_duration_ms


async def test_script_measured_duration_is_the_sum_once_every_segment_is_ready(
    db_session, storage, cleanup
):
    project = await _project(db_session)
    script, segments = await _approved_script(
        db_session, project, ["First segment.", "Second segment here."]
    )

    await _enqueue_and_run(db_session, storage, project, script)

    audios = []
    for segment in segments:
        audio = await audio_repo.get_by_segment_id(db_session, segment.id)
        assert audio is not None and audio.status == "ready"
        cleanup.append((audio.storage_bucket, audio.storage_key))
        audios.append(audio)

    finished = await scripts_repo.get_by_id_for_project(db_session, script.id, project.id)
    assert finished is not None
    assert finished.measured_duration_ms == sum(a.measured_duration_ms for a in audios)


async def test_the_job_result_reports_progress_honestly(db_session, storage, cleanup):
    project = await _project(db_session)
    script, [segment] = await _approved_script(db_session, project, ["Hello world."])

    job = await _enqueue_and_run(db_session, storage, project, script)

    audio = await audio_repo.get_by_segment_id(db_session, segment.id)
    assert audio is not None
    cleanup.append((audio.storage_bucket, audio.storage_key))
    assert job.result is not None
    assert job.result["ready_segment_count"] == 1
    assert job.result["segment_count"] == 1
    assert job.result["failed_segment_ids"] == []
    assert job.result["measured_duration_ms"] == audio.measured_duration_ms


# --- resumability, stale-audio replacement, retry --------------------------------------------


async def test_resumability_skips_already_valid_audio(db_session, storage, cleanup, monkeypatch):
    project = await _project(db_session)
    script, [segment] = await _approved_script(db_session, project, ["Hello world."])
    await _enqueue_and_run(db_session, storage, project, script, key="first")
    first_audio = await audio_repo.get_by_segment_id(db_session, segment.id)
    assert first_audio is not None
    cleanup.append((first_audio.storage_bucket, first_audio.storage_key))

    calls = 0
    real = real_synthesize_to_wav_file

    async def _spy(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await real(*args, **kwargs)

    monkeypatch.setattr("edutoon.services.script_audio.tts_provider.synthesize_to_wav_file", _spy)

    await _enqueue_and_run(db_session, storage, project, script, key="second")

    assert calls == 0  # already-valid audio is never re-synthesised
    second_audio = await audio_repo.get_by_segment_id(db_session, segment.id)
    assert second_audio is not None
    assert second_audio.id == first_audio.id
    assert second_audio.updated_at == first_audio.updated_at


async def test_stale_audio_with_a_mismatched_hash_is_regenerated(db_session, storage, cleanup):
    project = await _project(db_session)
    script, [segment] = await _approved_script(db_session, project, ["Hello world."])
    # Seed a row that *looks* ready but carries a content hash that can never
    # match what this segment's real text/voice/settings would compute - the
    # same shape as text or settings having changed since this audio was made.
    stale = await audio_repo.upsert(
        db_session,
        script_segment_id=segment.id,
        status="ready",
        voice_model=get_settings().PIPER_VOICE_EN_GB,
        is_temporary_voice_substitute=False,
        content_hash="0" * 64,
        storage_bucket=BUCKET,
        storage_key="projects/nonexistent/stale.wav",
        format="wav",
        sample_rate=22050,
        channels=1,
        measured_duration_ms=1,
    )

    await _enqueue_and_run(db_session, storage, project, script)

    refreshed = await audio_repo.get_by_segment_id(db_session, segment.id)
    assert refreshed is not None
    cleanup.append((refreshed.storage_bucket, refreshed.storage_key))
    assert refreshed.content_hash != stale.content_hash
    assert refreshed.storage_key != stale.storage_key
    assert (
        await storage.object_exists(bucket=refreshed.storage_bucket, key=refreshed.storage_key)
        is True
    )


async def test_a_failed_segment_is_retried_and_succeeds_on_the_next_run(
    db_session, storage, cleanup, monkeypatch
):
    project = await _project(db_session)
    script, [segment] = await _approved_script(db_session, project, ["Hello world."])

    async def _always_fails(*args, **kwargs):
        raise RuntimeError("synthesis backend unavailable")

    monkeypatch.setattr(
        "edutoon.services.script_audio.tts_provider.synthesize_to_wav_file", _always_fails
    )
    first_job = await _enqueue_and_run(db_session, storage, project, script, key="attempt-1")
    assert first_job.status == "failed"
    failed_audio = await audio_repo.get_by_segment_id(db_session, segment.id)
    assert failed_audio is not None and failed_audio.status == "failed"
    assert "unavailable" not in (failed_audio.error or "")  # sanitised, never the raw message

    monkeypatch.undo()
    second_job = await _enqueue_and_run(db_session, storage, project, script, key="attempt-2")

    assert second_job.status == "succeeded"
    retried_audio = await audio_repo.get_by_segment_id(db_session, segment.id)
    assert retried_audio is not None
    cleanup.append((retried_audio.storage_bucket, retried_audio.storage_key))
    assert retried_audio.status == "ready"


async def test_partial_failure_never_sets_the_scripts_measured_duration(
    db_session, storage, cleanup, monkeypatch
):
    project = await _project(db_session)
    script, [ok_segment, bad_segment] = await _approved_script(
        db_session, project, ["This one succeeds.", "This one will fail."]
    )

    real = real_synthesize_to_wav_file

    async def _fail_one(*, voices_dir, voice_model, chunks, wav_path):  # noqa: ANN001
        if "fail" in chunks[0]:
            raise RuntimeError("forced failure for this segment")
        return await real(
            voices_dir=voices_dir, voice_model=voice_model, chunks=chunks, wav_path=wav_path
        )

    monkeypatch.setattr(
        "edutoon.services.script_audio.tts_provider.synthesize_to_wav_file", _fail_one
    )

    job = await _enqueue_and_run(db_session, storage, project, script)

    assert job.status == "succeeded"  # at least one segment made progress
    assert job.result is not None
    assert job.result["ready_segment_count"] == 1
    assert str(bad_segment.id) in job.result["failed_segment_ids"]

    ok_audio = await audio_repo.get_by_segment_id(db_session, ok_segment.id)
    assert ok_audio is not None and ok_audio.status == "ready"
    cleanup.append((ok_audio.storage_bucket, ok_audio.storage_key))
    bad_audio = await audio_repo.get_by_segment_id(db_session, bad_segment.id)
    assert bad_audio is not None and bad_audio.status == "failed"

    finished = await scripts_repo.get_by_id_for_project(db_session, script.id, project.id)
    assert finished is not None
    assert finished.measured_duration_ms is None  # never presented as complete


async def test_every_segment_failing_fails_the_whole_job(db_session, storage, monkeypatch):
    project = await _project(db_session)
    script, [segment] = await _approved_script(db_session, project, ["Hello world."])

    async def _always_fails(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "edutoon.services.script_audio.tts_provider.synthesize_to_wav_file", _always_fails
    )

    job = await _enqueue_and_run(db_session, storage, project, script)

    assert job.status == "failed"
    assert job.error is not None


async def test_a_missing_voice_file_fails_the_whole_job_cleanly(db_session, storage, tmp_path):
    project = await _project(db_session)
    script, _ = await _approved_script(db_session, project, ["Hello world."])
    enqueued = await service.enqueue_synthesis(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k",
    )
    await db_session.commit()

    job = await service.run_synthesise_voice_job(
        db_session,
        storage,
        job_id=enqueued.job.id,
        voices_dir=tmp_path,  # no voice files here
    )

    assert job.status == "failed"
    assert job.error is not None
    assert "onnx" not in job.error  # sanitised - never a filesystem path or internal detail


# --- playback returns real audio -------------------------------------------------------------


async def test_playback_returns_the_real_synthesised_audio(db_session, storage, cleanup):
    project = await _project(db_session)
    script, [segment] = await _approved_script(db_session, project, ["Playback check."])
    await _enqueue_and_run(db_session, storage, project, script)
    audio = await audio_repo.get_by_segment_id(db_session, segment.id)
    assert audio is not None
    cleanup.append((audio.storage_bucket, audio.storage_key))

    data, played_audio = await service.get_segment_audio_for_playback(
        db_session,
        storage,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        segment_id=segment.id,
        range_header=None,
    )

    assert played_audio.id == audio.id
    assert data.status_code == 200
    assert data.content_type == "audio/wav"
    duration_ms = await _wav_duration_ms(data.body)
    assert duration_ms == audio.measured_duration_ms


async def test_playback_honours_a_range_request(db_session, storage, cleanup):
    project = await _project(db_session)
    script, [segment] = await _approved_script(
        db_session, project, ["A somewhat longer sentence so there is real audio to range over."]
    )
    await _enqueue_and_run(db_session, storage, project, script)
    audio = await audio_repo.get_by_segment_id(db_session, segment.id)
    assert audio is not None
    cleanup.append((audio.storage_bucket, audio.storage_key))

    data, _ = await service.get_segment_audio_for_playback(
        db_session,
        storage,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        segment_id=segment.id,
        range_header="bytes=0-999",
    )

    assert data.status_code == 206
    assert data.content_range is not None
    assert len(data.body) == 1000


# --- progress reporting through short, independent sessions ---------------------------------


@pytest.fixture
async def worker_sessionmaker(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """Mirrors ``test_worker.py``'s fixture of the same name: every session this
    binds shares ``db_session``'s own connection/SAVEPOINT, so nothing written
    through it persists past the test either.
    """
    connection = await db_session.connection()
    return async_sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


async def test_progress_is_reported_through_the_sessionmaker(
    db_session, storage, cleanup, worker_sessionmaker
):
    project = await _project(db_session)
    script, [segment] = await _approved_script(db_session, project, ["Hello world."])
    enqueued = await service.enqueue_synthesis(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k",
    )
    await db_session.commit()

    await service.run_synthesise_voice_job(
        db_session,
        storage,
        job_id=enqueued.job.id,
        voices_dir=VOICES_DIR,
        sessionmaker=worker_sessionmaker,
    )

    audio = await audio_repo.get_by_segment_id(db_session, segment.id)
    assert audio is not None
    cleanup.append((audio.storage_bucket, audio.storage_key))
    finished_job = await jobs_repo.get_by_id(db_session, enqueued.job.id)
    assert finished_job is not None
    # The final payload still carries the *last* progress stage reported -
    # proof the short-session writes actually happened, not just the final result.
    assert finished_job.payload.get("progress", {}).get("stage") == "finalising"
