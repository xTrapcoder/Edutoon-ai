"""The evidence-per-minute guard: a target longer than the verified claims can honestly fill is
refused before any paid call, instead of being padded with repeated or invented facts.

The heuristic (``domain/evidence_sufficiency.py``) is deliberately conservative: a supported claim
carries at most 30 seconds of narration, each topic earns 10 seconds of connective narration and the
lesson 15 for an opening and a close. The table below is the specification in numbers - if a
constant is re-tuned, these are the cases whose answer changes, on purpose.
"""

from __future__ import annotations

import pytest

from edutoon.core.config import get_settings
from edutoon.core.errors import ValidationError
from edutoon.domain import evidence_sufficiency as es
from edutoon.repositories import jobs as jobs_repo
from edutoon.services import scripts as scripts_service
from tests.test_services_scripts import (  # noqa: F401 - shared fixtures/helpers
    _no_duration_correction,
    _project_with_topics,
)

MINUTE = 60_000
CAP = 15  # MAX_CLAIMS_PER_SECTION's default: the most claims one topic can use


def assess(counts: list[int], minutes: float) -> es.EvidenceAssessment:
    return es.assess_evidence(
        counts, target_duration_ms=int(minutes * MINUTE), max_claims_per_topic=CAP
    )


# --- the arithmetic -----------------------------------------------------------------------------


def test_the_constants_are_the_documented_conservative_ones() -> None:
    assert es.DEFAULT_MS_PER_CLAIM == 30_000
    assert es.CONNECTIVE_MS_PER_TOPIC == 10_000
    assert es.LESSON_FRAME_MS == 15_000


@pytest.mark.parametrize(
    ("counts", "seconds"),
    [
        ([], 0),  # no topics: nothing to say at all
        ([1], 55),  # 30 + 10 + 15
        ([2], 85),
        ([3], 115),
        ([12], 385),
        ([15], 475),
        ([40], 475),  # claims beyond what one topic can use add nothing
        ([2, 2, 2], 225),  # 3 x (60 + 10) + 15
        ([10, 10, 10, 10], 1255),  # 4 x (300 + 10) + 15
        ([1] * 10, 415),  # 10 x (30 + 10) + 15
        ([15] * 20, 9215),  # 20 x (450 + 10) + 15
    ],
)
def test_the_defensible_duration(counts: list[int], seconds: int) -> None:
    assert es.defensible_duration_ms(counts, max_claims_per_topic=CAP) == seconds * 1000


# The specification as a table: (evidence, target in minutes, is it enough?).
CASES = [
    # one supported claim cannot carry any length of video
    pytest.param([1], 1, False, id="one-claim-1min"),
    pytest.param([1], 5, False, id="one-claim-5min"),
    pytest.param([1], 10, False, id="one-claim-10min"),
    pytest.param([1], 30, False, id="one-claim-30min"),
    pytest.param([1], 60, False, id="one-claim-60min"),
    # sparse: a few claims on one topic
    pytest.param([3], 1, True, id="one-topic-3-claims-1min"),
    pytest.param([3], 5, False, id="one-topic-3-claims-5min"),
    # sparse across topics: 6 claims over 3 topics fills under 4 minutes
    pytest.param([2, 2, 2], 3, True, id="three-topics-6-claims-3min"),
    pytest.param([2, 2, 2], 5, False, id="three-topics-6-claims-5min"),
    pytest.param([2, 2, 2], 10, False, id="three-topics-6-claims-10min"),
    # many claims, but all on one topic: a topic only uses 15 of them
    pytest.param([40], 5, True, id="one-topic-40-claims-5min"),
    pytest.param([40], 10, False, id="one-topic-40-claims-10min"),
    pytest.param([40], 30, False, id="one-topic-40-claims-30min"),
    # many topics, one claim each: 10 short segments
    pytest.param([1] * 10, 5, True, id="ten-topics-1-claim-5min"),
    pytest.param([1] * 10, 10, False, id="ten-topics-1-claim-10min"),
    # healthy: four well-covered topics
    pytest.param([10] * 4, 5, True, id="four-topics-10-claims-5min"),
    pytest.param([10] * 4, 10, True, id="four-topics-10-claims-10min"),
    pytest.param([10] * 4, 30, False, id="four-topics-10-claims-30min"),
    pytest.param([10] * 4, 60, False, id="four-topics-10-claims-60min"),
    # rich: the most a generation can use (20 topics of 15 claims)
    pytest.param([15] * 20, 5, True, id="rich-5min"),
    pytest.param([15] * 20, 10, True, id="rich-10min"),
    pytest.param([15] * 20, 30, True, id="rich-30min"),
    pytest.param([15] * 20, 60, True, id="rich-60min"),
]


@pytest.mark.parametrize(("counts", "minutes", "enough"), CASES)
def test_the_specification_table(counts: list[int], minutes: int, enough: bool) -> None:
    assert assess(counts, minutes).is_sufficient is enough


def test_a_healthy_short_lesson_is_not_rejected() -> None:
    """The guard errs towards refusing, but a few pages of source material must pass: a
    five-minute lesson needs about a dozen supported claims over a handful of topics."""
    assert assess([4, 4, 4], 5).is_sufficient  # 12 claims, 3 topics
    assert assess([3, 3, 3, 3], 5).is_sufficient  # the same dozen over 4 topics
    assert assess([2, 2], 2).is_sufficient


def test_the_defensible_duration_itself_is_enough_and_one_millisecond_over_is_not() -> None:
    at = es.defensible_duration_ms([10], max_claims_per_topic=CAP)
    ok = es.assess_evidence([10], target_duration_ms=at, max_claims_per_topic=CAP)
    over = es.assess_evidence([10], target_duration_ms=at + 1, max_claims_per_topic=CAP)
    assert ok.is_sufficient and ok.shortfall_ms == 0
    assert not over.is_sufficient and over.shortfall_ms == 1


def test_the_assessment_reports_what_it_counted() -> None:
    result = assess([20, 5], 15)  # 25 supported claims, but the first topic uses only 15
    assert (result.topic_count, result.supported_claims, result.usable_claims) == (2, 25, 20)
    assert result.shortfall_ms == 15 * MINUTE - result.defensible_duration_ms > 0


def test_it_is_deterministic() -> None:
    assert assess([3, 7, 1], 6) == assess([3, 7, 1], 6)


def test_more_evidence_never_lowers_what_can_be_defended() -> None:
    base = es.defensible_duration_ms([3, 3], max_claims_per_topic=CAP)
    assert es.defensible_duration_ms([4, 3], max_claims_per_topic=CAP) > base
    assert es.defensible_duration_ms([3, 3, 1], max_claims_per_topic=CAP) > base
    # Order does not matter: the topics are counted, not sequenced.
    assert es.defensible_duration_ms([3, 7], max_claims_per_topic=CAP) == es.defensible_duration_ms(
        [7, 3], max_claims_per_topic=CAP
    )


def test_the_per_claim_allowance_is_the_knob() -> None:
    generous = es.defensible_duration_ms([2], max_claims_per_topic=CAP, ms_per_claim=60_000)
    assert generous == 2 * 60_000 + es.CONNECTIVE_MS_PER_TOPIC + es.LESSON_FRAME_MS


@pytest.mark.parametrize(
    "kwargs",
    [
        {"claim_counts": [-1], "max_claims_per_topic": 15},
        {"claim_counts": [1], "max_claims_per_topic": 0},
        {"claim_counts": [1], "max_claims_per_topic": 15, "ms_per_claim": 0},
    ],
)
def test_nonsense_input_is_refused(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        es.defensible_duration_ms(**kwargs)


@pytest.mark.parametrize(
    ("ms", "text"),
    [
        (0, "0:00"),
        (55_000, "0:55"),
        (225_000, "3:45"),
        (300_000, "5:00"),
        (599_999, "9:59"),
        (3_600_000, "1:00:00"),
        (3_725_000, "1:02:05"),
        (-5, "0:00"),
    ],
)
def test_durations_are_written_as_clock_times(ms: int, text: str) -> None:
    assert es.format_clock(ms) == text


# --- the service refuses before anything exists ---------------------------------------------------


@pytest.fixture
def real_default(monkeypatch):
    """The suite relaxes the guard for unrelated tests (conftest); these use the real 30 s."""
    monkeypatch.setenv("EVIDENCE_SECONDS_PER_CLAIM", "30")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _enqueue(db_session, project, owner, key: str = "k"):
    return await scripts_service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key=key
    )


async def test_one_claim_and_a_five_minute_target_is_refused_before_any_job_exists(
    db_session, real_default
):
    owner, project, _topics, _claims = await _project_with_topics(
        db_session, [("Only topic", ["The single supported fact."])]
    )

    with pytest.raises(ValidationError) as caught:
        await _enqueue(db_session, project, owner)

    message = caught.value.message
    assert "not enough verified evidence for a video of 5:00" in message
    assert "1 supported claim across 1 accepted topic" in message
    assert "about 0:55 of narration" in message
    assert "inventing facts" in message and "choose a target of 0:55 or shorter" in message
    # Nothing was created: no job, no script, no provider call.
    assert (await jobs_repo.list_by_project(db_session, project.id)).items == []


async def test_a_sixty_minute_target_over_thin_evidence_is_refused(
    db_session, real_default, monkeypatch
):
    # Lift the render ceiling (a separate guard) to reach the evidence guard: this is what the
    # guard will say the day a 60-minute video can be made.
    monkeypatch.setenv("MAX_RENDER_DURATION_MS", "3600000")
    get_settings.cache_clear()
    owner, project, _topics, _claims = await _project_with_topics(
        db_session,
        [("A", ["a1", "a2"]), ("B", ["b1", "b2"]), ("C", ["c1", "c2"])],
        target_duration_ms=60 * MINUTE,
    )

    with pytest.raises(ValidationError, match="video of 1:00:00"):
        await _enqueue(db_session, project, owner)


async def test_enough_evidence_is_accepted_and_queues_the_job_as_before(db_session, real_default):
    owner, project, _topics, _claims = await _project_with_topics(
        db_session,
        [("A", [f"a{i}" for i in range(4)]), ("B", [f"b{i}" for i in range(4)])],
        target_duration_ms=5 * MINUTE,
    )  # 8 claims over 2 topics: 8 x 30 + 2 x 10 + 15 = 275 s ... a 4:30 target fits

    project = await _retarget(db_session, project, 270_000)
    result = await _enqueue(db_session, project, owner)

    assert result.job.status == "queued" and result.replayed is False


async def _retarget(db_session, project, target_ms: int):
    from edutoon.repositories import projects as projects_repo

    updated = await projects_repo.update(
        db_session, project_id=project.id, owner_id=project.owner_id, target_duration_ms=target_ms
    )
    assert updated is not None
    return updated


async def test_the_message_says_when_a_topics_extra_claims_could_not_be_used(
    db_session, real_default
):
    owner, project, _topics, _claims = await _project_with_topics(
        db_session,
        [("Big topic", [f"fact {i}" for i in range(20)])],
        target_duration_ms=10 * MINUTE,
    )  # 20 claims but one topic uses 15: 15 x 30 + 10 + 15 = 475 s

    with pytest.raises(ValidationError) as caught:
        await _enqueue(db_session, project, owner)

    assert "5 more than the 15 a single topic can use" in caught.value.message
    assert "about 7:55 of narration" in caught.value.message


async def test_a_replay_is_answered_before_the_guard_is_consulted(db_session, real_default):
    owner, project, _topics, _claims = await _project_with_topics(
        db_session,
        [("A", [f"a{i}" for i in range(4)]), ("B", [f"b{i}" for i in range(4)])],
    )
    project = await _retarget(db_session, project, 270_000)
    first = await _enqueue(db_session, project, owner, key="same")
    await _retarget(db_session, project, 60 * MINUTE)  # now far too long for the evidence

    replay = await _enqueue(db_session, project, owner, key="same")

    assert replay.replayed is True and replay.job.id == first.job.id


async def test_the_guard_does_not_change_the_no_evidence_message(db_session, real_default):
    from edutoon.repositories import project_topics as project_topics_repo
    from tests.test_services_scripts import _make_project

    owner, project = await _make_project(db_session)
    await project_topics_repo.create(
        db_session, project_id=project.id, title="Empty", status="accepted", position=0
    )

    with pytest.raises(ValidationError, match="no verified evidence yet") as refusal:
        await _enqueue(db_session, project, owner)

    # Regression: the web client does not show a sentence over 280 characters, and this one grew
    # past it, so the page lost the reason. The refusal now carries it as data.
    assert refusal.value.details == {"reason": "no_verified_evidence", "topics": ["Empty"]}


# --- the longest supported length ---------------------------------------------------------------


async def test_a_target_longer_than_the_longest_renderable_video_is_refused_before_anything_else(
    db_session, real_default
):
    """The first paid step is where an unsupported length stops: a script that could not be
    rendered would be written, narrated and storyboarded for nothing. The model still holds 1-60
    minutes; the ceiling is the render limit, the one number to raise when long video is done."""
    owner, project, _topics, _claims = await _project_with_topics(
        db_session,
        [("A", [f"a{i}" for i in range(15)])] * 1,
        target_duration_ms=31 * MINUTE,
    )

    with pytest.raises(ValidationError) as caught:
        await _enqueue(db_session, project, owner)

    assert "target of 31:00 is longer than the longest video that can be made today (30:00)" in (
        caught.value.message
    )
    assert "Shorten the target" in caught.value.message
    assert (await jobs_repo.list_by_project(db_session, project.id)).items == []


async def test_a_target_exactly_at_the_ceiling_is_not_refused_for_its_length(
    db_session, real_default
):
    owner, project, _topics, _claims = await _project_with_topics(
        db_session,
        [(f"T{t}", [f"t{t}c{i}" for i in range(15)]) for t in range(4)],
        target_duration_ms=30 * MINUTE,
    )  # 4 topics x 15 claims: 4 x (450 + 10) + 15 = 1855 s, enough for 30:00

    result = await _enqueue(db_session, project, owner)

    assert result.job.status == "queued"


async def test_the_ceiling_is_the_render_limit_so_raising_it_lifts_it(
    db_session, real_default, monkeypatch
):
    owner, project, _topics, _claims = await _project_with_topics(
        db_session,
        [(f"T{t}", [f"t{t}c{i}" for i in range(15)]) for t in range(3)],
        target_duration_ms=20 * MINUTE,
    )  # 3 x (450 + 10) + 15 = 1395 s: enough evidence for 20:00
    monkeypatch.setenv("MAX_RENDER_DURATION_MS", str(10 * MINUTE))
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="longer than the longest video"):
        await _enqueue(db_session, project, owner, key="before")

    monkeypatch.setenv("MAX_RENDER_DURATION_MS", str(30 * MINUTE))
    get_settings.cache_clear()
    result = await _enqueue(db_session, project, owner, key="after")

    assert result.job.status == "queued"
