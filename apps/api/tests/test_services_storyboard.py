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
from edutoon.domain.storyboard_timing import (
    segment_absolute_starts,
)
from edutoon.providers.llm import LLM, StoryboardSceneDraft, StoryboardSceneSetDraft, TokenUsage
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import script_segment_audio as audio_repo
from edutoon.repositories import script_segment_scenes as scenes_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.scripts import NewScriptSegment
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
