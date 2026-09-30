"""The in-process spend guard: a per-job tripwire on provider calls and a per-project hourly limit
on the entry points that spend (``services/spend_guard.py``, ``services/llm_usage.py``).

The guard is deliberately coarse. What is held here: it trips exactly at its limit and no earlier;
the call that would exceed the limit is never made; a job that trips says why in words written for
its owner; only paid work counts; projects do not count each other's; and a request that was
already answered (an idempotent replay) is never counted again.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from anthropic import AsyncAnthropic

from edutoon.core.config import get_settings
from edutoon.core.errors import TooManyRequestsError
from edutoon.domain.llm_usage import ProviderCallLimitError, UsageReport
from edutoon.providers.llm import LLM, TokenUsage
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import users as users_repo
from edutoon.services import llm_usage as llm_usage_service
from edutoon.services import scripts as scripts_service
from edutoon.services import spend_guard
from tests.test_services_scripts import (  # noqa: F401 - fixtures/helpers shared with that file
    _enqueued,
    _no_duration_correction,
    _on_target,
    _simple_llm,
)

pytestmark = pytest.mark.provider_mocked


@pytest.fixture
def settings_with(monkeypatch):
    """Override settings for one test (and put the cache back afterwards)."""

    def _apply(**values: Any) -> None:
        for name, value in values.items():
            monkeypatch.setenv(name, str(value))
        get_settings.cache_clear()

    yield _apply
    get_settings.cache_clear()


# --- the per-job tripwire ----------------------------------------------------------------------


class Usage:
    input_tokens = 1
    output_tokens = 1
    cache_creation_input_tokens = None
    cache_read_input_tokens = None


def test_a_report_with_no_limit_never_trips() -> None:
    report = UsageReport()
    for _ in range(1000):
        report.add("script_section", Usage())
    report.ensure_capacity()


def test_a_report_trips_exactly_when_the_limit_is_reached_not_before() -> None:
    report = UsageReport(max_calls=3)
    for _ in range(3):
        report.ensure_capacity()  # room for a call: 0, 1, 2 made so far
        report.add("script_section", Usage())
    with pytest.raises(ProviderCallLimitError) as caught:
        report.ensure_capacity()  # three made: the fourth is not allowed
    assert caught.value.limit == 3
    assert "3 AI calls" in str(caught.value)


def test_the_limit_counts_every_kind_of_call_together() -> None:
    report = UsageReport(max_calls=2)
    report.add("script_outline", Usage())
    report.add("script_section", Usage())
    with pytest.raises(ProviderCallLimitError):
        report.ensure_capacity()


async def test_a_call_over_the_limit_is_never_made_and_leaves_no_unawaited_coroutine() -> None:
    made: list[int] = []

    async def paid_call() -> tuple[str, TokenUsage]:
        made.append(1)
        return "answer", TokenUsage(1, 1)

    report = UsageReport(max_calls=1)
    assert await llm_usage_service.record_call(report, "storyboard", paid_call()) == "answer"

    coroutine = paid_call()
    with pytest.raises(ProviderCallLimitError):
        await llm_usage_service.record_call(report, "storyboard", coroutine)

    assert made == [1]  # the second call was never awaited
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED


def test_every_job_is_armed_from_the_setting(settings_with) -> None:
    settings_with(MAX_PROVIDER_CALLS_PER_JOB=7)
    assert llm_usage_service.new_usage_report().max_calls == 7


async def test_a_script_generation_that_reaches_the_limit_stops_before_the_next_call(
    db_session, monkeypatch, settings_with
) -> None:
    settings_with(MAX_PROVIDER_CALLS_PER_JOB=1)  # the outline is the only call it may make
    _owner, _project, topic, claim, enq = await _enqueued(db_session)
    llm, outline_mock, section_mock = _simple_llm(
        monkeypatch, segments_by_topic={topic.id: [_on_target([str(claim.id)])]}
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "failed"
    assert "1 AI calls" in job.error and "asked for nothing more" in job.error
    outline_mock.assert_awaited_once()
    section_mock.assert_not_awaited()  # the call that would have exceeded the limit was not made
    assert job.result["usage"]["total"]["calls"] == 1  # and the one that was made is recorded


async def test_a_script_generation_within_the_limit_is_untouched(
    db_session, monkeypatch, settings_with
) -> None:
    settings_with(MAX_PROVIDER_CALLS_PER_JOB=2)  # outline + one section: exactly enough
    _owner, _project, topic, claim, enq = await _enqueued(db_session)
    llm, _outline, section_mock = _simple_llm(
        monkeypatch, segments_by_topic={topic.id: [_on_target([str(claim.id)])]}
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded"
    section_mock.assert_awaited_once()


def test_no_bounded_job_comes_near_the_default_limit() -> None:
    """The tripwire must never fire on a legitimate job: the largest bound this codebase proves,
    a full-length script, is a fraction of it."""
    from edutoon.domain.generation_settings import MAX_TARGET_DURATION_MS
    from edutoon.domain.script_duration import max_provider_calls

    settings = get_settings()
    script_worst_case = max_provider_calls(
        max_target_duration_ms=MAX_TARGET_DURATION_MS,
        max_section_duration_ms=settings.MAX_SECTION_DURATION_MS,
        max_script_sections=settings.MAX_SCRIPT_SECTIONS,
    )
    claims_worst_case = settings.MAX_TOPICS_PER_EXTRACTION * (1 + settings.MAX_CLAIMS_PER_TOPIC)
    assert 5 * script_worst_case <= settings.MAX_PROVIDER_CALLS_PER_JOB
    assert 2 * claims_worst_case <= settings.MAX_PROVIDER_CALLS_PER_JOB
    assert settings.MAX_PROVIDER_CALLS_PER_JOB >= settings.MAX_STORYBOARD_SEGMENTS


# --- the hourly limit --------------------------------------------------------------------------


async def _project(db_session):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    return await projects_repo.create(db_session, owner_id=owner.id, title="P", source_type="pdf")


async def _job(db_session, project, kind: str, **fields: Any):
    job = await jobs_repo.create(db_session, project_id=project.id, kind=kind)
    if fields:
        updated = await jobs_repo.update(db_session, job_id=job.id, **fields)
        assert updated is not None
        return updated
    return job


async def test_a_project_may_start_paid_jobs_up_to_the_limit_and_no_more(
    db_session, settings_with
) -> None:
    settings_with(MAX_PROVIDER_JOBS_PER_PROJECT_PER_HOUR=3)
    project = await _project(db_session)
    for kind in ("extract_topics", "extract_claims", "generate_script"):
        await spend_guard.ensure_can_start_provider_job(db_session, project_id=project.id)
        await _job(db_session, project, kind)

    with pytest.raises(TooManyRequestsError, match="already started 3 AI jobs"):
        await spend_guard.ensure_can_start_provider_job(db_session, project_id=project.id)


async def test_a_job_that_failed_still_counts_because_it_was_paid_for(
    db_session, settings_with
) -> None:
    settings_with(MAX_PROVIDER_JOBS_PER_PROJECT_PER_HOUR=1)
    project = await _project(db_session)
    job = await _job(db_session, project, "extract_topics")
    await jobs_repo.update(
        db_session, job_id=job.id, status="running", started_at=datetime.now(UTC)
    )
    await jobs_repo.update(
        db_session, job_id=job.id, status="failed", finished_at=datetime.now(UTC), error="x"
    )

    with pytest.raises(TooManyRequestsError):
        await spend_guard.ensure_can_start_provider_job(db_session, project_id=project.id)


async def test_only_paid_jobs_count_rendering_and_narration_do_not(
    db_session, settings_with
) -> None:
    settings_with(MAX_PROVIDER_JOBS_PER_PROJECT_PER_HOUR=1)
    project = await _project(db_session)
    for kind in ("render_video", "synthesise_voice", "render_video"):
        await _job(db_session, project, kind)

    await spend_guard.ensure_can_start_provider_job(db_session, project_id=project.id)


async def test_one_projects_jobs_never_count_against_another(db_session, settings_with) -> None:
    settings_with(MAX_PROVIDER_JOBS_PER_PROJECT_PER_HOUR=1)
    busy, quiet = await _project(db_session), await _project(db_session)
    await _job(db_session, busy, "extract_topics")

    with pytest.raises(TooManyRequestsError):
        await spend_guard.ensure_can_start_provider_job(db_session, project_id=busy.id)
    await spend_guard.ensure_can_start_provider_job(db_session, project_id=quiet.id)


async def test_a_job_older_than_the_window_no_longer_counts(db_session, settings_with) -> None:
    settings_with(MAX_PROVIDER_JOBS_PER_PROJECT_PER_HOUR=1)
    project = await _project(db_session)
    await _job(
        db_session,
        project,
        "extract_topics",
        created_at=datetime.now(UTC) - spend_guard.WINDOW - timedelta(minutes=1),
    )

    await spend_guard.ensure_can_start_provider_job(db_session, project_id=project.id)


async def test_the_enqueue_functions_refuse_before_creating_anything(
    db_session, settings_with
) -> None:
    settings_with(MAX_PROVIDER_JOBS_PER_PROJECT_PER_HOUR=1)
    _owner, project, _topic, _claim, first = await _enqueued(db_session, key="first")
    # A second, different request (a fresh Idempotency-Key) is a new start and is refused ...
    with pytest.raises(TooManyRequestsError):
        await scripts_service.enqueue_script_generation(
            db_session, project_id=project.id, owner_id=project.owner_id, idempotency_key="second"
        )
    assert [j.id for j in (await jobs_repo.list_by_project(db_session, project.id)).items] == [
        first.job.id
    ]
    # ... but a replay of the first is an answer, not a start, and is never counted.
    replay = await scripts_service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=project.owner_id, idempotency_key="first"
    )
    assert replay.replayed is True and replay.job.id == first.job.id


# --- the manual claim path ----------------------------------------------------------------------


async def test_claims_typed_in_by_hand_are_limited_but_a_jobs_claims_are_not(
    db_session, settings_with
) -> None:
    from edutoon.repositories import claim_verifications as claim_verifications_repo

    settings_with(MAX_MANUAL_CLAIM_VERIFICATIONS_PER_PROJECT_PER_HOUR=2)
    project = await _project(db_session)
    topic_id = None
    from edutoon.repositories import project_topics as project_topics_repo

    topic = await project_topics_repo.create(db_session, project_id=project.id, title="T")
    topic_id = topic.id
    for text in ("one", "two"):
        await claim_verifications_repo.create(
            db_session,
            project_id=project.id,
            claim_text=text,
            status="unsupported",
            embedding_model="m",
            llm_model="l",
        )
    # Claims a job extracted carry a topic and are counted by that job, not here.
    for text in ("job one", "job two", "job three"):
        await claim_verifications_repo.create(
            db_session,
            project_id=project.id,
            claim_text=text,
            status="unsupported",
            embedding_model="m",
            llm_model="l",
            topic_id=topic_id,
        )

    with pytest.raises(TooManyRequestsError, match="already had 2 claims verified by hand"):
        await spend_guard.ensure_can_verify_manual_claim(db_session, project_id=project.id)


async def test_the_manual_route_is_behind_the_guard_and_the_llm_is_not_reached(
    db_session, settings_with, monkeypatch
) -> None:
    from openai import AsyncOpenAI

    from edutoon.providers.embeddings import Embeddings
    from edutoon.services import evidence as evidence_service

    settings_with(MAX_MANUAL_CLAIM_VERIFICATIONS_PER_PROJECT_PER_HOUR=1)
    project = await _project(db_session)
    embeddings = Embeddings(AsyncOpenAI(api_key="sk-test-not-a-real-key"), model="m")
    embed = AsyncMock(return_value=[[0.0] * 1536])
    monkeypatch.setattr(embeddings, "embed", embed)
    llm = LLM(AsyncAnthropic(api_key="sk-ant-test-not-a-real-key"), model="claude-sonnet-5")
    judge = AsyncMock()
    monkeypatch.setattr(llm, "judge_claim", judge)

    await evidence_service.verify_manual_claim(
        db_session, embeddings, llm, project_id=project.id, claim_text="first"
    )  # nothing indexed: an "unsupported" verdict without a model call, but it is recorded
    with pytest.raises(TooManyRequestsError):
        await evidence_service.verify_manual_claim(
            db_session, embeddings, llm, project_id=project.id, claim_text="second"
        )

    assert embed.await_count == 1  # the refused request cost nothing
    judge.assert_not_awaited()
