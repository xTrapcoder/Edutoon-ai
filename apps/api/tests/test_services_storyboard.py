"""``services/storyboard.py`` - Scene / Storyboard layer service (Phase 9).

``LLM.compose_storyboard_scenes`` is mocked directly here, the same pattern
``test_services_scripts.py`` uses for ``compose_script_outline`` /
``compose_script_section``: this file is about the service's own
orchestration (enqueue validation, timing, per-segment isolation,
completeness, retry, cost bound), not the provider's own request/response
parsing, which ``test_llm_storyboard.py`` already covers. A real
``LLM``/``AsyncAnthropic`` pair is still used underneath (never a real
network call).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from anthropic import AsyncAnthropic
from sqlalchemy.ext.asyncio import async_sessionmaker

from edutoon.core.config import get_settings
from edutoon.core.errors import ConflictError, NotFoundError, ValidationError
from edutoon.domain.script_audio import compute_content_hash
from edutoon.domain.storyboard_timing import (
    segment_absolute_starts,
)
from edutoon.providers import tts as tts_provider
from edutoon.providers.llm import LLM, StoryboardSceneDraft, StoryboardSceneSetDraft, TokenUsage
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import script_segment_audio as audio_repo
from edutoon.repositories import script_segment_scenes as scenes_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.scripts import NewScriptSegment
from edutoon.services import script_audio as script_audio_service
from edutoon.services import storyboard as service

# Stands in for an external provider (OpenAI / Anthropic): these tests prove how our code
# handles a response, never how a real provider behaves. See "Provider-mocked tests"
# in apps/api/README.md.
pytestmark = pytest.mark.provider_mocked

MODEL = "claude-sonnet-5"
BUCKET = "edutoon-segments"


# --- fixtures ------------------------------------------------------------------------


async def _project_with_ready_audio(
    db_session,
    *,
    narrations: list[str] | None = None,
    durations_ms: list[int] | None = None,
    orientation: str = "horizontal",
):
    """A project with an approved script whose every segment already has
    ``ready`` narration audio, and ``scripts.measured_duration_ms`` set -
    exactly the state ``enqueue_storyboard_generation`` requires, built
    without touching a real TTS engine.
    """
    durations_ms = durations_ms or [10_000, 8_000]
    narrations = narrations or [
        f"This explains part {i} of the process." for i in range(len(durations_ms))
    ]
    assert len(narrations) == len(durations_ms)

    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session,
        owner_id=owner.id,
        title="Photosynthesis",
        source_type="topic",
        orientation=orientation,
    )
    script = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)
    segments = await scripts_repo.add_segments(
        db_session,
        script_id=script.id,
        segments=[
            NewScriptSegment(segment_index=i, narration_text=text, estimated_duration_ms=d)
            for i, (text, d) in enumerate(zip(narrations, durations_ms, strict=True))
        ],
    )
    approved = await scripts_repo.set_status(
        db_session, project_id=project.id, script_id=script.id, status="approved"
    )
    assert approved is not None

    for segment, duration in zip(segments, durations_ms, strict=True):
        await audio_repo.upsert(
            db_session,
            script_segment_id=segment.id,
            status="ready",
            storage_bucket=BUCKET,
            storage_key=f"k/{segment.id}",
            format="wav",
            sample_rate=22_050,
            channels=1,
            measured_duration_ms=duration,
            voice_model="en_GB-cori-medium",
            is_temporary_voice_substitute=False,
            content_hash=f"hash-{segment.id}-{duration}",
        )

    final = await scripts_repo.set_measured_duration(
        db_session,
        project_id=project.id,
        script_id=script.id,
        measured_duration_ms=sum(durations_ms),
    )
    assert final is not None
    return owner, project, final, segments


def _llm_with(monkeypatch: Any, responses: dict[int, Any]) -> tuple[LLM, AsyncMock, list[int]]:
    """An ``LLM`` whose ``compose_storyboard_scenes`` returns
    ``responses[segment_index]`` - either a list of ``(template, content)``
    tuples, or an exception instance to raise instead.
    """
    llm = LLM(AsyncAnthropic(api_key="sk-ant-test-not-a-real-key"), model=MODEL)
    calls: list[int] = []

    async def _compose(segment, *, max_scenes: int):
        calls.append(segment.segment_index)
        response = responses[segment.segment_index]
        if isinstance(response, BaseException):
            raise response
        scenes = tuple(StoryboardSceneDraft(template=t, content=c) for t, c in response)
        return StoryboardSceneSetDraft(scenes=scenes), TokenUsage(input_tokens=5, output_tokens=5)

    mock = AsyncMock(side_effect=_compose)
    monkeypatch.setattr(llm, "compose_storyboard_scenes", mock)
    return llm, mock, calls


def _title_scene(title: str = "Intro") -> list[tuple[str, dict]]:
    return [("title_card", {"title": title})]


def _with_settings(monkeypatch: Any, **overrides: Any) -> None:
    settings = get_settings().model_copy(update=overrides)
    monkeypatch.setattr(service, "get_settings", lambda: settings)


# --- enqueue: preconditions --------------------------------------------------------


async def test_only_an_approved_script_can_be_storyboarded(db_session):
    _owner, project, script, _segments = await _project_with_ready_audio(db_session)
    await scripts_repo.set_status(
        db_session, project_id=project.id, script_id=script.id, status="draft"
    )

    with pytest.raises(ConflictError, match="approved"):
        await service.enqueue_storyboard_generation(
            db_session,
            project_id=project.id,
            owner_id=project.owner_id,
            script_id=script.id,
            idempotency_key="k",
        )


async def test_narration_must_be_fully_measured_first(db_session):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="P", source_type="topic"
    )
    script = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)
    await scripts_repo.add_segments(
        db_session,
        script_id=script.id,
        segments=[
            NewScriptSegment(segment_index=0, narration_text="Hi.", estimated_duration_ms=500)
        ],
    )
    approved = await scripts_repo.set_status(
        db_session, project_id=project.id, script_id=script.id, status="approved"
    )
    assert approved is not None

    with pytest.raises(ValidationError, match="not fully synthesised"):
        await service.enqueue_storyboard_generation(
            db_session,
            project_id=project.id,
            owner_id=owner.id,
            script_id=script.id,
            idempotency_key="k",
        )


async def test_a_missing_script_is_a_404(db_session):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="P", source_type="topic"
    )
    with pytest.raises(NotFoundError):
        await service.enqueue_storyboard_generation(
            db_session,
            project_id=project.id,
            owner_id=owner.id,
            script_id=uuid4(),
            idempotency_key="k",
        )


async def test_a_project_not_owned_by_the_caller_is_a_404(db_session):
    _owner, project, script, _segments = await _project_with_ready_audio(db_session)
    with pytest.raises(NotFoundError):
        await service.enqueue_storyboard_generation(
            db_session,
            project_id=project.id,
            owner_id=uuid4(),
            script_id=script.id,
            idempotency_key="k",
        )


# --- enqueue: cost bound, validated before any provider call -------------------------


async def test_too_many_segments_is_rejected_before_any_job_is_created(db_session, monkeypatch):
    _with_settings(monkeypatch, MAX_STORYBOARD_SEGMENTS=1)
    _owner, project, script, _segments = await _project_with_ready_audio(db_session)

    with pytest.raises(ValidationError, match="at most 1"):
        await service.enqueue_storyboard_generation(
            db_session,
            project_id=project.id,
            owner_id=project.owner_id,
            script_id=script.id,
            idempotency_key="k",
        )


async def test_too_many_total_scenes_is_rejected_before_any_job_is_created(db_session, monkeypatch):
    _with_settings(monkeypatch, MAX_SCENES_PER_SCRIPT=1)
    _owner, project, script, _segments = await _project_with_ready_audio(
        db_session, durations_ms=[10_000, 10_000]
    )

    with pytest.raises(ValidationError, match="at most 1"):
        await service.enqueue_storyboard_generation(
            db_session,
            project_id=project.id,
            owner_id=project.owner_id,
            script_id=script.id,
            idempotency_key="k",
        )


async def test_estimated_provider_calls_counts_only_segments_that_need_a_call(
    db_session, monkeypatch
):
    _with_settings(monkeypatch, MIN_SCENE_DURATION_MS=1_500)
    # One segment long enough for scenes, one too short for even one (degenerate case).
    _owner, project, script, _segments = await _project_with_ready_audio(
        db_session, durations_ms=[10_000, 500]
    )

    enq = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k",
    )

    assert enq.estimated_provider_calls == 1


# --- enqueue: idempotency and concurrency --------------------------------------------


async def test_a_repeat_call_with_the_same_key_replays_the_original_job(db_session):
    _owner, project, script, _segments = await _project_with_ready_audio(db_session)

    first = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k",
    )
    second = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k",
    )

    assert second.replayed is True
    assert second.job.id == first.job.id
    assert second.estimated_provider_calls == first.estimated_provider_calls


async def test_a_second_concurrent_generation_is_refused(db_session):
    _owner, project, script, _segments = await _project_with_ready_audio(db_session)
    await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k1",
    )

    with pytest.raises(ConflictError, match="already queued or running"):
        await service.enqueue_storyboard_generation(
            db_session,
            project_id=project.id,
            owner_id=project.owner_id,
            script_id=script.id,
            idempotency_key="k2",
        )


# --- run: happy path, exact timing ---------------------------------------------------


async def test_a_full_run_storyboards_every_segment(db_session, monkeypatch):
    _owner, project, script, segments = await _project_with_ready_audio(
        db_session, durations_ms=[9_000, 6_000]
    )
    llm, _mock, calls = _llm_with(
        monkeypatch, {0: _title_scene("Overview"), 1: _title_scene("Detail")}
    )
    enq = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k",
    )

    job = await service.run_generate_storyboard_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded"
    assert job.result["ready_segment_count"] == 2
    assert job.result["failed_segment_ids"] == []
    assert job.result["outcome"] == "complete"
    assert job.result["provider_calls"] == 2
    assert sorted(calls) == [0, 1]


async def test_scene_durations_sum_exactly_to_the_segments_measured_duration(
    db_session, monkeypatch
):
    _owner, project, script, segments = await _project_with_ready_audio(
        db_session, durations_ms=[10_001]
    )
    llm, _mock, _calls = _llm_with(
        monkeypatch, {0: [("bullet_points", {"bullets": ["Alpha step", "Beta step"]})]}
    )
    enq = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k",
    )

    await service.run_generate_storyboard_job(db_session, llm, job_id=enq.job.id)

    rows = await scenes_repo.list_by_segment_id(db_session, segments[0].id)
    assert sum(r.duration_ms for r in rows) == 10_001


async def test_scene_and_segment_positions_are_contiguous_with_no_gap_or_overlap(
    db_session, monkeypatch
):
    _owner, project, script, segments = await _project_with_ready_audio(
        db_session, durations_ms=[9_000, 6_000]
    )
    llm, _mock, _calls = _llm_with(
        monkeypatch,
        {
            0: [("title_card", {"title": "A"}), ("title_card", {"title": "B"})],
            1: _title_scene("C"),
        },
    )
    enq = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k",
    )
    await service.run_generate_storyboard_job(db_session, llm, job_id=enq.job.id)

    expected_starts = segment_absolute_starts([9_000, 6_000])
    rows_by_segment = [
        await scenes_repo.list_by_segment_id(db_session, segment.id) for segment in segments
    ]
    # First segment's two scenes: back to back, starting at its own absolute start.
    first_rows = rows_by_segment[0]
    assert first_rows[0].start_ms == expected_starts[0]
    assert first_rows[1].start_ms == first_rows[0].start_ms + first_rows[0].duration_ms
    # Second segment starts exactly where the first segment's measured audio ends.
    second_rows = rows_by_segment[1]
    assert second_rows[0].start_ms == expected_starts[1] == 9_000


async def test_a_degenerate_short_segment_gets_no_scenes_and_no_provider_call(
    db_session, monkeypatch
):
    _with_settings(monkeypatch, MIN_SCENE_DURATION_MS=1_500)
    _owner, project, script, segments = await _project_with_ready_audio(
        db_session, durations_ms=[10_000, 500]
    )
    llm, _mock, calls = _llm_with(monkeypatch, {0: _title_scene()})
    enq = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k",
    )

    job = await service.run_generate_storyboard_job(db_session, llm, job_id=enq.job.id)

    assert job.result["ready_segment_count"] == 2
    assert job.result["outcome"] == "complete"
    assert calls == [0]  # never called for the too-short segment
    assert await scenes_repo.list_by_segment_id(db_session, segments[1].id) == []


# --- run: partial failure, per-segment isolation -------------------------------------


async def test_one_segment_failing_does_not_discard_another_segments_success(
    db_session, monkeypatch
):
    _owner, project, script, segments = await _project_with_ready_audio(
        db_session, durations_ms=[9_000, 6_000]
    )
    llm, _mock, _calls = _llm_with(monkeypatch, {0: _title_scene("Good"), 1: RuntimeError("boom")})
    enq = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k",
    )

    job = await service.run_generate_storyboard_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded"
    assert job.result["outcome"] == "succeeded_with_failures"
    assert job.result["ready_segment_count"] == 1
    assert job.result["failed_segment_ids"] == [str(segments[1].id)]
    assert len(await scenes_repo.list_by_segment_id(db_session, segments[0].id)) == 1
    assert await scenes_repo.list_by_segment_id(db_session, segments[1].id) == []


async def test_every_segment_failing_fails_the_job(db_session, monkeypatch):
    _owner, project, script, _segments = await _project_with_ready_audio(
        db_session, durations_ms=[9_000]
    )
    llm, _mock, _calls = _llm_with(monkeypatch, {0: RuntimeError("boom")})
    enq = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k",
    )

    job = await service.run_generate_storyboard_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "failed"
    assert job.result["ready_segment_count"] == 0


async def test_a_fabricated_number_rejects_the_whole_scene_set_for_that_segment(
    db_session, monkeypatch
):
    _owner, project, script, segments = await _project_with_ready_audio(
        db_session, narrations=["Photosynthesis happens in the leaf."], durations_ms=[9_000]
    )
    llm, _mock, _calls = _llm_with(
        monkeypatch,
        {
            0: [
                ("title_card", {"title": "Fabricated"}),
                ("title_card", {"title": "Takes 42 minutes"}),
            ]
        },
    )
    enq = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k",
    )

    job = await service.run_generate_storyboard_job(db_session, llm, job_id=enq.job.id)

    assert job.result["failed_segment_ids"] == [str(segments[0].id)]
    assert await scenes_repo.list_by_segment_id(db_session, segments[0].id) == []


async def test_an_honest_paraphrase_using_only_narration_words_is_accepted(db_session, monkeypatch):
    _owner, project, script, segments = await _project_with_ready_audio(
        db_session, narrations=["Photosynthesis happens in the leaf."], durations_ms=[9_000]
    )
    llm, _mock, _calls = _llm_with(monkeypatch, {0: _title_scene("Photosynthesis in the leaf")})
    enq = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k",
    )

    job = await service.run_generate_storyboard_job(db_session, llm, job_id=enq.job.id)

    assert job.result["ready_segment_count"] == 1
    assert len(await scenes_repo.list_by_segment_id(db_session, segments[0].id)) == 1


async def test_an_unknown_template_rejects_the_segment_but_not_the_job(db_session, monkeypatch):
    _owner, project, script, segments = await _project_with_ready_audio(
        db_session, durations_ms=[9_000]
    )
    llm, _mock, _calls = _llm_with(monkeypatch, {0: [("section_recap", {"title": "x"})]})
    enq = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k",
    )

    job = await service.run_generate_storyboard_job(db_session, llm, job_id=enq.job.id)

    assert job.result["failed_segment_ids"] == [str(segments[0].id)]


async def test_job_errors_are_sanitized_not_a_raw_exception_message(db_session, monkeypatch):
    _owner, project, script, _segments = await _project_with_ready_audio(
        db_session, durations_ms=[9_000]
    )
    llm, _mock, _calls = _llm_with(
        monkeypatch, {0: RuntimeError("password=hunter2 at /srv/app/worker.py")}
    )
    enq = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k",
    )

    job = await service.run_generate_storyboard_job(db_session, llm, job_id=enq.job.id)

    assert "hunter2" not in (job.error or "")
    assert "worker.py" not in (job.error or "")


# --- resumability / retry -------------------------------------------------------------


async def test_a_retry_only_regenerates_failed_segments_not_ready_ones(db_session, monkeypatch):
    _owner, project, script, segments = await _project_with_ready_audio(
        db_session, durations_ms=[9_000, 6_000]
    )
    llm, _mock, calls = _llm_with(monkeypatch, {0: _title_scene("Good"), 1: RuntimeError("boom")})
    enq = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k1",
    )
    await service.run_generate_storyboard_job(db_session, llm, job_id=enq.job.id)
    calls.clear()

    async def _compose_retry(segment, *, max_scenes: int):
        calls.append(segment.segment_index)
        scenes = (StoryboardSceneDraft(template="title_card", content={"title": "Recovered"}),)
        return StoryboardSceneSetDraft(scenes=scenes), TokenUsage(input_tokens=5, output_tokens=5)

    llm.compose_storyboard_scenes = AsyncMock(side_effect=_compose_retry)  # type: ignore[method-assign]
    retry = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k2",
    )
    job = await service.run_generate_storyboard_job(db_session, llm, job_id=retry.job.id)

    assert calls == [1]  # only the previously-failed segment was retried
    assert job.result["ready_segment_count"] == 2
    assert job.result["outcome"] == "complete"


async def test_a_second_run_is_a_no_op_when_nothing_is_stale(db_session, monkeypatch):
    _owner, project, script, segments = await _project_with_ready_audio(
        db_session, durations_ms=[9_000]
    )
    llm, _mock, calls = _llm_with(monkeypatch, {0: _title_scene()})
    enq = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k1",
    )
    await service.run_generate_storyboard_job(db_session, llm, job_id=enq.job.id)
    calls.clear()

    second = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k2",
    )
    job = await service.run_generate_storyboard_job(db_session, llm, job_id=second.job.id)

    assert calls == []
    assert job.result["ready_segment_count"] == 1


async def test_re_narrating_a_segment_makes_its_scenes_stale_and_they_are_regenerated(
    db_session, monkeypatch
):
    _owner, project, script, segments = await _project_with_ready_audio(
        db_session, durations_ms=[9_000]
    )
    llm, _mock, calls = _llm_with(monkeypatch, {0: _title_scene("Original")})
    enq = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k1",
    )
    await service.run_generate_storyboard_job(db_session, llm, job_id=enq.job.id)
    calls.clear()

    # Re-narrate: a new content_hash for the same segment (Phase 8 re-synthesis).
    await audio_repo.upsert(
        db_session,
        script_segment_id=segments[0].id,
        status="ready",
        storage_bucket=BUCKET,
        storage_key=f"k/{segments[0].id}",
        format="wav",
        sample_rate=22_050,
        channels=1,
        measured_duration_ms=9_000,
        voice_model="en_GB-cori-medium",
        is_temporary_voice_substitute=False,
        content_hash="a-different-hash",
    )
    await scripts_repo.set_measured_duration(
        db_session, project_id=project.id, script_id=script.id, measured_duration_ms=9_000
    )

    status_before = await service.get_storyboard_status(
        db_session, project_id=project.id, owner_id=project.owner_id, script_id=script.id
    )
    assert status_before.segments[0].is_stale is True
    assert status_before.segments[0].is_ready is False
    assert status_before.is_complete is False

    retry = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k2",
    )
    await service.run_generate_storyboard_job(db_session, llm, job_id=retry.job.id)

    assert calls == [0]
    status_after = await service.get_storyboard_status(
        db_session, project_id=project.id, owner_id=project.owner_id, script_id=script.id
    )
    assert status_after.segments[0].is_stale is False
    assert status_after.is_complete is True


# --- read model: positions are computed on read, timings_authoritative (Phase 11) ----------


async def _make_narration_current(db_session, project, segments, durations_ms):
    """Give every segment audio whose fingerprint matches today's text, voice and engine - what a
    finished, up-to-date synthesis leaves behind."""
    voice = script_audio_service.voice_selection_from_settings(get_settings(), project.language)
    for segment, duration in zip(segments, durations_ms, strict=True):
        await audio_repo.upsert(
            db_session,
            script_segment_id=segment.id,
            status="ready",
            storage_bucket=BUCKET,
            storage_key=f"k/{segment.id}",
            format="wav",
            sample_rate=22_050,
            channels=1,
            measured_duration_ms=duration,
            voice_model=voice.voice_model,
            is_temporary_voice_substitute=False,
            content_hash=compute_content_hash(
                narration_text=segment.narration_text,
                voice_model=voice.voice_model,
                engine_version=tts_provider.engine_version(),
                sample_rate=22_050,
            ),
        )


async def _storyboarded_project(db_session, monkeypatch, durations_ms):
    _owner, project, script, segments = await _project_with_ready_audio(
        db_session, durations_ms=durations_ms
    )
    await _make_narration_current(db_session, project, segments, durations_ms)
    llm, _mock, _calls = _llm_with(
        monkeypatch, {i: _title_scene(f"S{i}") for i in range(len(durations_ms))}
    )
    enq = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k1",
    )
    await service.run_generate_storyboard_job(db_session, llm, job_id=enq.job.id)
    return project, script, segments


async def _status(db_session, project, script):
    return await service.get_storyboard_status(
        db_session, project_id=project.id, owner_id=project.owner_id, script_id=script.id
    )


def _scene_starts(status) -> list[list[int]]:
    return [[scene.start_ms for scene in segment.scenes] for segment in status.segments]


async def test_timings_are_authoritative_when_narration_and_scenes_are_current(
    db_session, monkeypatch
):
    project, script, _segments = await _storyboarded_project(
        db_session, monkeypatch, [10_000, 8_000]
    )
    status = await _status(db_session, project, script)
    assert status.timings_authoritative is True
    assert _scene_starts(status) == [[0], [10_000]]


async def test_a_later_segments_start_follows_an_earlier_re_narration_not_the_stored_offset(
    db_session, monkeypatch
):
    """The regression: re-narrating segment 0 (10 s -> 14 s) leaves segment 1's scenes 'fresh',
    so their stored start_ms (10 000) is never rewritten. The read model must show 14 000."""
    project, script, segments = await _storyboarded_project(
        db_session, monkeypatch, [10_000, 8_000]
    )
    stored = await scenes_repo.list_by_segment_id(db_session, segments[1].id)
    assert stored[0].start_ms == 10_000  # the stored value is what goes out of date

    await audio_repo.upsert(
        db_session,
        script_segment_id=segments[0].id,
        status="ready",
        storage_bucket=BUCKET,
        storage_key=f"k/{segments[0].id}",
        format="wav",
        sample_rate=22_050,
        channels=1,
        measured_duration_ms=14_000,
        voice_model="en_GB-cori-medium",
        is_temporary_voice_substitute=False,
        content_hash="re-narrated-in-another-voice",
    )
    status = await _status(db_session, project, script)

    assert _scene_starts(status)[1] == [14_000]
    assert (await scenes_repo.list_by_segment_id(db_session, segments[1].id))[0].start_ms == 10_000
    assert status.timings_authoritative is False  # segment 0 is stale, so the numbers may move


async def test_timings_are_not_authoritative_while_narration_is_being_made(db_session, monkeypatch):
    project, script, _segments = await _storyboarded_project(
        db_session, monkeypatch, [10_000, 8_000]
    )
    await jobs_repo.create(
        db_session, project_id=project.id, kind="synthesise_voice", idempotency_key="synth-1"
    )
    status = await _status(db_session, project, script)
    assert status.timings_authoritative is False
    assert _scene_starts(status) == [[0], [10_000]]  # still shown, still integers


async def test_timings_are_not_authoritative_when_a_segments_narration_failed(
    db_session, monkeypatch
):
    project, script, segments = await _storyboarded_project(
        db_session, monkeypatch, [10_000, 8_000]
    )
    await audio_repo.upsert(
        db_session,
        script_segment_id=segments[0].id,
        status="failed",
        voice_model="en_GB-cori-medium",
        is_temporary_voice_substitute=False,
        content_hash="x",
        error="The voice could not be loaded.",
    )
    status = await _status(db_session, project, script)
    assert status.timings_authoritative is False
    # A failed segment has no measured length: it contributes its last known scene length.
    assert _scene_starts(status)[1] == [10_000]


async def test_a_finished_job_is_not_narration_in_flight(db_session, monkeypatch):
    project, script, _segments = await _storyboarded_project(
        db_session, monkeypatch, [10_000, 8_000]
    )
    job = await jobs_repo.create(
        db_session, project_id=project.id, kind="synthesise_voice", idempotency_key="synth-2"
    )
    await jobs_repo.update(db_session, job_id=job.id, status="succeeded")
    assert (await _status(db_session, project, script)).timings_authoritative is True


# --- procedural visuals: invalid ones are dropped and counted, provenance stays hard ---------

WATER = "Water evaporates from the ocean. Clouds form and rain falls."


def _flow_visual(*labels: str) -> dict:
    return {"type": "process_flow", "nodes": [{"label": text} for text in labels]}


def _diagram(visual: object, *, with_labels: bool = False) -> list[tuple[str, dict]]:
    content: dict = {"illustration": {"description": "The water cycle", "visual": visual}}
    if with_labels:
        content["labels"] = [{"text": "Ocean", "position": "left"}]
    return [("labelled_diagram", content)]


async def _run(db_session, monkeypatch, responses, *, narrations=None, durations=None):
    _owner, project, script, segments = await _project_with_ready_audio(
        db_session, narrations=narrations, durations_ms=durations or [10_000]
    )
    llm, _mock, _calls = _llm_with(monkeypatch, responses)
    enq = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k1",
    )
    job = await service.run_generate_storyboard_job(db_session, llm, job_id=enq.job.id)
    status = await service.get_storyboard_status(
        db_session, project_id=project.id, owner_id=project.owner_id, script_id=script.id
    )
    return job, status, segments


async def test_a_valid_visual_is_stored_intact_and_nothing_is_counted_as_dropped(
    db_session, monkeypatch
):
    visual = _flow_visual("Water evaporates", "Clouds form", "Rain falls")
    job, status, _ = await _run(db_session, monkeypatch, {0: _diagram(visual)}, narrations=[WATER])
    assert job.status == "succeeded"
    [scene] = status.segments[0].scenes
    assert scene.scene.content["illustration"]["visual"] == visual
    assert "visual_dropped" not in scene.scene.content["illustration"]
    assert status.dropped_visual_count == 0
    assert job.result["dropped_visual_count"] == 0


async def test_a_labelled_diagram_with_a_visual_and_no_labels_is_stored(db_session, monkeypatch):
    visual = _flow_visual("Water evaporates", "Rain falls")
    _job, status, _ = await _run(db_session, monkeypatch, {0: _diagram(visual)}, narrations=[WATER])
    assert status.segments[0].is_ready is True
    assert "labels" not in status.segments[0].scenes[0].scene.content


async def test_a_structurally_invalid_visual_is_dropped_the_scene_and_job_survive(
    db_session, monkeypatch
):
    invalid = {"type": "cycle", "nodes": [{"label": "Water"}]}  # a cycle needs three steps
    job, status, _ = await _run(
        db_session, monkeypatch, {0: _diagram(invalid, with_labels=True)}, narrations=[WATER]
    )

    assert job.status == "succeeded"
    assert job.result["outcome"] == "complete"
    assert status.is_complete is True
    [scene] = status.segments[0].scenes
    illustration = scene.scene.content["illustration"]
    assert "visual" not in illustration
    assert illustration["visual_dropped"] is True
    assert illustration["description"] == "The water cycle"  # the rest of the scene is kept
    assert scene.scene.content["labels"] == [{"text": "Ocean", "position": "left"}]


async def test_dropping_a_visual_is_never_silent_it_is_counted_in_the_status_and_the_job(
    db_session, monkeypatch
):
    invalid = {"type": "cycle", "nodes": [{"label": "Water"}]}
    job, status, _ = await _run(
        db_session, monkeypatch, {0: _diagram(invalid, with_labels=True)}, narrations=[WATER]
    )
    assert status.dropped_visual_count == 1
    assert job.result["dropped_visual_count"] == 1


async def test_an_unknown_key_in_a_visual_drops_it_the_model_is_never_trusted_to_be_close(
    db_session, monkeypatch
):
    sneaky = {**_flow_visual("Water evaporates", "Rain falls"), "colour": "red"}
    _job, status, _ = await _run(
        db_session, monkeypatch, {0: _diagram(sneaky, with_labels=True)}, narrations=[WATER]
    )
    assert status.dropped_visual_count == 1
    assert "visual" not in status.segments[0].scenes[0].scene.content["illustration"]


async def test_a_value_chart_is_not_in_the_vocabulary_so_it_is_dropped(db_session, monkeypatch):
    chart = {"type": "value_chart", "nodes": [{"label": "Water"}, {"label": "Rain"}]}
    _job, status, _ = await _run(
        db_session, monkeypatch, {0: _diagram(chart, with_labels=True)}, narrations=[WATER]
    )
    assert status.dropped_visual_count == 1


async def test_a_value_chart_cannot_smuggle_numbers_in_they_are_dropped_with_the_visual(
    db_session, monkeypatch
):
    """Numbers stored as JSON values must appear in the narration exactly like numbers in text -
    so a visual whose values are invented is never drawn: it is dropped, the scene is kept, and
    the invented numbers are nowhere in what is stored."""
    chart = {"type": "value_chart", "values": [73, 42]}
    job, status, _ = await _run(
        db_session, monkeypatch, {0: _diagram(chart, with_labels=True)}, narrations=[WATER]
    )
    assert job.status == "succeeded" and job.result["outcome"] == "complete"
    assert status.dropped_visual_count == 1 and status.is_complete is True
    assert "73" not in str(status.segments[0].scenes[0].scene.content)
    assert job.result["provenance_visual_fallback_count"] == 1


async def test_a_visual_too_big_for_a_split_scene_is_dropped(db_session, monkeypatch):
    big = _flow_visual("Water", "Ocean", "Clouds", "Rain", "Falls")  # 5 steps > 4 in a half
    scene = [
        (
            "split_text_illustration",
            {"text": "Water evaporates.", "illustration": {"description": "d", "visual": big}},
        )
    ]
    _job, status, _ = await _run(db_session, monkeypatch, {0: scene}, narrations=[WATER])
    assert status.dropped_visual_count == 1
    assert status.segments[0].is_ready is True


async def test_dropped_visuals_are_counted_across_scenes_and_segments(db_session, monkeypatch):
    invalid = {"type": "cycle", "nodes": [{"label": "Water"}]}
    responses = {
        0: _diagram(invalid, with_labels=True) * 2,
        1: _diagram(_flow_visual("Water evaporates", "Rain falls")),
    }
    _job, status, _ = await _run(
        db_session,
        monkeypatch,
        responses,
        narrations=[WATER, WATER],
        durations=[10_000, 8_000],
    )
    assert status.dropped_visual_count == 2
    assert status.is_complete is True


async def test_a_visual_dropped_flag_written_by_the_model_is_discarded(db_session, monkeypatch):
    forged = [
        (
            "labelled_diagram",
            {
                "illustration": {"description": "d", "visual_dropped": True},
                "labels": [{"text": "Ocean", "position": "left"}],
            },
        )
    ]
    _job, status, _ = await _run(db_session, monkeypatch, {0: forged}, narrations=[WATER])
    assert status.dropped_visual_count == 0
    assert "visual_dropped" not in status.segments[0].scenes[0].scene.content["illustration"]


async def test_regenerating_a_scene_with_a_valid_visual_clears_the_dropped_count(
    db_session, monkeypatch
):
    invalid = {"type": "cycle", "nodes": [{"label": "Water"}]}
    _job, status, segments = await _run(
        db_session, monkeypatch, {0: _diagram(invalid, with_labels=True)}, narrations=[WATER]
    )
    assert status.dropped_visual_count == 1
    # Re-narrate: the scene set goes stale and is regenerated - this time with a valid visual.
    await audio_repo.upsert(
        db_session,
        script_segment_id=segments[0].id,
        status="ready",
        storage_bucket=BUCKET,
        storage_key=f"k/{segments[0].id}",
        format="wav",
        sample_rate=22_050,
        channels=1,
        measured_duration_ms=10_000,
        voice_model="en_GB-cori-medium",
        is_temporary_voice_substitute=False,
        content_hash="a-new-hash",
    )
    project_id = status.script.project_id
    owner_id = (await projects_repo.get_by_id(db_session, project_id)).owner_id
    llm, _mock, _calls = _llm_with(
        monkeypatch, {0: _diagram(_flow_visual("Water evaporates", "Rain falls"))}
    )
    enq = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project_id,
        owner_id=owner_id,
        script_id=status.script.id,
        idempotency_key="k2",
    )
    await service.run_generate_storyboard_job(db_session, llm, job_id=enq.job.id)
    again = await service.get_storyboard_status(
        db_session, project_id=project_id, owner_id=owner_id, script_id=status.script.id
    )
    assert again.dropped_visual_count == 0


# provenance is never weakened: unsupported text never reaches a stored scene -----------------
# Text in a scene's own slots still fails the segment. Text confined to its illustration no longer
# does (that is where false positives live): the visual is dropped, or the scene becomes a
# text-only slide built from the narration - the storyboard stays complete, no call is repeated,
# and the fallback is recorded and flagged.


async def test_fabricated_text_in_a_valid_visual_drops_the_visual_and_keeps_the_scene(
    db_session, monkeypatch
):
    fabricated = _flow_visual(
        "Water evaporates", "Rain over Delhi"
    )  # Delhi is not in the narration
    job, status, _ = await _run(
        db_session, monkeypatch, {0: _diagram(fabricated)}, narrations=[WATER]
    )
    [scene] = status.segments[0].scenes
    assert job.status == "succeeded" and status.is_complete is True
    assert status.dropped_visual_count == 1
    assert "Delhi" not in str(scene.scene.content)  # the unsupported fact is nowhere on the scene
    assert scene.scene.content["illustration"]["visual_dropped"] is True
    assert job.result["provenance_visual_fallback_count"] == 1
    assert job.result["provenance_text_fallback_count"] == 0
    assert job.result["provider_calls"] == 1  # the fallback needed no second call


async def test_fabrication_in_an_invalid_visual_is_dropped_and_recorded_as_provenance(
    db_session, monkeypatch
):
    """The visual is structurally invalid (one step) AND fabricates a number: either way it is
    dropped, and the fabricated number is recorded as a provenance fallback, not merely as an
    invalid visual."""
    both = {"type": "cycle", "nodes": [{"label": "About 99 percent of rain"}]}
    job, status, _ = await _run(
        db_session, monkeypatch, {0: _diagram(both, with_labels=True)}, narrations=[WATER]
    )
    assert job.status == "succeeded"
    assert "99" not in str(status.segments[0].scenes[0].scene.content)
    assert status.dropped_visual_count == 1
    assert job.result["provenance_visual_fallback_count"] == 1


async def test_a_fabricated_percentage_in_visual_text_no_longer_fails_the_segment(
    db_session, monkeypatch
):
    responses = {
        0: _diagram(_flow_visual("Water evaporates", "40% of rain falls")),
        1: _diagram(_flow_visual("Water evaporates", "Rain falls")),
    }
    job, status, _ = await _run(
        db_session,
        monkeypatch,
        responses,
        narrations=[WATER, WATER],
        durations=[10_000, 8_000],
    )
    assert job.status == "succeeded" and job.result["outcome"] == "complete"
    assert status.segments[0].is_ready is True and status.segments[1].is_ready is True
    assert status.dropped_visual_count == 1  # only the first segment's visual was unsupported
    assert "40" not in str(status.segments[0].scenes[0].scene.content)


async def test_fabricated_text_in_the_illustrations_description_becomes_a_text_only_scene(
    db_session, monkeypatch
):
    scene = [
        (
            "split_text_illustration",
            {
                "text": "Water evaporates.",
                "illustration": {"description": "A market in Delhi at dawn"},
            },
        )
    ]
    job, status, _ = await _run(db_session, monkeypatch, {0: scene}, narrations=[WATER])

    [detail] = status.segments[0].scenes
    content = detail.scene.content
    assert job.status == "succeeded" and status.is_complete is True
    assert detail.scene.template == "bullet_points"
    assert content["provenance_fallback"] is True
    assert "Delhi" not in str(content)
    assert all(bullet in WATER for bullet in content["bullets"])  # only the narration's own words
    assert detail.reviewer_flag is True  # a reviewer is always shown a replaced scene
    assert job.result["provenance_text_fallback_count"] == 1
    assert job.result["provider_calls"] == 1


async def test_fabricated_text_in_a_scenes_own_labels_still_fails_the_segment(
    db_session, monkeypatch
):
    scene = [
        (
            "labelled_diagram",
            {
                "illustration": {"description": "d"},
                "labels": [{"text": "Rain in 1998", "position": "left"}],
            },
        )
    ]
    job, status, _ = await _run(db_session, monkeypatch, {0: scene}, narrations=[WATER])

    assert job.status == "failed"  # the only segment failed: the label is the scene's own text
    assert status.segments[0].scenes == ()
    assert status.dropped_visual_count == 0
    assert job.result["provenance_text_fallback_count"] == 0


async def test_a_fabricated_number_stored_as_a_json_number_fails_the_segment(
    db_session, monkeypatch
):
    scene = [("title_card", {"title": "Water", "stat": 73})]  # 73 is not in the narration
    job, _status, _ = await _run(db_session, monkeypatch, {0: scene}, narrations=[WATER])
    assert job.status == "failed"


async def test_a_json_number_the_narration_has_is_accepted(db_session, monkeypatch):
    scene = [("title_card", {"title": "Water", "stat": 3})]
    job, _status, _ = await _run(
        db_session,
        monkeypatch,
        {0: scene},
        narrations=["Water has 3 states. Ice, water and steam."],
    )
    assert job.status == "succeeded"


# --- read model: completeness, reviewer flags ------------------------------------------


async def test_status_is_incomplete_while_a_segment_has_not_been_generated(db_session):
    _owner, project, script, _segments = await _project_with_ready_audio(db_session)

    status = await service.get_storyboard_status(
        db_session, project_id=project.id, owner_id=project.owner_id, script_id=script.id
    )

    assert status.is_complete is False
    assert status.ready_segment_count == 0
    assert len(status.not_ready_segment_ids) == 2


async def test_status_is_complete_only_once_every_segment_is_ready(db_session, monkeypatch):
    _owner, project, script, segments = await _project_with_ready_audio(
        db_session, durations_ms=[9_000, 6_000]
    )
    llm, _mock, _calls = _llm_with(monkeypatch, {0: _title_scene("A"), 1: _title_scene("B")})
    enq = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k",
    )
    await service.run_generate_storyboard_job(db_session, llm, job_id=enq.job.id)

    status = await service.get_storyboard_status(
        db_session, project_id=project.id, owner_id=project.owner_id, script_id=script.id
    )

    assert status.is_complete is True
    assert status.ready_segment_count == 2
    assert status.not_ready_segment_ids == ()
    assert status.actual_provider_calls == 2


async def test_a_scene_with_no_lexical_overlap_is_flagged_but_the_job_still_succeeds(
    db_session, monkeypatch
):
    _owner, project, script, segments = await _project_with_ready_audio(
        db_session, narrations=["Photosynthesis happens in the leaf."], durations_ms=[9_000]
    )
    llm, _mock, _calls = _llm_with(
        monkeypatch, {0: _title_scene("Volcanic eruptions reshape coastlines")}
    )
    enq = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k",
    )

    job = await service.run_generate_storyboard_job(db_session, llm, job_id=enq.job.id)
    assert job.status == "succeeded"

    status = await service.get_storyboard_status(
        db_session, project_id=project.id, owner_id=project.owner_id, script_id=script.id
    )
    [segment_detail] = status.segments
    assert segment_detail.is_ready is True  # a warning, never a rejection
    [scene_detail] = segment_detail.scenes
    assert scene_detail.reviewer_flag is True


# --- progress checkpoints -------------------------------------------------------------


async def test_progress_is_reported_through_independent_sessions(db_session, monkeypatch):
    _owner, project, script, segments = await _project_with_ready_audio(
        db_session, durations_ms=[9_000, 6_000]
    )
    llm, _mock, _calls = _llm_with(monkeypatch, {0: _title_scene("A"), 1: _title_scene("B")})
    enq = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k",
    )
    sessionmaker: async_sessionmaker = async_sessionmaker(
        bind=db_session.bind, join_transaction_mode="create_savepoint", expire_on_commit=False
    )

    job = await service.run_generate_storyboard_job(
        db_session, llm, job_id=enq.job.id, sessionmaker=sessionmaker
    )

    assert job.status == "succeeded"
    assert job.payload["progress"]["stage"] == "finalising"


# --- token usage --------------------------------------------------------------------------


async def test_the_job_records_one_storyboard_call_per_segment_it_composed(db_session, monkeypatch):
    _owner, project, script, _segments = await _project_with_ready_audio(
        db_session, durations_ms=[9_000, 6_000]
    )
    llm, _mock, _calls = _llm_with(monkeypatch, {0: _title_scene("A"), 1: _title_scene("B")})
    enq = await service.enqueue_storyboard_generation(
        db_session,
        project_id=project.id,
        owner_id=project.owner_id,
        script_id=script.id,
        idempotency_key="k",
    )

    job = await service.run_generate_storyboard_job(db_session, llm, job_id=enq.job.id)

    usage = job.result["usage"]
    assert sorted(usage["by_call"]) == ["storyboard"]
    assert usage["by_call"]["storyboard"]["calls"] == 2
    assert usage["total"]["input_tokens"] == 10 and usage["total"]["output_tokens"] == 10
    assert job.result["provider_calls"] == 2
