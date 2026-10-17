"""The sentence-level support check in script generation (``services.narration_support``).

The regression that started it: a photosynthesis script said "Plants need water to open their
stomata" while its only cited claim said a plant short of water *closes* them. Every word of the
sentence is in the claim, so the numbers-and-names check passed it. The judge here is faked (these
tests are about what the service does with a verdict, like the rest of ``test_services_scripts``);
``scripts/check_support_judge.py`` measures the real model.
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest

from edutoon.core.config import get_settings
from edutoon.domain import narration_support as ns
from edutoon.providers.llm import LLM, LLMResponseError, TokenUsage
from edutoon.repositories import scripts as scripts_repo
from edutoon.services import scripts as scripts_service
from tests.test_services_scripts import _enqueued, _mock_llm, _segment

pytestmark = pytest.mark.provider_mocked

CLAIM = "Water availability limits photosynthesis by causing plants to close their stomata."
GOOD = "A plant that is short of water closes its stomata."
BAD = "Plants need water to open their stomata."
GOOD_TWO = "Closed stomata let in less carbon dioxide."


@pytest.fixture(autouse=True)
def _support_check_on(monkeypatch):
    monkeypatch.setenv("SCRIPT_SUPPORT_CHECK_ENABLED", "true")
    monkeypatch.setenv("MAX_SCRIPT_CORRECTION_ATTEMPTS", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


Judge = Callable[[str, bool], ns.SentenceJudgement]


def _judge_with(monkeypatch, llm: LLM, decide: Judge) -> AsyncMock:
    async def _judge(sentence, claims, *, second_opinion=False):
        return decide(sentence, second_opinion), TokenUsage(input_tokens=5, output_tokens=5)

    mock = AsyncMock(side_effect=_judge)
    monkeypatch.setattr(llm, "judge_sentence_support", mock)
    return mock


def _flags_bad(sentence: str, second_opinion: bool) -> ns.SentenceJudgement:
    if "open their stomata" in sentence:
        return ns.SentenceJudgement("unsupported", "the claims say water shortage closes stomata")
    return ns.SentenceJudgement("supported", "a claim says so")


async def _texts(db_session, enq) -> list[str]:
    return [
        segment.narration_text
        for segment in await scripts_repo.list_segments(
            db_session, project_id=enq.job.project_id, script_id=enq.script.id
        )
    ]


async def test_an_unsupported_sentence_is_rewritten_and_the_writer_is_told_why(
    db_session, monkeypatch
):
    _o, _p, topic, claim, enq = await _enqueued(db_session, claim_text=CLAIM)
    llm, _outline, section = _mock_llm(
        monkeypatch,
        sections_by_topic={
            topic.id: [
                [_segment(f"{GOOD} {BAD}", [str(claim.id)])],
                [_segment(f"{GOOD} {GOOD_TWO}", [str(claim.id)])],
            ]
        },
    )
    judge = _judge_with(monkeypatch, llm, _flags_bad)

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded"
    assert section.await_count == 2
    assert any(
        BAD in line and "no verified claim supports it" in line for line in llm.received_corrections
    )
    assert await _texts(db_session, enq) == [f"{GOOD} {GOOD_TWO}"]
    check = job.result["support_check"]
    assert check["rewrites_for_support"] == 1 and check["removed_sentences"] == []
    assert check["judged_sentences"] >= 3 and judge.await_count >= 3
    [retry] = job.result["section_retries"]
    assert retry["unsupported_sentences"] == 1


async def test_a_sentence_still_unsupported_after_the_rewrites_is_taken_out_not_kept(
    db_session, monkeypatch
):
    _o, _p, topic, claim, enq = await _enqueued(db_session, claim_text=CLAIM)
    always_bad = [_segment(f"{GOOD} {BAD} {GOOD_TWO}", [str(claim.id)])]
    llm, _outline, section = _mock_llm(monkeypatch, sections_by_topic={topic.id: [always_bad]})
    _judge_with(monkeypatch, llm, _flags_bad)

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded"
    assert section.await_count == 3  # the first draft and the two bounded rewrites
    assert await _texts(db_session, enq) == [f"{GOOD} {GOOD_TWO}"]
    [removed] = job.result["support_check"]["removed_sentences"]
    assert removed["sentence"] == BAD
    assert "closes stomata" in removed["reason"] and removed["segment_dropped"] is False


async def test_a_segment_left_too_short_to_read_out_is_dropped(db_session, monkeypatch):
    _o, _p, topic, claim, enq = await _enqueued(db_session, claim_text=CLAIM)
    draft = [
        _segment(f"{GOOD} {GOOD_TWO}", [str(claim.id)]),
        _segment("Plants need water to open their stomata.", [str(claim.id)]),
    ]
    llm, _outline, _section = _mock_llm(monkeypatch, sections_by_topic={topic.id: [draft]})
    _judge_with(monkeypatch, llm, _flags_bad)

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded"
    assert await _texts(db_session, enq) == [f"{GOOD} {GOOD_TWO}"]
    assert job.result["support_check"]["removed_sentences"][0]["segment_dropped"] is True


async def test_a_section_with_nothing_the_claims_support_fails_instead_of_being_kept(
    db_session, monkeypatch
):
    _o, _p, topic, claim, enq = await _enqueued(db_session, claim_text=CLAIM)
    llm, _outline, _section = _mock_llm(
        monkeypatch,
        sections_by_topic={
            topic.id: [[_segment(BAD + " Plants need water to open them.", [str(claim.id)])]]
        },
    )
    _judge_with(
        monkeypatch,
        llm,
        lambda s, _second: ns.SentenceJudgement("unsupported", "not in the claims"),
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "failed"
    assert "supported by its verified claims" in job.error


async def test_a_judge_that_cannot_answer_leaves_the_sentence_in_and_says_so(
    db_session, monkeypatch
):
    _o, _p, topic, claim, enq = await _enqueued(db_session, claim_text=CLAIM)
    llm, _outline, _section = _mock_llm(
        monkeypatch, sections_by_topic={topic.id: [[_segment(f"{GOOD} {BAD}", [str(claim.id)])]]}
    )
    monkeypatch.setattr(
        llm, "judge_sentence_support", AsyncMock(side_effect=LLMResponseError("cut off"))
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded"
    assert await _texts(db_session, enq) == [f"{GOOD} {BAD}"]
    assert job.result["support_check"]["unchecked_sentences"] == 2


async def test_signposts_and_questions_are_not_judged_as_claims(db_session, monkeypatch):
    _o, _p, topic, claim, enq = await _enqueued(db_session, claim_text=CLAIM)
    narration = f"Let's look at water and stomata. What happens when water is scarce? {GOOD}"
    llm, _outline, _section = _mock_llm(
        monkeypatch, sections_by_topic={topic.id: [[_segment(narration, [str(claim.id)])]]}
    )
    judge = _judge_with(
        monkeypatch, llm, lambda s, _second: ns.SentenceJudgement("supported", "ok")
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded"
    judged = [call.args[0] for call in judge.await_args_list]
    assert GOOD in judged and not any("Let's look" in s or s.endswith("?") for s in judged)


async def test_a_sentence_with_a_cause_cue_gets_a_second_stricter_opinion(db_session, monkeypatch):
    _o, _p, topic, claim, enq = await _enqueued(db_session, claim_text=CLAIM)
    cued = "Plants close their stomata because water is scarce."
    llm, _outline, _section = _mock_llm(
        monkeypatch,
        sections_by_topic={topic.id: [[_segment(f"{GOOD} {cued}", [str(claim.id)])]]},
    )

    def decide(sentence: str, second_opinion: bool) -> ns.SentenceJudgement:
        if second_opinion and "because" in sentence:
            return ns.SentenceJudgement("unsupported", "no claim gives that reason")
        return ns.SentenceJudgement("supported", "ok")

    judge = _judge_with(monkeypatch, llm, decide)

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded"
    second = [c for c in judge.await_args_list if c.kwargs.get("second_opinion")]
    assert [c.args[0] for c in second] == [cued]  # only the cued sentence is asked twice
    assert await _texts(db_session, enq) == [GOOD]
    assert job.result["support_check"]["second_opinions"] == 1


async def test_the_check_is_skipped_when_switched_off(db_session, monkeypatch):
    monkeypatch.setenv("SCRIPT_SUPPORT_CHECK_ENABLED", "false")
    get_settings.cache_clear()
    _o, _p, topic, claim, enq = await _enqueued(db_session, claim_text=CLAIM)
    llm, _outline, _section = _mock_llm(
        monkeypatch, sections_by_topic={topic.id: [[_segment(f"{GOOD} {BAD}", [str(claim.id)])]]}
    )
    judge = _judge_with(monkeypatch, llm, _flags_bad)

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded" and job.result["support_check"]["enabled"] is False
    judge.assert_not_awaited()


async def test_the_judge_calls_are_counted_under_their_own_usage_kind(db_session, monkeypatch):
    _o, _p, topic, claim, enq = await _enqueued(db_session, claim_text=CLAIM)
    llm, _outline, _section = _mock_llm(
        monkeypatch, sections_by_topic={topic.id: [[_segment(GOOD, [str(claim.id)])]]}
    )
    _judge_with(monkeypatch, llm, lambda s, _second: ns.SentenceJudgement("supported", "ok"))

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    usage = job.result["usage"]["by_call"]["narration_support"]
    assert usage["calls"] >= 1 and usage["input_tokens"] >= 5
