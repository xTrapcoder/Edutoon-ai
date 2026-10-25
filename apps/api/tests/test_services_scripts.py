"""``services/scripts.py`` - Script Generation Engine service layer (Phase 7:
duration-aware, topic-scoped generation).

``LLM``'s two Phase 7 methods (``compose_script_outline`` /
``compose_script_section``) are mocked directly here, the same pattern
``_llm_raising`` already used for failure injection: this file is about the
service's *orchestration* (topic scoping, duration allocation, correction,
atomicity, progress), not the provider's own request/response parsing, which
``test_llm_provider.py`` already covers in full. A real ``LLM``/``AsyncAnthropic``
pair is still used underneath (never a real network call) so nothing about the
``LLM`` type itself is faked away.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from anthropic import AsyncAnthropic
from sqlalchemy import select

import edutoon.db.session as session_module
from edutoon.core.config import get_settings
from edutoon.core.errors import ConflictError, NotFoundError, ValidationError
from edutoon.db.database import dispose_engine, get_engine
from edutoon.db.session import get_sessionmaker
from edutoon.db.tables import claim_citations as claim_citations_table
from edutoon.db.tables import claim_verifications as claim_verifications_table
from edutoon.db.tables import projects as projects_table
from edutoon.db.tables import script_segment_claims as script_segment_claims_table
from edutoon.db.tables import users as users_table
from edutoon.domain.script import ScriptOutlineDraft, ScriptSegmentDraft, TopicOutlineDraft
from edutoon.providers import errors as provider_errors
from edutoon.providers.llm import (
    LLM,
    Claim,
    LLMResponseError,
    OutlineTopicBrief,
    ScriptBrief,
    SectionContinuity,
    SectionTopicBrief,
    TokenUsage,
)
from edutoon.repositories import claim_verifications as claim_verifications_repo
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.claim_verifications import ClaimVerificationRecord, NewClaimCitation
from edutoon.repositories.project_topics import ProjectTopicRecord
from edutoon.repositories.source_chunks import NewSourceChunk
from edutoon.services import audit_logs as audit_logs_service
from edutoon.services import job_errors
from edutoon.services import scripts as scripts_service
from tests.leaky_errors import assert_clean, leaky_anthropic
from tests.storage_isolation import UPLOADS_BUCKET

# Stands in for an external provider (OpenAI / Anthropic): these tests prove how our code
# handles a response, never how a real provider behaves. See "Provider-mocked tests"
# in apps/api/README.md.
pytestmark = pytest.mark.provider_mocked

SCRIPT_MODEL = "claude-sonnet-5"


@pytest.fixture(autouse=True)
def _no_duration_correction(monkeypatch):
    """Phase 7's bounded duration-correction pass would otherwise fire for
    almost any test's necessarily-short fake narration: the shortest target
    duration a project may have is 60 seconds, far longer than any short test
    sentence. Disabled by default so every test but the ones explicitly about
    correction gets one predictable pass; those tests turn it back on
    themselves.
    """
    monkeypatch.setenv("MAX_SCRIPT_CORRECTION_ATTEMPTS", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- fixtures: projects, topics, claims ---------------------------------------------


async def _make_project(db_session, *, title: str = "Photosynthesis", **project_fields: Any):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title=title, source_type="pdf", **project_fields
    )
    return owner, project


async def _add_supported_claim(db_session, project, topic: ProjectTopicRecord, text: str):
    source = await uploaded_sources_repo.create(
        db_session,
        project_id=project.id,
        original_filename="notes.pdf",
        storage_bucket=UPLOADS_BUCKET,
        storage_key=f"{project.id}/{uuid4()}.pdf",
        byte_size=1024,
        checksum_sha256=uuid4().hex + uuid4().hex[:32],
    )
    [chunk] = await source_chunks_repo.create_many(
        db_session,
        [NewSourceChunk(source_id=source.id, project_id=project.id, chunk_index=0, content=text)],
    )
    return await claim_verifications_repo.create(
        db_session,
        project_id=project.id,
        claim_text=text,
        status="supported",
        embedding_model="text-embedding-3-small",
        llm_model=SCRIPT_MODEL,
        topic_id=topic.id,
        citations=[NewClaimCitation(source_chunk_id=chunk.id, similarity=0.9)],
    )


async def _project_with_topics(
    db_session, topic_specs: list[tuple[str, list[str]]], **project_fields: Any
) -> tuple[Any, Any, list[ProjectTopicRecord], dict[UUID, list[ClaimVerificationRecord]]]:
    """``topic_specs`` is one ``(title, [claim_text, ...])`` per accepted
    topic to create, each with its own supported claims.
    """
    owner, project = await _make_project(db_session, **project_fields)
    topics: list[ProjectTopicRecord] = []
    claims_by_topic: dict[UUID, list[ClaimVerificationRecord]] = {}
    for position, (title, claim_texts) in enumerate(topic_specs):
        topic = await project_topics_repo.create(
            db_session,
            project_id=project.id,
            title=title,
            status="accepted",
            origin="source",
            position=position,
        )
        topics.append(topic)
        claims_by_topic[topic.id] = [
            await _add_supported_claim(db_session, project, topic, text) for text in claim_texts
        ]
    return owner, project, topics, claims_by_topic


async def _enqueued(
    db_session, *, key: str = "idem-1", claim_text: str = "A fact.", **project_fields: Any
):
    owner, project, [topic], claims_by_topic = await _project_with_topics(
        db_session, [("A topic", [claim_text])], **project_fields
    )
    claim = claims_by_topic[topic.id][0]
    result = await scripts_service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key=key
    )
    return owner, project, topic, claim, result


# --- fake LLM: mocks the two Phase 7 methods directly -------------------------------


def _mock_llm(
    monkeypatch: Any,
    *,
    sections_by_topic: dict[UUID, list[list[ScriptSegmentDraft]]],
    outline_title: str = "How photosynthesis works",
    model: str = SCRIPT_MODEL,
) -> tuple[LLM, AsyncMock, AsyncMock]:
    """An ``LLM`` whose outline call echoes back whatever topics it is given
    (generic purpose, no key concepts) and whose section call returns
    ``sections_by_topic[topic_id][call_index]`` - a *list of generations* per
    topic, so a test can give the correction pass a different response than
    the first pass (the last entry repeats for any further call).
    """
    llm = LLM(AsyncAnthropic(api_key="sk-ant-test-not-a-real-key"), model=model)
    call_counts: dict[UUID, int] = {}
    received_corrections: list[str] = []
    llm.received_corrections = received_corrections  # type: ignore[attr-defined]

    async def _outline(
        brief: ScriptBrief, topics: Sequence[OutlineTopicBrief]
    ) -> tuple[ScriptOutlineDraft, TokenUsage]:
        return (
            ScriptOutlineDraft(
                title=outline_title,
                topics=tuple(
                    TopicOutlineDraft(topic_id=topic.topic_id, purpose="p", key_concepts=())
                    for topic in topics
                ),
            ),
            TokenUsage(input_tokens=10, output_tokens=10),
        )

    async def _section(
        brief: ScriptBrief,
        topic: SectionTopicBrief,
        claims: list[Claim],
        continuity: SectionContinuity,
        *,
        duration_ms: int,
        unit_index: int,
        unit_count: int,
        max_segments: int,
        max_claims: int,
        words_per_minute: int,
        corrections: Sequence[str] = (),
    ) -> tuple[list[ScriptSegmentDraft], TokenUsage]:
        if corrections:
            received_corrections.extend(corrections)
        generations = sections_by_topic[topic.topic_id]
        index = call_counts.get(topic.topic_id, 0)
        call_counts[topic.topic_id] = index + 1
        chosen = generations[min(index, len(generations) - 1)]
        return list(chosen), TokenUsage(input_tokens=10, output_tokens=10)

    outline_mock = AsyncMock(side_effect=_outline)
    section_mock = AsyncMock(side_effect=_section)
    monkeypatch.setattr(llm, "compose_script_outline", outline_mock)
    monkeypatch.setattr(llm, "compose_script_section", section_mock)
    return llm, outline_mock, section_mock


def _simple_llm(
    monkeypatch: Any,
    *,
    segments_by_topic: dict[UUID, list[ScriptSegmentDraft]],
    outline_title: str = "How photosynthesis works",
    model: str = SCRIPT_MODEL,
) -> tuple[LLM, AsyncMock, AsyncMock]:
    return _mock_llm(
        monkeypatch,
        sections_by_topic={
            topic_id: [segments] for topic_id, segments in segments_by_topic.items()
        },
        outline_title=outline_title,
        model=model,
    )


def _llm_raising_on_outline(monkeypatch: Any, exc: Exception, *, model: str = SCRIPT_MODEL) -> LLM:
    llm = LLM(AsyncAnthropic(api_key="sk-ant-test-not-a-real-key"), model=model)
    monkeypatch.setattr(llm, "compose_script_outline", AsyncMock(side_effect=exc))
    return llm


# --- fake segments -------------------------------------------------------------------


# The words of every claim these tests create, so a padded segment that cites a claim shares
# wording with it (a citation that shares none is dropped - domain.script_provenance).
_PAD_WORDS = [
    "fact",
    "first",
    "second",
    "third",
    "chlorophyll",
    "absorbs",
    "light",
    "carbon",
    "fixed",
    "plants",
    "convert",
    "energy",
    "another",
    "word",
]


def _words_for_duration(duration_ms: int, *, wpm: int = 150) -> int:
    return max(1, round(duration_ms / 60_000 * wpm))


def _narration_of_duration(duration_ms: int, *, wpm: int = 150) -> str:
    """Padded narration whose word count lands on ``duration_ms`` at ``wpm`` -
    so a test controls the duration the service will compute without needing
    to hand-count words in realistic prose.
    """
    count = _words_for_duration(duration_ms, wpm=wpm)
    return " ".join(_PAD_WORDS[index % len(_PAD_WORDS)] for index in range(count))


def _segment(
    narration: str, claim_ids: Sequence[str] = (), *, connective: bool = False, wpm: int = 150
) -> ScriptSegmentDraft:
    return ScriptSegmentDraft(
        segment_index=0,
        narration_text=narration,
        claim_ids=list(claim_ids),
        estimated_duration_ms=round(len(narration.split()) / wpm * 60_000),
        connective=connective,
    )


def _on_target(claim_ids: Sequence[str] = (), *, duration_ms: int = 300_000) -> ScriptSegmentDraft:
    """A single segment padded to ``duration_ms`` (default: a project's own
    default target) - connective only when given no claims - so a test that
    is not itself about duration correction does not trigger it.
    """
    return _segment(_narration_of_duration(duration_ms), claim_ids, connective=not claim_ids)


# --- enqueue_script_generation --------------------------------------------------


async def test_enqueue_creates_a_pending_script_and_a_queued_job(db_session):
    owner, project, topic, _claim, result = await _enqueued(db_session)

    assert result.replayed is False
    assert result.script.status == "pending"
    assert result.script.project_id == project.id
    assert result.job.kind == "generate_script"
    assert result.job.status == "queued"
    assert result.job.payload == {
        "script_id": str(result.script.id),
        "project_id": str(project.id),
        "topic_ids": [str(topic.id)],
    }


async def test_enqueue_writes_an_audit_log(db_session):
    _owner, _project, _topic, _claim, result = await _enqueued(db_session)

    logs = await audit_logs_service.list_audit_logs_for_entity(
        db_session, entity_type="script", entity_id=result.script.id
    )
    assert [log.action for log in logs.items] == ["script.generation_enqueued"]
    assert logs.items[0].context["job_id"] == str(result.job.id)
    assert logs.items[0].context["accepted_topic_count"] == 1


async def test_enqueue_requires_at_least_one_accepted_topic(db_session):
    owner, project = await _make_project(db_session)
    # A merely proposed topic does not count.
    await project_topics_repo.create(
        db_session, project_id=project.id, title="Not yet accepted", status="proposed"
    )

    with pytest.raises(ValidationError, match="accepted topic"):
        await scripts_service.enqueue_script_generation(
            db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
        )


async def test_enqueue_requires_every_accepted_topic_to_have_supported_evidence(db_session):
    owner, project = await _make_project(db_session)
    await project_topics_repo.create(
        db_session,
        project_id=project.id,
        title="Photosynthesis",
        status="accepted",
        origin="source",
    )

    with pytest.raises(ValidationError, match="Photosynthesis"):
        await scripts_service.enqueue_script_generation(
            db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
        )


async def _keyword_topic_with_a_supported_claim(db_session, *, title: str, document_text: str):
    """A keyword the owner typed (origin ``user``) whose one supported statement is cited from a
    document that says ``document_text``."""
    owner, project = await _make_project(db_session, title="Ecosystems")
    topic = await project_topics_repo.create(
        db_session,
        project_id=project.id,
        title=title,
        status="accepted",
        origin="user",
    )
    await _add_supported_claim(db_session, project, topic, document_text)
    return owner, project


async def test_enqueue_refuses_a_keyword_the_document_never_mentions_despite_supported_claims(
    db_session,
):
    """Regression (a real run): "coral bleaching" over a chapter on ecosystems. Statements about
    the nearest passages were judged supported, so the keyword looked covered and a section titled
    "coral bleaching" would have taught invasive species and keystone species under it."""
    owner, project = await _keyword_topic_with_a_supported_claim(
        db_session,
        title="Coral bleaching",
        document_text="Invasive species can harm native ecosystems by spreading quickly.",
    )

    with pytest.raises(ValidationError, match="Coral bleaching") as refused:
        await scripts_service.enqueue_script_generation(
            db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
        )
    assert refused.value.details["topics"] == ["Coral bleaching"]


async def test_enqueue_accepts_a_keyword_the_document_does_mention(db_session):
    owner, project = await _keyword_topic_with_a_supported_claim(
        db_session,
        title="Invasive species",
        document_text="Invasive species can harm native ecosystems by spreading quickly.",
    )

    result = await scripts_service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
    )

    assert result.replayed is False


async def test_enqueue_ignores_a_partially_supported_claim_as_evidence(db_session):
    owner, project = await _make_project(db_session)
    topic = await project_topics_repo.create(
        db_session,
        project_id=project.id,
        title="Photosynthesis",
        status="accepted",
        origin="source",
    )
    await claim_verifications_repo.create(
        db_session,
        project_id=project.id,
        claim_text="Not fully backed.",
        status="partially_supported",
        embedding_model="text-embedding-3-small",
        llm_model=SCRIPT_MODEL,
        topic_id=topic.id,
    )

    with pytest.raises(ValidationError, match="Photosynthesis"):
        await scripts_service.enqueue_script_generation(
            db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
        )


async def test_enqueue_ignores_a_hand_typed_claim_with_no_topic(db_session):
    """A claim verified through the manual route (``topic_id`` always
    ``NULL``) belongs to no topic's section, so it can never satisfy an
    accepted topic's evidence requirement - never borrowed.
    """
    owner, project = await _make_project(db_session)
    await project_topics_repo.create(
        db_session,
        project_id=project.id,
        title="Photosynthesis",
        status="accepted",
        origin="source",
    )
    await claim_verifications_repo.create(
        db_session,
        project_id=project.id,
        claim_text="Typed in by hand.",
        status="supported",
        embedding_model="text-embedding-3-small",
        llm_model=SCRIPT_MODEL,
        topic_id=None,
    )

    with pytest.raises(ValidationError, match="Photosynthesis"):
        await scripts_service.enqueue_script_generation(
            db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
        )


async def test_enqueue_rejects_more_accepted_topics_than_the_configured_maximum(
    db_session, monkeypatch
):
    monkeypatch.setenv("MAX_SCRIPT_SECTIONS", "1")
    get_settings.cache_clear()
    try:
        owner, project, _topics, _claims = await _project_with_topics(
            db_session, [("Topic A", ["A fact."]), ("Topic B", ["Another fact."])]
        )

        with pytest.raises(ValidationError, match="at most 1"):
            await scripts_service.enqueue_script_generation(
                db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
            )
    finally:
        get_settings.cache_clear()


async def test_enqueue_rejects_too_many_topics_for_the_target_duration(db_session, monkeypatch):
    monkeypatch.setenv("MIN_SECTION_DURATION_MS", "200000")
    get_settings.cache_clear()
    try:
        # Default target duration is 300_000ms; two topics at a 200_000ms floor
        # each cannot both fit.
        owner, project, _topics, _claims = await _project_with_topics(
            db_session, [("Topic A", ["A fact."]), ("Topic B", ["Another fact."])]
        )

        with pytest.raises(ValidationError, match="cannot each receive"):
            await scripts_service.enqueue_script_generation(
                db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
            )
    finally:
        get_settings.cache_clear()


async def test_enqueue_is_idempotent_on_the_key(db_session):
    owner, project, _topic, _claim, first = await _enqueued(db_session, key="same")
    second = await scripts_service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="same"
    )

    assert second.replayed is True
    assert second.script.id == first.script.id
    assert second.job.id == first.job.id
    page = await jobs_repo.list_by_project(db_session, project.id)
    assert len(page.items) == 1


async def test_enqueue_rejects_a_project_the_caller_does_not_own(db_session):
    _owner, project, _topic, _claim, _result = await _enqueued(db_session)
    other = await users_repo.create(db_session, email=f"{uuid4()}@example.com")

    with pytest.raises(NotFoundError):
        await scripts_service.enqueue_script_generation(
            db_session, project_id=project.id, owner_id=other.id, idempotency_key="k"
        )


async def test_enqueue_records_the_configured_script_model(db_session):
    owner, project, _topics, _claims = await _project_with_topics(
        db_session, [("A topic", ["A fact."])]
    )

    result = await scripts_service.enqueue_script_generation(
        db_session,
        project_id=project.id,
        owner_id=owner.id,
        idempotency_key="k",
        llm_model="claude-opus-5",
    )

    assert result.script.llm_model == "claude-opus-5"


async def test_enqueue_does_not_touch_an_existing_approved_script(db_session, monkeypatch):
    owner, project, topic, claim, enq = await _enqueued(db_session)
    llm, _outline_mock, _section_mock = _simple_llm(
        monkeypatch, segments_by_topic={topic.id: [_on_target([str(claim.id)])]}
    )
    await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)
    approved = await scripts_service.approve_script(
        db_session, project_id=project.id, owner_id=owner.id, script_id=enq.script.id
    )
    assert approved.status == "approved"

    # Enqueuing a new generation must not itself touch the approved script -
    # only a *successful* attach may (migration 0013).
    await scripts_service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="second"
    )

    still_approved = await scripts_repo.get_by_id(db_session, enq.script.id)
    assert still_approved is not None
    assert still_approved.status == "approved"


# --- run_generate_script_job: topic scoping and success -----------------------------


async def test_run_scopes_each_section_to_its_own_topic(db_session, monkeypatch):
    owner, project, topics, claims_by_topic = await _project_with_topics(
        db_session,
        [
            ("Light reactions", ["Chlorophyll absorbs light."]),
            ("Calvin cycle", ["Carbon is fixed."]),
        ],
    )
    topic_a, topic_b = topics
    claim_a = claims_by_topic[topic_a.id][0]
    claim_b = claims_by_topic[topic_b.id][0]
    enq = await scripts_service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
    )
    llm, _outline_mock, section_mock = _mock_llm(
        monkeypatch,
        sections_by_topic={
            topic_a.id: [[_on_target([str(claim_a.id)], duration_ms=150_000)]],
            topic_b.id: [[_on_target([str(claim_b.id)], duration_ms=150_000)]],
        },
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded"
    assert job.result["topic_count"] == 2
    assert job.result["segment_count"] == 2

    # Every section call only ever received its own topic's claims - never
    # the other's (rule: never borrow another topic's claims).
    for call in section_mock.call_args_list:
        called_topic: SectionTopicBrief = call.args[1]
        called_claims: list[Claim] = call.args[2]
        expected_id = str(claim_a.id) if called_topic.topic_id == topic_a.id else str(claim_b.id)
        assert [c.id for c in called_claims] == [expected_id]

    segments = await scripts_repo.list_segments(
        db_session, project_id=project.id, script_id=enq.script.id
    )
    assert {s.topic_id for s in segments} == {topic_a.id, topic_b.id}


async def test_run_caps_claims_per_section_to_the_oldest(db_session, monkeypatch):
    monkeypatch.setenv("MAX_CLAIMS_PER_SECTION", "2")
    get_settings.cache_clear()
    try:
        owner, project, [topic], claims_by_topic = await _project_with_topics(
            db_session, [("A topic", ["First fact.", "Second fact.", "Third fact."])]
        )
        first, second, third = claims_by_topic[topic.id]
        enq = await scripts_service.enqueue_script_generation(
            db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
        )
        llm, _outline_mock, section_mock = _simple_llm(
            monkeypatch, segments_by_topic={topic.id: [_on_target([str(first.id)])]}
        )

        job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

        assert job.status == "succeeded"
        offered = section_mock.await_args_list[0].args[2]
        # Oldest-first, deterministically - never the newest, never random, never all three.
        assert [c.id for c in offered] == [str(first.id), str(second.id)]
        assert str(third.id) not in [c.id for c in offered]
    finally:
        get_settings.cache_clear()


async def test_run_writes_the_script_in_the_projects_own_language(db_session, monkeypatch):
    """The stored ``projects.language`` is the only thing that picks the
    language: it flows project -> service -> ``ScriptBrief`` -> both provider
    calls.
    """
    owner, project, [topic], claims_by_topic = await _project_with_topics(
        db_session, [("A topic", ["A fact."])], language="en-IN"
    )
    claim = claims_by_topic[topic.id][0]
    enq = await scripts_service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
    )
    llm, outline_mock, section_mock = _simple_llm(
        monkeypatch, segments_by_topic={topic.id: [_on_target([str(claim.id)])]}
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded"
    assert outline_mock.await_args_list[0].args[0].language == "en-IN"
    assert section_mock.await_args_list[0].args[0].language == "en-IN"


async def test_run_marks_job_and_script_failed_on_a_provider_error(db_session, monkeypatch):
    _owner, _project, _topic, _claim, enq = await _enqueued(db_session)
    llm = _llm_raising_on_outline(monkeypatch, LLMResponseError("model returned junk"))

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "failed"
    assert "junk" in job.error
    script = await scripts_repo.get_by_id(db_session, enq.script.id)
    assert script.status == "failed"


async def test_run_never_raises_on_a_value_error_from_the_provider(db_session, monkeypatch):
    _owner, _project, _topic, _claim, enq = await _enqueued(db_session)
    llm = _llm_raising_on_outline(monkeypatch, ValueError("too many claims"))

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "failed"


async def test_run_takes_out_a_passage_that_stays_unsourced_after_the_rewrites(
    db_session, monkeypatch
):
    """Nothing unsourced reaches the script: the writer is asked to fix it, and a passage that is
    still uncited after the bounded rewrites is taken out, with the cited one kept (a whole section
    is not thrown away for one passage). It is recorded on the job's result."""
    _owner, project, topic, claim, enq = await _enqueued(db_session)
    llm, _outline_mock, _section_mock = _mock_llm(
        monkeypatch,
        sections_by_topic={
            topic.id: [
                [
                    _segment("A properly cited fact.", [str(claim.id)]),
                    _segment("An unsourced assertion with no citation.", []),
                ]
            ]  # the last entry repeats: the retry writes the same thing
        },
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded"
    assert llm.received_corrections  # it was told what was wrong
    texts = [
        segment.narration_text
        for segment in await scripts_repo.list_segments(
            db_session, project_id=enq.job.project_id, script_id=enq.script.id
        )
    ]
    assert texts == ["A properly cited fact."]
    [removed] = job.result["support_check"]["removed_sentences"]
    assert removed["sentence"] == "An unsourced assertion with no citation."
    assert "cites no verified claim" in removed["reason"]


async def test_run_accepts_a_section_that_the_writer_fixes_when_told_what_was_wrong(
    db_session, monkeypatch
):
    """The retry never lowers a bar: the rewritten section passes the same checks, the rejected
    wording is not in the script, and the job's result says the section was rewritten and why."""
    _owner, project, topic, claim, enq = await _enqueued(db_session)
    llm, _outline_mock, _section_mock = _mock_llm(
        monkeypatch,
        sections_by_topic={
            topic.id: [
                [
                    _segment("A properly cited fact.", [str(claim.id)]),
                    _segment("An unsourced assertion with no citation.", []),
                ],
                [
                    _segment("A properly cited fact.", [str(claim.id)]),
                    _segment("A short hand-off.", [], connective=True),
                ],
            ]
        },
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded"
    texts = [
        segment.narration_text
        for segment in await scripts_repo.list_segments(
            db_session, project_id=enq.job.project_id, script_id=enq.script.id
        )
    ]
    assert texts == ["A properly cited fact.", "A short hand-off."]
    assert "unsourced" not in " ".join(texts)
    [retry] = job.result["section_retries"]
    assert retry["rejected_passages"] == 1
    assert "lists no claim ids" in retry["problems"][0]
    assert "An unsourced assertion" in retry["problems"][0]


async def test_run_persists_nothing_when_a_later_topic_fails_even_though_an_earlier_one_succeeded(
    db_session, monkeypatch
):
    """Atomicity (rule 13): topics are generated one after another, but
    nothing is ever attached until every one of them has succeeded - a later
    topic's failure must not leave an earlier, perfectly good topic's
    segments behind.
    """
    owner, project, topics, claims_by_topic = await _project_with_topics(
        db_session,
        [
            ("Light reactions", ["Chlorophyll absorbs light."]),
            ("Calvin cycle", ["Carbon is fixed."]),
        ],
    )
    topic_a, topic_b = topics
    claim_a = claims_by_topic[topic_a.id][0]
    enq = await scripts_service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
    )
    llm, _outline_mock, _section_mock = _mock_llm(
        monkeypatch,
        sections_by_topic={
            # Topic A (generated first, in reading order) succeeds cleanly.
            topic_a.id: [[_on_target([str(claim_a.id)], duration_ms=150_000)]],
            # Topic B fails provenance validation.
            topic_b.id: [[_segment("Unsourced.", [])]],
        },
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "failed"
    script = await scripts_repo.get_by_id(db_session, enq.script.id)
    assert script.status == "failed"
    # Topic A's perfectly good segment never reached the database either.
    assert (
        await scripts_repo.list_segments(db_session, project_id=project.id, script_id=enq.script.id)
        == []
    )


async def test_run_raises_not_found_for_a_missing_job(db_session):
    llm = LLM(AsyncAnthropic(api_key="sk-ant-test-not-a-real-key"), model=SCRIPT_MODEL)

    with pytest.raises(NotFoundError):
        await scripts_service.run_generate_script_job(db_session, llm, job_id=uuid4())


async def test_run_fails_cleanly_if_the_pending_script_was_superseded(db_session, monkeypatch):
    _owner, project, topic, claim, enq = await _enqueued(db_session)
    # Something else superseded the pending script before the worker got to it.
    await scripts_repo.supersede_active(db_session, project_id=project.id)
    llm, _outline_mock, _section_mock = _simple_llm(
        monkeypatch, segments_by_topic={topic.id: [_on_target([str(claim.id)])]}
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "failed"


async def test_run_increments_attempts_across_runs(db_session, monkeypatch):
    _owner, _project, _topic, _claim, enq = await _enqueued(db_session)
    llm = _llm_raising_on_outline(monkeypatch, LLMResponseError("boom"))

    first = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)
    second = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert first.attempts == 1
    assert second.attempts == 2


# --- run_generate_script_job: duration tolerance and correction ---------------------


async def test_run_accepts_a_script_within_duration_tolerance_without_correcting(
    db_session, monkeypatch
):
    monkeypatch.setenv("MAX_SCRIPT_CORRECTION_ATTEMPTS", "1")
    get_settings.cache_clear()
    try:
        _owner, project, topic, claim, enq = await _enqueued(db_session)
        # Default target is 300_000ms; land dead on it.
        llm, _outline_mock, section_mock = _simple_llm(
            monkeypatch, segments_by_topic={topic.id: [_on_target([str(claim.id)])]}
        )

        job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

        assert job.status == "succeeded"
        assert job.result["correction_applied"] is False
        section_mock.assert_awaited_once()
    finally:
        get_settings.cache_clear()


async def test_run_applies_one_correction_pass_when_outside_tolerance(db_session, monkeypatch):
    monkeypatch.setenv("MAX_SCRIPT_CORRECTION_ATTEMPTS", "1")
    get_settings.cache_clear()
    try:
        _owner, project, topic, claim, enq = await _enqueued(db_session)
        # First pass: a tiny fraction of the 300_000ms target - well outside
        # SCRIPT_DURATION_TOLERANCE (default 0.15). Second (correction) pass:
        # right on target.
        llm, _outline_mock, section_mock = _mock_llm(
            monkeypatch,
            sections_by_topic={
                topic.id: [
                    [_on_target([str(claim.id)], duration_ms=10_000)],
                    [_on_target([str(claim.id)], duration_ms=300_000)],
                ]
            },
        )

        job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

        assert job.status == "succeeded"
        assert job.result["correction_applied"] is True
        assert section_mock.await_count == 2
        script = await scripts_repo.get_by_id(db_session, enq.script.id)
        assert script.estimated_duration_ms == 300_000
    finally:
        get_settings.cache_clear()


async def test_run_accepts_the_result_after_correction_even_if_still_outside_tolerance(
    db_session, monkeypatch
):
    """Duration is a target for a not-yet-real TTS estimate, never a hard
    gate: after its one bounded correction pass, the script is accepted
    whatever the remaining mismatch.
    """
    monkeypatch.setenv("MAX_SCRIPT_CORRECTION_ATTEMPTS", "1")
    get_settings.cache_clear()
    try:
        _owner, project, topic, claim, enq = await _enqueued(db_session)
        llm, _outline_mock, section_mock = _mock_llm(
            monkeypatch,
            sections_by_topic={
                # Both passes stay far too short - the model simply cannot be
                # made to hit the target here, and that must not fail the job.
                topic.id: [[_on_target([str(claim.id)], duration_ms=10_000)]]
            },
        )

        job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

        assert job.status == "succeeded"
        assert job.result["correction_applied"] is True
        assert job.result["target_duration_ms"] == 300_000
        assert job.result["estimated_duration_ms"] == 10_000
        assert job.result["duration_difference_ms"] == -290_000
        assert section_mock.await_count == 2
    finally:
        get_settings.cache_clear()


async def test_run_never_corrects_when_correction_attempts_are_disabled(db_session, monkeypatch):
    # The autouse fixture already sets this to "0" - assert the behaviour
    # explicitly rather than relying on it implicitly.
    assert get_settings().MAX_SCRIPT_CORRECTION_ATTEMPTS == 0
    _owner, project, topic, claim, enq = await _enqueued(db_session)
    llm, _outline_mock, section_mock = _simple_llm(
        monkeypatch, segments_by_topic={topic.id: [_on_target([str(claim.id)], duration_ms=1_000)]}
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded"
    assert job.result["correction_applied"] is False
    section_mock.assert_awaited_once()


# --- run_generate_script_job: the provider-call budget is enforced ------------------


async def test_the_provider_call_budget_is_enforced_before_any_call(db_session, monkeypatch):
    _owner, project, topic, claim, enq = await _enqueued(db_session)
    monkeypatch.setattr(scripts_service, "max_provider_calls", lambda **_kwargs: 0)
    llm, outline_mock, _section_mock = _simple_llm(
        monkeypatch, segments_by_topic={topic.id: [_on_target([str(claim.id)])]}
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "failed"
    assert "provider calls" in job.error
    outline_mock.assert_not_awaited()


# --- atomicity: the previous script is never destroyed by a bad regeneration --------


async def test_a_successful_regeneration_supersedes_the_previous_approved_script(
    db_session, monkeypatch
):
    owner, project, topic, claim, enq = await _enqueued(db_session)
    llm, _outline_mock, _section_mock = _simple_llm(
        monkeypatch, segments_by_topic={topic.id: [_on_target([str(claim.id)])]}
    )
    await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)
    approved = await scripts_service.approve_script(
        db_session, project_id=project.id, owner_id=owner.id, script_id=enq.script.id
    )
    assert approved.status == "approved"

    second = await scripts_service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="second"
    )
    llm2, _outline_mock2, _section_mock2 = _simple_llm(
        monkeypatch, segments_by_topic={topic.id: [_on_target([str(claim.id)])]}
    )
    job2 = await scripts_service.run_generate_script_job(db_session, llm2, job_id=second.job.id)

    assert job2.status == "succeeded"
    first_script = await scripts_repo.get_by_id(db_session, enq.script.id)
    assert first_script.status == "superseded"
    second_script = await scripts_repo.get_by_id(db_session, second.script.id)
    assert second_script.status == "draft"


async def test_a_failed_regeneration_leaves_the_previous_approved_script_untouched(
    db_session, monkeypatch
):
    owner, project, topic, claim, enq = await _enqueued(db_session)
    llm, _outline_mock, _section_mock = _simple_llm(
        monkeypatch, segments_by_topic={topic.id: [_on_target([str(claim.id)])]}
    )
    await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)
    await scripts_service.approve_script(
        db_session, project_id=project.id, owner_id=owner.id, script_id=enq.script.id
    )
    original_segments = await scripts_repo.list_segments(
        db_session, project_id=project.id, script_id=enq.script.id
    )

    second = await scripts_service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="second"
    )
    # This regeneration produces an unsourced segment and must fail entirely.
    llm2, _outline_mock2, _section_mock2 = _mock_llm(
        monkeypatch, sections_by_topic={topic.id: [[_segment("Unsourced.", [])]]}
    )
    job2 = await scripts_service.run_generate_script_job(db_session, llm2, job_id=second.job.id)

    assert job2.status == "failed"
    failed_script = await scripts_repo.get_by_id(db_session, second.script.id)
    assert failed_script.status == "failed"

    # The previously approved script is completely untouched, segments and all.
    still_approved = await scripts_repo.get_by_id(db_session, enq.script.id)
    assert still_approved.status == "approved"
    unchanged_segments = await scripts_repo.list_segments(
        db_session, project_id=project.id, script_id=enq.script.id
    )
    assert [s.id for s in unchanged_segments] == [s.id for s in original_segments]


# --- assemble_script_detail --------------------------------------------------


async def _drafted(db_session, monkeypatch):
    owner, project, topic, claim, enq = await _enqueued(
        db_session, claim_text="Plants convert light to energy."
    )
    llm, _outline_mock, _section_mock = _mock_llm(
        monkeypatch,
        sections_by_topic={
            topic.id: [
                [
                    _on_target([], duration_ms=20_000),  # a short hand-off, well under the cap
                    _on_target([str(claim.id)], duration_ms=280_000),
                ]
            ]
        },
    )
    await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)
    return owner, project, topic, claim, enq.script.id


async def test_assemble_returns_the_script_with_segments_and_cited_claims(db_session, monkeypatch):
    owner, project, topic, claim, script_id = await _drafted(db_session, monkeypatch)

    detail = await scripts_service.assemble_script_detail(
        db_session, project_id=project.id, owner_id=owner.id, script_id=script_id
    )

    assert detail.script.id == script_id
    assert detail.script.status == "draft"
    assert [s.segment.segment_index for s in detail.segments] == [0, 1]
    assert all(s.segment.topic_id == topic.id for s in detail.segments)
    assert detail.segments[0].claims == ()
    assert [c.id for c in detail.segments[1].claims] == [claim.id]
    assert detail.segments[1].claims[0].claim_text == "Plants convert light to energy."


async def test_assemble_rejects_a_project_the_caller_does_not_own(db_session, monkeypatch):
    _owner, project, _topic, _claim, script_id = await _drafted(db_session, monkeypatch)
    other = await users_repo.create(db_session, email=f"{uuid4()}@example.com")

    with pytest.raises(NotFoundError):
        await scripts_service.assemble_script_detail(
            db_session, project_id=project.id, owner_id=other.id, script_id=script_id
        )


async def test_assemble_is_a_404_for_an_unknown_script(db_session):
    owner, project = await _make_project(db_session)

    with pytest.raises(NotFoundError):
        await scripts_service.assemble_script_detail(
            db_session, project_id=project.id, owner_id=owner.id, script_id=uuid4()
        )


# --- approve_script ---------------------------------------------------------


async def test_approve_advances_a_draft_to_approved_and_audit_logs_it(db_session, monkeypatch):
    owner, project, _topic, _claim, script_id = await _drafted(db_session, monkeypatch)

    approved = await scripts_service.approve_script(
        db_session, project_id=project.id, owner_id=owner.id, script_id=script_id
    )

    assert approved.status == "approved"
    logs = await audit_logs_service.list_audit_logs_for_entity(
        db_session, entity_type="script", entity_id=script_id
    )
    assert "script.approved" in {log.action for log in logs.items}


async def test_approve_rejects_a_script_that_is_not_a_draft(db_session):
    owner, project, _topic, _claim, result = await _enqueued(db_session)

    # still 'pending' - never drafted
    with pytest.raises(ConflictError):
        await scripts_service.approve_script(
            db_session, project_id=project.id, owner_id=owner.id, script_id=result.script.id
        )


async def test_approve_rejects_a_project_the_caller_does_not_own(db_session, monkeypatch):
    _owner, project, _topic, _claim, script_id = await _drafted(db_session, monkeypatch)
    other = await users_repo.create(db_session, email=f"{uuid4()}@example.com")

    with pytest.raises(NotFoundError):
        await scripts_service.approve_script(
            db_session, project_id=project.id, owner_id=other.id, script_id=script_id
        )


async def test_approve_is_a_404_for_an_unknown_script(db_session):
    owner, project = await _make_project(db_session)

    with pytest.raises(NotFoundError):
        await scripts_service.approve_script(
            db_session, project_id=project.id, owner_id=owner.id, script_id=uuid4()
        )


async def test_approve_is_idempotent_enough_to_reject_double_approval(db_session, monkeypatch):
    owner, project, _topic, _claim, script_id = await _drafted(db_session, monkeypatch)
    await scripts_service.approve_script(
        db_session, project_id=project.id, owner_id=owner.id, script_id=script_id
    )

    with pytest.raises(ConflictError):
        await scripts_service.approve_script(
            db_session, project_id=project.id, owner_id=owner.id, script_id=script_id
        )


# --- error sanitisation --------------------------------------------------------------


async def test_a_provider_failure_tells_the_owner_a_fixed_sentence_and_nothing_else(
    db_session, monkeypatch
):
    _owner, _project, _topic, _claim, enq = await _enqueued(db_session)
    llm = _llm_raising_on_outline(monkeypatch, leaky_anthropic(429))

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "failed" and job.error == provider_errors.RATE_LIMITED
    assert_clean(job.error)
    script = await scripts_repo.get_by_id(db_session, enq.script.id)
    assert script.status == "failed"


async def test_an_unexpected_failure_while_composing_a_script_is_generic(db_session, monkeypatch):
    _owner, _project, _topic, _claim, enq = await _enqueued(db_session)
    llm = _llm_raising_on_outline(
        monkeypatch, RuntimeError("password=hunter2 at /srv/app/worker.py")
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "failed" and job.error == job_errors.GENERIC_ERROR
    assert_clean(job.error)


# --- progress: visible mid-run, never blocking, never partial script data -----------
#
# Needs two genuinely independent connections (one running the job, one polling it)
# to prove real cross-connection visibility - the transactional ``db_session``
# fixture shares one connection via SAVEPOINTs, so it cannot exercise this. Follows
# ``test_worker.py``'s / ``test_stale_extraction_recovery.py``'s real-engine,
# real-commit, manual-cleanup pattern instead.


async def test_progress_is_visible_mid_run_without_blocking_or_partial_script_data():
    engine = get_engine()
    sessionmaker = get_sessionmaker()
    reached_section_call = asyncio.Event()
    release_section_call = asyncio.Event()

    async with sessionmaker() as setup_session:
        owner = await users_repo.create(setup_session, email=f"{uuid4()}@example.com")
        project = await projects_repo.create(
            setup_session, owner_id=owner.id, title="P", source_type="pdf"
        )
        topic = await project_topics_repo.create(
            setup_session,
            project_id=project.id,
            title="A topic",
            status="accepted",
            origin="source",
        )
        claim = await _add_supported_claim(setup_session, project, topic, "A fact.")
        await setup_session.commit()

    try:
        async with sessionmaker() as enqueue_session:
            enq = await scripts_service.enqueue_script_generation(
                enqueue_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
            )
            await enqueue_session.commit()

        llm = LLM(AsyncAnthropic(api_key="sk-ant-test-not-a-real-key"), model=SCRIPT_MODEL)

        async def _outline(brief, topics):
            return (
                ScriptOutlineDraft(
                    title="T",
                    topics=tuple(
                        TopicOutlineDraft(topic_id=t.topic_id, purpose="p", key_concepts=())
                        for t in topics
                    ),
                ),
                TokenUsage(input_tokens=1, output_tokens=1),
            )

        async def _section(brief, topic_brief, claims, continuity, **_kwargs):
            reached_section_call.set()
            await release_section_call.wait()
            return [_on_target([str(claim.id)])], TokenUsage(input_tokens=1, output_tokens=1)

        llm.compose_script_outline = AsyncMock(side_effect=_outline)  # type: ignore[method-assign]
        llm.compose_script_section = AsyncMock(side_effect=_section)  # type: ignore[method-assign]

        async def _run() -> None:
            async with sessionmaker() as run_session:
                await scripts_service.run_generate_script_job(
                    run_session, llm, job_id=enq.job.id, sessionmaker=sessionmaker
                )
                await run_session.commit()

        run_task = asyncio.create_task(_run())
        await asyncio.wait_for(reached_section_call.wait(), timeout=5)

        # A fully independent connection, while generation is paused mid-call.
        async with sessionmaker() as poll_session:
            mid_run_job = await jobs_repo.get_by_id(poll_session, enq.job.id)
            assert mid_run_job is not None
            assert mid_run_job.status == "running"
            progress = mid_run_job.payload.get("progress")
            assert progress is not None
            assert progress["stage"] == "generating_topic"
            assert progress["topic_title"] == "A topic"
            assert progress["topic_index"] == 0
            assert progress["topic_count"] == 1

            mid_run_segments = await scripts_repo.list_segments(
                poll_session, project_id=project.id, script_id=enq.script.id
            )
            assert mid_run_segments == []
            mid_run_script = await scripts_repo.get_by_id(poll_session, enq.script.id)
            assert mid_run_script is not None
            assert mid_run_script.status == "pending"

        release_section_call.set()
        await asyncio.wait_for(run_task, timeout=5)

        async with sessionmaker() as verify_session:
            finished_job = await jobs_repo.get_by_id(verify_session, enq.job.id)
            assert finished_job is not None and finished_job.status == "succeeded"
            finished_script = await scripts_repo.get_by_id(verify_session, enq.script.id)
            assert finished_script is not None and finished_script.status == "draft"
    finally:
        async with engine.begin() as cleanup_conn:
            # A successful run leaves script_segment_claims -> claim_verifications
            # and claim_citations -> source_chunks rows, both ON DELETE RESTRICT -
            # deleted explicitly first so the project's cascade delete below has
            # nothing left restricting it.
            claim_ids = select(claim_verifications_table.c.id).where(
                claim_verifications_table.c.project_id == project.id
            )
            await cleanup_conn.execute(
                script_segment_claims_table.delete().where(
                    script_segment_claims_table.c.claim_verification_id.in_(claim_ids)
                )
            )
            await cleanup_conn.execute(
                claim_citations_table.delete().where(
                    claim_citations_table.c.claim_verification_id.in_(claim_ids)
                )
            )
            await cleanup_conn.execute(
                projects_table.delete().where(projects_table.c.id == project.id)
            )
            await cleanup_conn.execute(users_table.delete().where(users_table.c.id == owner.id))
        await dispose_engine()
        session_module._sessionmaker = None


# --- token usage is recorded by kind of call, including on failure --------------------


async def test_a_generation_records_its_usage_by_kind_with_the_correction_pass_apart(
    db_session, monkeypatch
):
    monkeypatch.setenv("MAX_SCRIPT_CORRECTION_ATTEMPTS", "1")
    get_settings.cache_clear()
    try:
        _owner, _project, topic, claim, enq = await _enqueued(db_session)
        llm, _outline, _section = _mock_llm(
            monkeypatch,
            sections_by_topic={
                topic.id: [
                    [_on_target([str(claim.id)], duration_ms=10_000)],  # far off target
                    [_on_target([str(claim.id)], duration_ms=300_000)],  # the correction
                ]
            },
        )

        job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

        usage = job.result["usage"]
        assert job.status == "succeeded" and job.result["correction_applied"] is True
        assert sorted(usage["by_call"]) == ["script_correction", "script_outline", "script_section"]
        for kind in usage["by_call"]:
            assert usage["by_call"][kind]["calls"] == 1
            assert (
                usage["by_call"][kind]["input_tokens"],
                usage["by_call"][kind]["output_tokens"],
            ) == (10, 10)
        assert usage["total"]["calls"] == 3 and usage["total"]["input_tokens"] == 30
        # The flat totals the result always had are the same numbers.
        assert (job.result["input_tokens"], job.result["output_tokens"]) == (30, 30)
    finally:
        get_settings.cache_clear()


async def test_a_failed_generation_still_records_what_its_calls_cost(db_session, monkeypatch):
    """A response that was received, paid for and then rejected is counted, and so are the calls
    that succeeded before it: a run that fails on its second call still cost two."""
    _owner, _project, topic, claim, enq = await _enqueued(db_session)
    llm, _outline, _section = _simple_llm(
        monkeypatch, segments_by_topic={topic.id: [_on_target([str(claim.id)])]}
    )
    monkeypatch.setattr(
        llm,
        "compose_script_section",
        AsyncMock(
            side_effect=LLMResponseError(
                "The section was cut off at the output limit.",
                usage=TokenUsage(input_tokens=7, output_tokens=3),
            )
        ),
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "failed"
    usage = job.result["usage"]
    assert usage["by_call"]["script_outline"]["input_tokens"] == 10
    assert usage["by_call"]["script_section"]["calls"] == 1
    assert (
        usage["by_call"]["script_section"]["input_tokens"],
        usage["by_call"]["script_section"]["output_tokens"],
    ) == (7, 3)
    assert usage["total"]["input_tokens"] == 17


async def test_a_failure_before_any_answer_records_no_usage_rather_than_a_guess(
    db_session, monkeypatch
):
    _owner, _project, _topic, _claim, enq = await _enqueued(db_session)
    llm = _llm_raising_on_outline(monkeypatch, leaky_anthropic(500))

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "failed"
    assert job.result["usage"]["total"]["calls"] == 0
    assert job.result["usage"]["total"]["input_tokens"] is None  # unavailable, not zero
    assert job.result["usage"]["by_call"] == {}


# --- script grounding: a segment may not add a figure or a name the claims lack ---------------


async def test_a_section_that_adds_a_place_the_claims_never_mention_fails_the_generation(
    db_session, monkeypatch
):
    _owner, _project, topic, claim, enq = await _enqueued(
        db_session, claim_text="Plants convert light to energy."
    )
    llm, _outline, _section = _simple_llm(
        monkeypatch,
        segments_by_topic={
            topic.id: [
                _segment(
                    "Picture a farm near Mumbai, where plants convert light to energy.",
                    [str(claim.id)],
                )
            ]
        },
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "failed"
    assert "'Mumbai'" in job.error and "nothing unsupported may reach the script" in job.error
    script = await scripts_repo.get_by_id(db_session, enq.script.id)
    assert script.status == "failed"
    assert (
        await scripts_repo.list_segments(
            db_session, project_id=enq.script.project_id, script_id=enq.script.id
        )
        == []
    )  # nothing was attached


async def test_ordinary_teaching_language_is_not_mistaken_for_an_added_fact(
    db_session, monkeypatch
):
    _owner, _project, topic, claim, enq = await _enqueued(
        db_session, claim_text="Plants convert light to energy."
    )
    llm, _outline, _section = _simple_llm(
        monkeypatch,
        segments_by_topic={
            topic.id: [
                _segment(
                    "First, imagine a leaf as a tiny kitchen. One thing to remember is that "
                    "plants convert light to energy every day.",
                    [str(claim.id)],
                )
            ]
        },
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded"


async def test_run_refuses_a_section_that_cites_no_claim_at_all(db_session, monkeypatch):
    """A topic explained only by hand-offs is in the script by name and absent in substance: the
    writer is asked to rewrite it, and a section that still cites nothing fails the generation."""
    _owner, project, topic, claim, enq = await _enqueued(db_session)
    llm, _outline_mock, _section_mock = _mock_llm(
        monkeypatch,
        sections_by_topic={topic.id: [[_segment("A short hand-off only.", [], connective=True)]]},
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "failed"
    assert "cites none of its verified claims" in (job.error or "")
    assert any("No passage of this section cites a claim" in c for c in llm.received_corrections)
    assert (
        await scripts_repo.list_segments(
            db_session, project_id=enq.job.project_id, script_id=enq.script.id
        )
        == []
    )


async def test_run_accepts_a_hollow_section_that_is_rewritten_with_cited_content(
    db_session, monkeypatch
):
    _owner, project, topic, claim, enq = await _enqueued(db_session)
    llm, _outline_mock, _section_mock = _mock_llm(
        monkeypatch,
        sections_by_topic={
            topic.id: [
                [_segment("A short hand-off only.", [], connective=True)],
                [
                    _segment("A short hand-off only.", [], connective=True),
                    _segment("A properly cited fact.", [str(claim.id)]),
                ],
            ]
        },
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded"
    [retry] = job.result["section_retries"]
    assert retry["hollow"] is True and retry["rejected_passages"] == 0


async def test_run_keeps_the_first_draft_when_the_correction_pass_cannot_pass_the_checks(
    db_session, monkeypatch
):
    """The length-correction pass is optional tuning. If its rewrite breaks a script rule even after
    its own rewrites, it is not used - but it does not cost the valid first draft either."""
    monkeypatch.setenv("MAX_SCRIPT_CORRECTION_ATTEMPTS", "1")
    get_settings.cache_clear()
    try:
        _owner, project, topic, claim, enq = await _enqueued(db_session)
        llm, _outline_mock, _section_mock = _mock_llm(
            monkeypatch,
            sections_by_topic={
                topic.id: [
                    # first draft: valid, but far too short, so a correction pass runs
                    [_on_target([str(claim.id)], duration_ms=10_000)],
                    # the correction pass and its rewrites: an uncited, non-connective passage
                    [_segment("An unsourced assertion with no citation.", [])],
                ]
            },
        )

        job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

        assert job.status == "succeeded"
        assert job.result["correction_applied"] is False
        assert "cites none of its verified claims" in job.result["correction_failed"]
        script = await scripts_repo.get_by_id(db_session, enq.script.id)
        assert script.estimated_duration_ms == 10_000  # the first draft, as written
        texts = [
            segment.narration_text
            for segment in await scripts_repo.list_segments(
                db_session, project_id=enq.job.project_id, script_id=enq.script.id
            )
        ]
        assert "unsourced" not in " ".join(texts)
    finally:
        get_settings.cache_clear()


async def test_a_fact_two_topics_hold_is_written_once_under_the_topic_it_fits(
    db_session, monkeypatch
):
    """Regression: neighbouring topics' claims come from the same passages, so a lesson said the
    same fact in several sections. Each fact now goes to one section - the one whose title fits."""
    owner, project, [britain, steam], claims_by_topic = await _project_with_topics(
        db_session,
        [
            (
                "Conditions in Britain",
                [
                    "Britain had large coal and iron ore deposits close to its ports.",
                    "Steam engines let factories be built anywhere, away from rivers.",
                    "A stable government and a growing banking system helped industry.",
                ],
            ),
            (
                "Steam engines",
                [
                    "Steam engines allowed factories to be built anywhere, away from rivers.",
                    "James Watt added a separate condenser, making the engine far more efficient.",
                    "Coal became the fuel of the new age.",
                ],
            ),
        ],
    )
    enq = await scripts_service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="idem-dedupe"
    )
    first = {topic.id: claims_by_topic[topic.id][0] for topic in (britain, steam)}
    llm, _outline, section = _mock_llm(
        monkeypatch,
        sections_by_topic={
            topic_id: [[_segment(claim.claim_text, [str(claim.id)])]]
            for topic_id, claim in first.items()
        },
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded"
    texts_by_topic = {
        call.args[1].topic_id: [claim.text for claim in call.args[2]]
        for call in section.await_args_list
    }
    assert not any("anywhere" in text for text in texts_by_topic[britain.id])
    assert any("anywhere" in text for text in texts_by_topic[steam.id])
    assert len(texts_by_topic[britain.id]) == 2
    assert len(texts_by_topic[steam.id]) == 3
    assert job.result["claims_deduplicated"] == 1


async def test_a_script_that_just_stops_is_given_a_fixed_closing_naming_its_topics(
    db_session, monkeypatch
):
    """Regression: the writer's own closing is a synthesis no single claim states, which the support
    check removes, so scripts ended abruptly. The fixed closing names only the accepted topics."""
    monkeypatch.setenv("SCRIPT_FIXED_CLOSING", "true")
    get_settings.cache_clear()
    _o, _p, topic, claim, enq = await _enqueued(db_session, claim_text="A fact.")
    llm, _outline, _section = _mock_llm(
        monkeypatch,
        sections_by_topic={topic.id: [[_segment("A fact.", [str(claim.id)])]]},
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded"
    assert job.result["closing_added"] is True
    segments = await scripts_repo.list_segments(
        db_session, project_id=enq.job.project_id, script_id=enq.script.id
    )
    assert segments[-1].narration_text == (
        "That brings us to the end of this lesson. We covered A topic."
    )
    claim_links = await scripts_repo.list_segment_claims(
        db_session, project_id=enq.job.project_id, script_segment_id=segments[-1].id
    )
    assert claim_links == []


async def test_a_script_that_already_ends_in_a_closing_gets_no_second_one(db_session, monkeypatch):
    monkeypatch.setenv("SCRIPT_FIXED_CLOSING", "true")
    get_settings.cache_clear()
    _o, _p, topic, claim, enq = await _enqueued(db_session, claim_text="A fact.")
    llm, _outline, _section = _mock_llm(
        monkeypatch,
        sections_by_topic={
            topic.id: [
                [
                    _segment("A fact.", [str(claim.id)]),
                    _segment("In conclusion, that is the lesson.", connective=True),
                ]
            ]
        },
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded"
    assert job.result["closing_added"] is False
    segments = await scripts_repo.list_segments(
        db_session, project_id=enq.job.project_id, script_id=enq.script.id
    )
    assert [s.narration_text for s in segments][-1] == "In conclusion, that is the lesson."
    assert sum("end of this lesson" in s.narration_text for s in segments) == 0


async def test_the_closing_can_be_switched_off(db_session, monkeypatch):
    assert get_settings().SCRIPT_FIXED_CLOSING is False  # the test fixture's pin
    _o, _p, topic, claim, enq = await _enqueued(db_session, claim_text="A fact.")
    llm, _outline, _section = _mock_llm(
        monkeypatch, sections_by_topic={topic.id: [[_segment("A fact.", [str(claim.id)])]]}
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.result["closing_added"] is False


async def test_a_closing_over_many_topics_cannot_fail_the_generation(db_session, monkeypatch):
    """Regression: with eight topics the closing said "We covered 8 topics" and the figure rule
    rejected it, failing the whole generation at the end."""
    monkeypatch.setenv("SCRIPT_FIXED_CLOSING", "true")
    monkeypatch.setenv("MIN_SECTION_DURATION_MS", "1000")
    get_settings.cache_clear()
    specs = [(f"Topic {chr(65 + n)}", [f"Fact number {chr(65 + n)} is stated."]) for n in range(8)]
    owner, project, topics, claims_by_topic = await _project_with_topics(db_session, specs)
    enq = await scripts_service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="idem-many"
    )
    llm, _outline, _section = _mock_llm(
        monkeypatch,
        sections_by_topic={
            topic.id: [
                [
                    _segment(
                        claims_by_topic[topic.id][0].claim_text,
                        [str(claims_by_topic[topic.id][0].id)],
                    )
                ]
            ]
            for topic in topics
        },
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded"
    assert job.result["closing_added"] is True
    segments = await scripts_repo.list_segments(
        db_session, project_id=enq.job.project_id, script_id=enq.script.id
    )
    assert segments[-1].narration_text.endswith("We covered everything from Topic A to Topic H.")
