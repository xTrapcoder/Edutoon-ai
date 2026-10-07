"""``services/render.py`` - Phase 10's render gate, resumable chunked job, partial re-render,
failure isolation, crash recovery, status read model and cleanup.

Real Postgres (rolled back per test), real MinIO, real FFmpeg and the real Pillow renderer -
only the narration is generated audio (see ``render_helpers``). Renders use the ``low`` quality
(half size) where the test is about orchestration rather than pixels, so the suite stays quick.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from edutoon.core.config import get_settings
from edutoon.core.errors import (
    ConflictError,
    InsufficientStorageError,
    NotFoundError,
    ValidationError,
)
from edutoon.domain import render as render_domain
from edutoon.providers.storage import Storage, get_storage_client
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import project_topics as topics_repo
from edutoon.repositories import render as render_repo
from edutoon.repositories import script_segment_audio as audio_repo
from edutoon.repositories import script_segment_scenes as scenes_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.script_segment_scenes import NewScene
from edutoon.services import projects as projects_service
from edutoon.services import render as render_service
from tests.render_helpers import (
    SAMPLE_RATE,
    Built,
    CountingRenderer,
    build_project,
    label_of,
    remove_objects,
    requires_ffmpeg,
    scene_content,
    tone_hz,
    wav_bytes,
)

pytestmark = requires_ffmpeg

TOTAL_SCENES = 6


class WorkerKilled(BaseException):
    """A worker process dying: deliberately not an ``Exception`` so no handler can swallow it."""


# --- fixtures ---


@pytest.fixture(autouse=True)
def _one_chunk_at_a_time(monkeypatch: Any) -> None:
    settings = get_settings().model_copy(update={"RENDER_CONCURRENCY": 1})
    monkeypatch.setattr(render_service, "get_settings", lambda: settings)


def _with_settings(monkeypatch: Any, **overrides: Any) -> None:
    settings = render_service.get_settings().model_copy(update=overrides)
    monkeypatch.setattr(render_service, "get_settings", lambda: settings)


async def _age_job(session, job_id, *, by: timedelta = timedelta(hours=2)) -> None:
    """Make a job look as though its worker died ``by`` ago and left it ``running``.

    Recovery measures a job's silence from the later of its start and its last progress report, so
    both move back; moving only the start would leave the last heartbeat fresh and the job - rightly
    - alive."""
    job = await jobs_repo.get_by_id(session, job_id)
    assert job is not None
    when = datetime.now(UTC) - by
    payload = dict(job.payload)
    payload["progress"] = {**(payload.get("progress") or {}), "updated_at": when.isoformat()}
    await jobs_repo.update(
        session, job_id=job_id, status="running", started_at=when, payload=payload
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
async def worker_sessionmaker(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    connection = await db_session.connection()
    return async_sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


@pytest.fixture
async def make_built(db_session, storage) -> AsyncIterator[Callable[..., Awaitable[Built]]]:
    made: list[Built] = []

    async def _make(**kwargs: Any) -> Built:
        built = await build_project(db_session, storage, **kwargs)
        made.append(built)
        return built

    yield _make
    for built in made:
        await remove_objects(db_session, storage, built)


@pytest.fixture
async def built(make_built) -> Built:
    return await make_built()


@pytest.fixture
def renderer() -> CountingRenderer:
    return CountingRenderer()


async def _enqueue(db_session, built: Built, *, orientation="horizontal", quality="low", key=None):
    enqueued = await render_service.enqueue_render(
        db_session,
        project_id=built.project.id,
        owner_id=built.project.owner_id,
        script_id=built.script.id,
        orientation=orientation,
        quality=quality,
        idempotency_key=key or str(uuid4()),
    )
    await db_session.commit()
    return enqueued


async def _render(
    db_session,
    storage,
    renderer,
    sessionmaker,
    built: Built,
    *,
    orientation="horizontal",
    quality="low",
):
    enqueued = await _enqueue(db_session, built, orientation=orientation, quality=quality)
    job = await render_service.run_render_video_job(
        db_session, storage, renderer, job_id=enqueued.job.id, sessionmaker=sessionmaker
    )
    return enqueued, job


async def _status(db_session, built: Built, *, orientation="horizontal", quality="low"):
    return await render_service.get_render_status(
        db_session,
        project_id=built.project.id,
        owner_id=built.project.owner_id,
        script_id=built.script.id,
        orientation=orientation,
        quality=quality,
    )


# --- the prerequisite gate ---


async def test_only_an_approved_script_can_be_rendered(db_session, built):
    from edutoon.repositories import scripts as scripts_repo

    await scripts_repo.set_status(
        db_session, project_id=built.project.id, script_id=built.script.id, status="draft"
    )
    with pytest.raises(ConflictError, match="approved"):
        await _enqueue(db_session, built)


async def test_narration_must_be_fully_measured(db_session, built):
    await db_session.execute(
        text("UPDATE scripts SET measured_duration_ms = NULL WHERE id = :i"),
        {"i": built.script.id},
    )
    with pytest.raises(ValidationError, match="narration is not fully recorded"):
        await _enqueue(db_session, built)


async def test_every_segment_needs_ready_audio(db_session, built):
    segment = built.segments[1]
    await audio_repo.upsert(
        db_session,
        script_segment_id=segment.id,
        status="failed",
        voice_model="v",
        is_temporary_voice_substitute=False,
        content_hash="x",
        error="e",
    )
    with pytest.raises(ValidationError, match=r"\(2\) has no narration audio"):
        await _enqueue(db_session, built)


async def test_stale_narration_is_refused(db_session, built):
    # The text changed after the audio was recorded, so the recorded audio no longer matches.
    await db_session.execute(text("ALTER TABLE script_segments DISABLE TRIGGER ALL"))
    await db_session.execute(
        text(
            "UPDATE script_segments SET narration_text = 'Completely different words.' "
            "WHERE id = :i"
        ),
        {"i": built.segments[0].id},
    )
    with pytest.raises(ValidationError, match=r"out of\s+date|out of date"):
        await _enqueue(db_session, built)


async def test_an_incomplete_storyboard_is_refused_before_any_work(db_session, built, renderer):
    await scenes_repo.replace_for_segment(
        db_session, script_segment_id=built.segments[2].id, scenes=[]
    )
    with pytest.raises(ValidationError, match="storyboard is incomplete"):
        await _enqueue(db_session, built)
    assert renderer.chunks == []


async def test_a_storyboard_made_for_older_narration_is_refused(db_session, built):
    audio = built.audio[built.segments[0].id]
    await audio_repo.upsert(
        db_session,
        script_segment_id=built.segments[0].id,
        status="ready",
        storage_bucket=audio.storage_bucket,
        storage_key=audio.storage_key,
        format="wav",
        sample_rate=SAMPLE_RATE,
        channels=1,
        measured_duration_ms=audio.measured_duration_ms,
        voice_model=audio.voice_model,
        is_temporary_voice_substitute=False,
        content_hash="a-different-hash",
    )
    with pytest.raises(ValidationError):
        await _enqueue(db_session, built)


async def test_scenes_that_do_not_tile_their_segment_are_refused(db_session, built):
    segment = built.segments[0]
    audio = built.audio[segment.id]
    duration = audio.measured_duration_ms
    await scenes_repo.replace_for_segment(
        db_session,
        script_segment_id=segment.id,
        scenes=[
            NewScene(0, 0, duration - 500, "title_card", {"title": "A"}, audio.content_hash),
            NewScene(1, duration - 500, 400, "title_card", {"title": "B"}, audio.content_hash),
        ],
    )
    with pytest.raises(ValidationError, match="add up to its narration"):
        await _enqueue(db_session, built)


async def test_a_video_longer_than_the_maximum_is_refused_with_the_limit(
    db_session, built, monkeypatch
):
    _with_settings(monkeypatch, MAX_RENDER_DURATION_MS=5_000)
    with pytest.raises(ValidationError, match="maximum for one render is"):
        await _enqueue(db_session, built)


async def test_a_render_is_refused_cleanly_when_the_disk_is_too_full(
    db_session, built, monkeypatch
):
    class _Usage:
        free = 1024

    monkeypatch.setattr(render_service.shutil, "disk_usage", lambda _path: _Usage())
    with pytest.raises(InsufficientStorageError, match="not enough free disk space"):
        await _enqueue(db_session, built)
    assert (
        await jobs_repo.get_latest_by_project_kind(
            db_session, project_id=built.project.id, kind="render_video"
        )
        is None
    )


async def test_another_owners_project_and_a_foreign_script_are_not_found(db_session, built):
    stranger = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    with pytest.raises(NotFoundError):
        await render_service.enqueue_render(
            db_session,
            project_id=built.project.id,
            owner_id=stranger.id,
            script_id=built.script.id,
            orientation="horizontal",
            quality="low",
            idempotency_key="k",
        )
    with pytest.raises(NotFoundError):
        await render_service.enqueue_render(
            db_session,
            project_id=built.project.id,
            owner_id=built.project.owner_id,
            script_id=uuid4(),
            orientation="horizontal",
            quality="low",
            idempotency_key="k2",
        )
    with pytest.raises(NotFoundError):
        await render_service.get_render_status(
            db_session,
            project_id=built.project.id,
            owner_id=stranger.id,
            script_id=built.script.id,
            orientation="horizontal",
            quality="low",
        )


async def test_a_second_render_while_one_is_active_is_a_conflict(db_session, built):
    await _enqueue(db_session, built, key="first")
    with pytest.raises(ConflictError, match="already queued or running"):
        await _enqueue(db_session, built, key="second")


async def test_the_same_idempotency_key_replays_the_same_job(db_session, built):
    first = await _enqueue(db_session, built, key="same")
    again = await _enqueue(db_session, built, key="same")
    assert again.replayed is True and again.job.id == first.job.id
    assert again.estimate.chunk_count == first.estimate.chunk_count == TOTAL_SCENES


async def test_the_estimate_is_shown_before_any_work(db_session, built):
    enqueued = await _enqueue(db_session, built)
    estimate = enqueued.estimate
    assert estimate.chunk_count == TOTAL_SCENES
    assert estimate.chunks_to_render == TOTAL_SCENES and estimate.chunks_reusable == 0
    assert (estimate.width, estimate.height, estimate.fps) == (640, 360, 30)
    assert estimate.total_ms == sum(a.measured_duration_ms for a in built.audio.values())


# --- a full render ---


def _ffprobe_json(path: Path) -> dict[str, Any]:
    import json

    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        check=True,
    ).stdout
    return json.loads(out)  # type: ignore[no-any-return]


def _packets(path: Path) -> list[tuple[int, bool]]:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=pts,flags",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    rows = [line.split(",") for line in out.split() if line]
    return [(int(pts), "K" in flags) for pts, flags in rows]


def _decode_audio(path: Path) -> np.ndarray:
    out = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    return np.frombuffer(out, dtype=np.int16)


def _zero_crossing_hz(samples: np.ndarray) -> float:
    if len(samples) < 2:
        return 0.0
    signs = np.signbit(samples)
    crossings = int(np.count_nonzero(signs[1:] != signs[:-1]))
    return crossings / 2 / (len(samples) / SAMPLE_RATE)


async def _download_output(
    db_session, storage: Storage, built: Built, tmp_path: Path, **key: str
) -> Path:
    output = await render_repo.get_output(
        db_session,
        project_id=built.project.id,
        script_id=built.script.id,
        orientation=key.get("orientation", "horizontal"),
        quality=key.get("quality", "low"),
    )
    assert output is not None
    dest = tmp_path / "out.mp4"
    await storage.download_file(bucket=output.storage_bucket, key=output.storage_key, dest=dest)
    return dest


async def test_a_full_render_produces_a_verified_mp4_synchronised_with_the_narration(
    db_session, storage, renderer, worker_sessionmaker, built, tmp_path
):
    enqueued, job = await _render(db_session, storage, renderer, worker_sessionmaker, built)

    assert job.status == "succeeded", job.error
    assert job.result["outcome"] == "complete"
    assert job.result["chunk_total"] == TOTAL_SCENES == job.result["chunks_rendered"]
    assert job.result["chunks_reused"] == 0

    path = await _download_output(db_session, storage, built, tmp_path)
    info = _ffprobe_json(path)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert (video["codec_name"], video["profile"], video["pix_fmt"]) == ("h264", "High", "yuv420p")
    assert (video["width"], video["height"], video["r_frame_rate"]) == (640, 360, "30/1")
    assert video["avg_frame_rate"] == "30/1"  # constant frame rate
    assert video["level"] == 30  # H.264 level 3.0: the lowest valid one for 640x360 at 30 fps
    assert (audio["codec_name"], audio["sample_rate"], audio["channels"]) == ("aac", "22050", 1)

    total_ms = sum(a.measured_duration_ms for a in built.audio.values())
    frames = render_domain.frame_at(total_ms)
    packets = _packets(path)
    assert len(packets) == frames  # exactly the frames the absolute rule calls for
    assert {b[0] - a[0] for a, b in zip(packets, packets[1:], strict=False)} == {
        1000
    }  # no gap/overlap
    assert abs(float(video["duration"]) * 1000 - total_ms) <= render_domain.DURATION_TOLERANCE_MS
    assert abs(float(audio["duration"]) * 1000 - total_ms) <= render_domain.DURATION_TOLERANCE_MS

    # Every scene starts on the exact absolute frame of its time: chunks begin on keyframes.
    plan_starts = _chunk_start_frames(built)
    keyframes = {index for index, (_, is_key) in enumerate(packets) if is_key}
    assert set(plan_starts) <= keyframes

    # And the narration switches from one segment to the next where the timeline says.
    samples = _decode_audio(path)
    boundary = 0
    for index, segment in enumerate(built.segments):
        duration = built.audio[segment.id].measured_duration_ms
        if index > 0:
            window = samples[
                int((boundary + 100) / 1000 * SAMPLE_RATE) : int(
                    (boundary + 300) / 1000 * SAMPLE_RATE
                )
            ]
            assert abs(_zero_crossing_hz(window) - tone_hz(index)) < 25
            before = samples[
                int((boundary - 300) / 1000 * SAMPLE_RATE) : int(
                    (boundary - 100) / 1000 * SAMPLE_RATE
                )
            ]
            assert abs(_zero_crossing_hz(before) - tone_hz(index - 1)) < 25
            assert _switch_ms(samples, boundary, tone_hz(index)) == pytest.approx(boundary, abs=20)
        boundary += duration

    status = await _status(db_session, built)
    assert status.is_complete and status.output_is_current
    assert (status.ready_count, status.needs_render_count, status.stale_count) == (
        TOTAL_SCENES,
        0,
        0,
    )
    assert status.output is not None and status.output.duration_ms == total_ms


def _switch_ms(samples: np.ndarray, around_ms: int, new_hz: float) -> float:
    """When (ms) the audio around ``around_ms`` switches to ``new_hz``, by 5 ms windows."""
    step = int(0.005 * SAMPLE_RATE)
    start = int((around_ms - 150) / 1000 * SAMPLE_RATE)
    for offset in range(0, int(0.3 * SAMPLE_RATE), step):
        window = samples[start + offset : start + offset + step]
        if abs(_zero_crossing_hz(window) - new_hz) < new_hz * 0.2:
            return (start + offset) / SAMPLE_RATE * 1000
    return -1.0


def _chunk_start_frames(built: Built) -> list[int]:
    starts, cursor = [], 0
    for index, segment in enumerate(built.segments):
        duration = built.audio[segment.id].measured_duration_ms
        n = len(built.labels[index])
        base, rem = divmod(duration, n)
        for scene in range(n):
            starts.append(render_domain.frame_at(cursor))
            cursor += base + (1 if scene < rem else 0)
    return starts


async def test_a_second_render_with_nothing_changed_does_no_work(
    db_session, storage, renderer, worker_sessionmaker, built
):
    await _render(db_session, storage, renderer, worker_sessionmaker, built)
    drawn, assemblies = len(renderer.chunks), renderer.assemblies

    _, job = await _render(db_session, storage, renderer, worker_sessionmaker, built)

    assert job.status == "succeeded" and job.result["output_reused"] is True
    assert len(renderer.chunks) == drawn and renderer.assemblies == assemblies


# --- partial re-render: only what depends on the change ---


async def _replace_scene(db_session, built: Built, segment_index: int, scene_index: int, content):
    segment = built.segments[segment_index]
    existing = await scenes_repo.list_by_segment_id(db_session, segment.id)
    await scenes_repo.replace_for_segment(
        db_session,
        script_segment_id=segment.id,
        scenes=[
            NewScene(
                s.scene_index,
                s.start_ms,
                s.duration_ms,
                s.template,
                content if s.scene_index == scene_index else s.content,
                s.source_audio_content_hash,
            )
            for s in existing
        ],
    )


async def _narration_bytes(storage: Storage, audio_by_segment) -> dict:
    """The stored narration audio itself, per segment - what a render must never rewrite."""
    return {
        segment_id: (
            await storage.get_object(bucket=audio.storage_bucket, key=audio.storage_key)
        ).body
        for segment_id, audio in audio_by_segment.items()
    }


async def test_editing_one_scene_re_renders_only_that_scene_and_never_the_narration(
    db_session, storage, renderer, worker_sessionmaker, built
):
    await _render(db_session, storage, renderer, worker_sessionmaker, built)
    audio_before = {
        s.id: await audio_repo.get_by_segment_id(db_session, s.id) for s in built.segments
    }
    audio_bytes_before = await _narration_bytes(storage, audio_before)
    renderer.chunks.clear()

    await _replace_scene(
        db_session, built, 1, 1, scene_content("split_text_illustration", "EditedScene")
    )
    status = await _status(db_session, built)
    states = {s.position: s.state for s in status.scenes}
    assert [p for p, st in states.items() if st != "ready"] == [3]
    assert states[3] == "stale"
    assert status.is_complete is False and status.output_is_current is False
    assert status.stale_count == 1 and status.ready_count == TOTAL_SCENES - 1

    enqueued, job = await _render(db_session, storage, renderer, worker_sessionmaker, built)

    assert enqueued.estimate.chunks_to_render == 1
    assert enqueued.estimate.chunks_reusable == TOTAL_SCENES - 1
    assert renderer.chunks == ["EditedScene"]  # nothing else was drawn
    assert job.result["chunks_rendered"] == 1 and job.result["chunks_reused"] == TOTAL_SCENES - 1
    assert (await _status(db_session, built)).is_complete is True
    for segment in built.segments:  # narration is untouched: same rows, never rewritten
        after = await audio_repo.get_by_segment_id(db_session, segment.id)
        assert after == audio_before[segment.id]
    assert await _narration_bytes(storage, audio_before) == audio_bytes_before  # nor its audio
    assert (
        await jobs_repo.get_latest_by_project_kind(
            db_session, project_id=built.project.id, kind="synthesise_voice"
        )
        is None
    )  # and a render never asks for narration to be made again


async def test_renarrating_one_segment_re_renders_only_its_scenes_even_though_later_ones_move(
    db_session, storage, renderer, worker_sessionmaker, built
):
    await _render(db_session, storage, renderer, worker_sessionmaker, built)
    renderer.chunks.clear()
    settings = get_settings()

    # Simulate the real path: segment 1 is narrated again, coming out longer (new audio, a new
    # content hash, a new measured duration), and its storyboard is regenerated. Segment 2's
    # scenes are untouched - they are simply later in the video now.
    segment = built.segments[1]
    old = built.audio[segment.id]
    new_duration = old.measured_duration_ms + 1_333
    data = wav_bytes(new_duration, frequency=tone_hz(1))
    await storage.put_object(
        bucket=settings.BUCKET_SEGMENTS, key=old.storage_key, body=data, content_type="audio/wav"
    )
    new_audio = await audio_repo.upsert(
        db_session,
        script_segment_id=segment.id,
        status="ready",
        storage_bucket=old.storage_bucket,
        storage_key=old.storage_key,
        format="wav",
        sample_rate=SAMPLE_RATE,
        channels=1,
        measured_duration_ms=new_duration,
        voice_model=old.voice_model,
        is_temporary_voice_substitute=False,
        content_hash="renarrated-hash",
    )
    total = sum(
        (new_audio if s.id == segment.id else built.audio[s.id]).measured_duration_ms
        for s in built.segments
    )
    from edutoon.repositories import scripts as scripts_repo

    await scripts_repo.set_measured_duration(
        db_session,
        project_id=built.project.id,
        script_id=built.script.id,
        measured_duration_ms=total,
    )
    # Fake the stale-narration check's view of the text by making the hash what it expects.
    from edutoon.domain.script_audio import compute_content_hash
    from edutoon.providers import tts as tts_provider
    from edutoon.services import script_audio as script_audio_service

    voice = script_audio_service.voice_selection_from_settings(settings, built.project.language)
    expected = compute_content_hash(
        narration_text=segment.narration_text,
        voice_model=voice.voice_model,
        engine_version=tts_provider.engine_version(),
        sample_rate=SAMPLE_RATE,
    )
    new_audio = await audio_repo.upsert(
        db_session,
        script_segment_id=segment.id,
        status="ready",
        storage_bucket=old.storage_bucket,
        storage_key=old.storage_key,
        format="wav",
        sample_rate=SAMPLE_RATE,
        channels=1,
        measured_duration_ms=new_duration,
        voice_model=old.voice_model,
        is_temporary_voice_substitute=False,
        content_hash=expected,
    )
    from edutoon.domain.storyboard_timing import allocate_scene_durations, scene_timings

    timings = scene_timings(
        built.audio[built.segments[0].id].measured_duration_ms,
        allocate_scene_durations(new_duration, 2),
    )
    existing = await scenes_repo.list_by_segment_id(db_session, segment.id)
    await scenes_repo.replace_for_segment(
        db_session,
        script_segment_id=segment.id,
        scenes=[
            NewScene(
                t.scene_index,
                t.start_ms,
                t.duration_ms,
                e.template,
                e.content,
                new_audio.content_hash,
            )
            for t, e in zip(timings, existing, strict=True)
        ],
    )
    # The old audio's hash was different, so this segment's scenes are new work; segment 2 only
    # moved, and its scenes' stored start_ms are now stale - which must not matter.

    status = await _status(db_session, built)
    assert [s.state for s in status.scenes if s.segment_index == 1] == ["stale", "stale"]
    assert [s.state for s in status.scenes if s.segment_index != 1] == ["ready"] * 4

    enqueued, job = await _render(db_session, storage, renderer, worker_sessionmaker, built)

    assert job.status == "succeeded", job.error
    assert sorted(renderer.chunks) == ["Seg1Scene0", "Seg1Scene1"]
    assert job.result["chunks_reused"] == 4
    assert (await _status(db_session, built)).is_complete is True


async def test_editing_every_scene_of_one_topic_touches_only_that_topics_segments(
    db_session, storage, renderer, worker_sessionmaker, built
):
    await _render(db_session, storage, renderer, worker_sessionmaker, built)
    renderer.chunks.clear()
    # A "topic" is just the set of segments written for it: change segments 0 and 1.
    for seg, templates in (
        (0, ["title_card", "bullet_points"]),
        (1, ["labelled_diagram", "split_text_illustration"]),
    ):
        for n, template in enumerate(templates):
            await _replace_scene(
                db_session, built, seg, n, scene_content(template, f"Topic1-{seg}-{n}")
            )
    _, job = await _render(db_session, storage, renderer, worker_sessionmaker, built)
    assert sorted(renderer.chunks) == sorted(f"Topic1-{s}-{n}" for s in (0, 1) for n in (0, 1))
    assert job.result["chunks_reused"] == 2


async def test_orientation_and_quality_change_only_video_never_narration(
    db_session, storage, renderer, worker_sessionmaker, built
):
    await _render(db_session, storage, renderer, worker_sessionmaker, built)
    audio_before = [await audio_repo.get_by_segment_id(db_session, s.id) for s in built.segments]
    scenes_before = [await scenes_repo.list_by_segment_id(db_session, s.id) for s in built.segments]
    renderer.chunks.clear()

    _, vertical = await _render(
        db_session, storage, renderer, worker_sessionmaker, built, orientation="vertical"
    )
    assert vertical.status == "succeeded" and len(renderer.chunks) == TOTAL_SCENES
    renderer.chunks.clear()
    _, high = await _render(
        db_session, storage, renderer, worker_sessionmaker, built, quality="high"
    )
    assert high.status == "succeeded" and len(renderer.chunks) == TOTAL_SCENES

    # The original configuration is still complete, untouched by the other two.
    assert (await _status(db_session, built)).is_complete is True
    assert (await _status(db_session, built, orientation="vertical")).is_complete is True
    assert (await _status(db_session, built, quality="high")).output.width == 1280
    vertical_output = (await _status(db_session, built, orientation="vertical")).output
    assert (vertical_output.width, vertical_output.height) == (360, 640)
    assert audio_before == [
        await audio_repo.get_by_segment_id(db_session, s.id) for s in built.segments
    ]
    assert scenes_before == [
        await scenes_repo.list_by_segment_id(db_session, s.id) for s in built.segments
    ]
    # Same storyboard, same narration timeline: every output has the same duration.
    durations = {
        (await _status(db_session, built, orientation=o, quality=q)).output.duration_ms
        for o, q in (("horizontal", "low"), ("vertical", "low"), ("horizontal", "high"))
    }
    assert len(durations) == 1


# --- failure isolation, retry, crash recovery ---


async def test_one_failed_chunk_keeps_the_rest_and_a_retry_renders_only_it(
    db_session, storage, renderer, worker_sessionmaker, built
):
    renderer.fail_label = "Seg1Scene0"
    enqueued, job = await _render(db_session, storage, renderer, worker_sessionmaker, built)

    assert job.status == "failed"
    assert "1 render section needs another attempt" in job.error
    assert job.result["outcome"] == "chunks_failed" and len(job.result["failed_chunks"]) == 1
    assert "boom" in job.result["failed_chunks"][0]["error"]
    assert len(renderer.chunks) == TOTAL_SCENES - 1
    assert renderer.assemblies == 0  # nothing is assembled from an incomplete set

    status = await _status(db_session, built)
    assert status.failed_count == 1 and status.ready_count == TOTAL_SCENES - 1
    assert status.is_complete is False and status.output is None
    assert [s.state for s in status.scenes if s.position == 2] == ["failed"]

    renderer.fail_label = None
    renderer.chunks.clear()
    retried = await render_service.retry_render(
        db_session,
        project_id=built.project.id,
        owner_id=built.project.owner_id,
        idempotency_key="retry",
    )
    await db_session.commit()
    assert retried.estimate.chunks_to_render == 1
    job2 = await render_service.run_render_video_job(
        db_session, storage, renderer, job_id=retried.job.id, sessionmaker=worker_sessionmaker
    )
    assert job2.status == "succeeded", job2.error
    assert renderer.chunks == ["Seg1Scene0"]
    assert (await _status(db_session, built)).is_complete is True


async def test_retry_is_refused_when_the_last_render_did_not_fail(db_session, built):
    with pytest.raises(ConflictError, match="no failed render"):
        await render_service.retry_render(
            db_session,
            project_id=built.project.id,
            owner_id=built.project.owner_id,
            idempotency_key="r",
        )


async def test_a_worker_killed_mid_render_loses_no_finished_chunk(
    db_session, storage, renderer, worker_sessionmaker, built, monkeypatch
):
    _with_settings(monkeypatch, RENDER_STALE_AFTER_SECONDS=60)
    enqueued = await _enqueue(db_session, built)
    renderer.die_after = 3
    original = renderer.render_chunk

    async def _dying(spec, dest):
        if len(renderer.chunks) >= 3:
            raise WorkerKilled()
        return await original(spec, dest)

    renderer.render_chunk = _dying  # type: ignore[method-assign]
    with pytest.raises(WorkerKilled):
        await render_service.run_render_video_job(
            db_session, storage, renderer, job_id=enqueued.job.id, sessionmaker=worker_sessionmaker
        )
    assert len(renderer.chunks) == 3
    stored = await render_repo.chunks_by_hashes(
        db_session,
        project_id=built.project.id,
        hashes=set(await _plan_hashes(db_session, built)),
    )
    assert len(stored) == 3  # committed the moment each finished

    # The dead worker's job is left 'running'; once it is old enough it is failed, not resumed
    # in place, and a fresh render picks up exactly where it stopped.
    await _age_job(db_session, enqueued.job.id)
    renderer.render_chunk = original  # type: ignore[method-assign]
    renderer.die_after = None
    renderer.chunks.clear()
    enqueued2, job2 = await _render(db_session, storage, renderer, worker_sessionmaker, built)

    assert (await jobs_repo.get_by_id(db_session, enqueued.job.id)).status == "failed"
    assert enqueued2.estimate.chunks_to_render == TOTAL_SCENES - 3
    assert job2.status == "succeeded", job2.error
    assert len(renderer.chunks) == TOTAL_SCENES - 3
    assert (await _status(db_session, built)).is_complete is True


async def _plan_hashes(db_session, built: Built) -> list[str]:
    evaluation = await render_service._evaluate(
        db_session,
        project=built.project,
        script=built.script,
        orientation="horizontal",
        quality="low",
    )
    assert evaluation.plan is not None
    return list(evaluation.plan.chunk_hashes)


async def test_a_failed_assembly_keeps_every_chunk_and_retry_only_reassembles(
    db_session, storage, renderer, worker_sessionmaker, built
):
    renderer.fail_assembly = True
    _, job = await _render(db_session, storage, renderer, worker_sessionmaker, built)
    assert job.status == "failed" and job.result["outcome"] == "assembly_failed"
    assert "could not be assembled" in job.error
    assert len(renderer.chunks) == TOTAL_SCENES

    renderer.fail_assembly = False
    renderer.chunks.clear()
    _, job2 = await _render(db_session, storage, renderer, worker_sessionmaker, built)
    assert job2.status == "succeeded", job2.error
    assert renderer.chunks == [] and renderer.assemblies == 2  # assembled again, drew nothing


async def test_a_worker_killed_during_assembly_loses_no_chunk_and_the_next_render_only_assembles(
    db_session, storage, renderer, worker_sessionmaker, built, monkeypatch
):
    _with_settings(monkeypatch, RENDER_STALE_AFTER_SECONDS=60)
    enqueued = await _enqueue(db_session, built)
    original = renderer.assemble

    async def _dying(spec, dest):
        raise WorkerKilled()

    renderer.assemble = _dying  # type: ignore[method-assign]
    with pytest.raises(WorkerKilled):
        await render_service.run_render_video_job(
            db_session, storage, renderer, job_id=enqueued.job.id, sessionmaker=worker_sessionmaker
        )
    assert len(renderer.chunks) == TOTAL_SCENES  # every chunk was already committed
    assert (await _status(db_session, built)).output is None  # and nothing half-made is published

    await _age_job(db_session, enqueued.job.id)
    renderer.assemble = original  # type: ignore[method-assign]
    renderer.chunks.clear()
    _, job = await _render(db_session, storage, renderer, worker_sessionmaker, built)

    assert job.status == "succeeded", job.error
    assert renderer.chunks == []  # nothing drawn again
    assert (await _status(db_session, built)).is_complete is True


async def test_a_missing_or_damaged_stored_chunk_is_never_trusted(
    db_session, storage, renderer, worker_sessionmaker, built
):
    await _render(db_session, storage, renderer, worker_sessionmaker, built)
    hashes = await _plan_hashes(db_session, built)
    stored = await render_repo.chunks_by_hashes(
        db_session, project_id=built.project.id, hashes=set(hashes)
    )
    victim, damaged = stored[hashes[0]], stored[hashes[3]]
    await storage.delete_object(bucket=victim.storage_bucket, key=victim.storage_key)
    await storage.put_object(
        bucket=damaged.storage_bucket,
        key=damaged.storage_key,
        body=b"not a video",
        content_type="video/mp4",
    )
    # Something else must change so the whole-video shortcut cannot apply.
    await db_session.execute(
        text("DELETE FROM render_outputs WHERE project_id = :p"),
        {"p": built.project.id},
    )
    renderer.chunks.clear()
    _, job = await _render(db_session, storage, renderer, worker_sessionmaker, built)
    assert job.status == "succeeded", job.error
    assert sorted(renderer.chunks) == sorted(["Seg0Scene0", "Seg1Scene1"])


# --- lifecycle ---


async def test_progress_is_real_and_reported_through_the_job(
    db_session, storage, renderer, worker_sessionmaker, built
):
    seen: list[dict[str, Any]] = []
    original = jobs_repo.record_progress

    async def _spy(session, *, job_id, payload):
        if "progress" in payload:
            seen.append(payload["progress"])
        return await original(session, job_id=job_id, payload=payload)

    import edutoon.services.render as module

    module.jobs_repo.record_progress = _spy  # type: ignore[assignment]
    try:
        await _render(db_session, storage, renderer, worker_sessionmaker, built)
    finally:
        module.jobs_repo.record_progress = original  # type: ignore[assignment]

    stages = [p["stage"] for p in seen]
    assert (
        stages[0] == "checking_stored_sections" and "rendering" in stages and "assembling" in stages
    )
    done = [p["chunks_done"] for p in seen if p["stage"] == "rendering"]
    assert done == sorted(done) and max(done) == TOTAL_SCENES
    percents = [p["percent"] for p in seen]
    assert percents == sorted(percents) and percents[-1] == 100
    rendering = [p for p in seen if p["stage"] == "rendering" and p["segment_index"] is not None]
    assert rendering[0]["segment_count"] == 3 and rendering[0]["scene_count"] == 2


async def test_status_before_any_render_says_everything_needs_render(db_session, built):
    status = await _status(db_session, built)
    assert status.needs_render_count == TOTAL_SCENES and status.is_complete is False
    assert status.output is None and status.blockers == ()
    assert [s.state for s in status.scenes] == ["needs_render"] * TOTAL_SCENES


async def test_status_explains_what_is_missing_instead_of_pretending(db_session, make_built):
    built = await make_built()
    await scenes_repo.replace_for_segment(
        db_session, script_segment_id=built.segments[0].id, scenes=[]
    )
    status = await _status(db_session, built)
    assert status.scenes == () and status.is_complete is False
    assert status.blockers and "storyboard is incomplete" in status.blockers[0]


async def test_a_segment_too_short_for_a_scene_is_covered_by_the_previous_scene(
    db_session, storage, renderer, worker_sessionmaker, make_built
):
    built = await make_built(
        layout=[["title_card", "bullet_points"], [], ["formula_or_code"]],
        durations_ms=[4_000, 900, 3_000],
    )
    _, job = await _render(db_session, storage, renderer, worker_sessionmaker, built)
    assert job.status == "succeeded", job.error
    status = await _status(db_session, built)
    assert [s.duration_ms for s in status.scenes][1] == 2_000 + 900  # absorbed the short segment
    assert status.output is not None and status.output.duration_ms == 7_900


# --- project deletion ---


async def test_deleting_a_project_removes_every_render_object(
    db_session, storage, renderer, worker_sessionmaker, built
):
    await _render(db_session, storage, renderer, worker_sessionmaker, built)
    locations = await render_repo.list_storage_locations_for_project(db_session, built.project.id)
    assert len(locations) == TOTAL_SCENES + 1  # every chunk plus the finished video
    for location in locations:
        assert await storage.object_exists(bucket=location.bucket, key=location.key)

    files = await projects_service.delete_project(
        db_session, project_id=built.project.id, owner_id=built.project.owner_id
    )
    await projects_service.remove_stored_files(storage, project_id=built.project.id, files=files)

    for location in locations:
        assert not await storage.object_exists(bucket=location.bucket, key=location.key)
    assert await render_repo.list_storage_locations_for_project(db_session, built.project.id) == []


async def test_render_storage_keys_stay_inside_the_project_and_reject_bad_input():
    project_id = uuid4()
    prefix = f"projects/{project_id}/"
    assert render_domain.chunk_storage_key(project_id, "horizontal", "low", "ab12").startswith(
        prefix
    )
    assert render_domain.output_storage_key(project_id, uuid4(), "vertical", "high").startswith(
        prefix
    )
    for bad in ("../x", "a/b", "a b", ""):
        with pytest.raises(render_domain.RenderPlanError):
            render_domain.chunk_storage_key(project_id, "horizontal", "low", bad)
    with pytest.raises(render_domain.RenderPlanError):
        render_domain.chunk_storage_key(project_id, "../..", "low", "ab")
    assert label_of({"title": "x"}) == "x"


# --- transitions (Phase 11): topics decide where a scene fades and where it cuts ---------------


async def _assign_topics(db_session, built: Built, topic_of_segment: list[str]) -> None:
    topics = {}
    for position, name in enumerate(dict.fromkeys(topic_of_segment)):
        topics[name] = await topics_repo.create(
            db_session, project_id=built.project.id, title=name, position=position
        )
    for segment, name in zip(built.segments, topic_of_segment, strict=True):
        await db_session.execute(
            text("UPDATE script_segments SET topic_id = :topic WHERE id = :segment"),
            {"topic": topics[name].id, "segment": segment.id},
        )


def _expected_hash(status_scene, built: Built, fades: tuple[int, int]) -> str:
    label = built.labels[status_scene.segment_index][status_scene.scene_index]
    return render_domain.chunk_fingerprint(
        template=status_scene.template,
        content=scene_content(status_scene.template, label),
        duration_ms=status_scene.duration_ms,
        orientation="horizontal",
        quality="low",
        encoder=get_settings().RENDER_VIDEO_ENCODER,
        enter_fade_ms=fades[0],
        exit_fade_ms=fades[1],
    )


async def test_the_scenes_of_a_topic_cut_and_the_boundaries_between_topics_fade(
    db_session, make_built
):
    built = await make_built()  # three segments of two scenes each
    await _assign_topics(db_session, built, ["A", "A", "B"])
    scenes = (await _status(db_session, built)).scenes
    settings = get_settings()
    fade_in, fade_out = settings.RENDER_FADE_IN_MS, settings.RENDER_FADE_OUT_MS
    #            A opens          straight cuts           A closes        B opens         B closes
    expected = [(fade_in, 0), (0, 0), (0, 0), (0, fade_out), (fade_in, 0), (0, fade_out)]
    assert [s.chunk_hash for s in scenes] == [
        _expected_hash(scene, built, fades) for scene, fades in zip(scenes, expected, strict=True)
    ]


async def test_a_script_with_no_topics_is_one_group_only_its_two_ends_fade(db_session, make_built):
    built = await make_built()
    scenes = (await _status(db_session, built)).scenes
    settings = get_settings()
    expected = [(settings.RENDER_FADE_IN_MS, 0)] + [(0, 0)] * 4 + [(0, settings.RENDER_FADE_OUT_MS)]
    assert [s.chunk_hash for s in scenes] == [
        _expected_hash(scene, built, fades) for scene, fades in zip(scenes, expected, strict=True)
    ]


async def test_the_fade_settings_reach_the_plan_and_change_only_the_scenes_that_fade(
    db_session, make_built, monkeypatch
):
    built = await make_built()
    await _assign_topics(db_session, built, ["A", "A", "B"])
    before = (await _status(db_session, built)).scenes
    _with_settings(monkeypatch, RENDER_FADE_IN_MS=100, RENDER_FADE_OUT_MS=0)
    after = (await _status(db_session, built)).scenes
    changed = {
        i
        for i, (a, b) in enumerate(zip(before, after, strict=True))
        if a.chunk_hash != b.chunk_hash
    }
    assert changed == {
        0,
        3,
        4,
        5,
    }  # the scenes that fade in or out; the straight cuts are untouched
    assert after[0].chunk_hash == _expected_hash(after[0], built, (100, 0))
    assert after[1].chunk_hash == before[1].chunk_hash  # a straight cut has no fade to retune
