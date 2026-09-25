"""``services/scripts.py`` - Script Generation Engine service layer (SG-4).

The LLM is a real ``LLM``/``AsyncAnthropic`` pair with only
``messages.create`` monkeypatched (same pattern as ``test_llm_provider.py``
and ``test_evidence_service.py``) - never a real network call, but the
orchestration runs exactly as it would in production.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from anthropic import AsyncAnthropic
from anthropic.types import Message, ToolUseBlock
from anthropic.types import Usage as AnthropicUsage

from edutoon.core.errors import ConflictError, NotFoundError, ValidationError
from edutoon.providers import errors as provider_errors
from edutoon.providers.llm import LLM, LLMResponseError
from edutoon.repositories import claim_verifications as claim_verifications_repo
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.claim_verifications import NewClaimCitation
from edutoon.repositories.source_chunks import NewSourceChunk
from edutoon.services import audit_logs as audit_logs_service
from edutoon.services import job_errors
from edutoon.services import scripts as scripts_service
from tests.leaky_errors import assert_clean, leaky_anthropic

SCRIPT_MODEL = "claude-sonnet-5"


def _fake_llm(
    monkeypatch: Any,
    segments: list[dict[str, Any]],
    *,
    title: str = "How photosynthesis works",
    input_tokens: int = 100,
    output_tokens: int = 200,
) -> LLM:
    client = AsyncAnthropic(api_key="sk-ant-test-not-a-real-key")
    message = Message(
        id="msg_1",
        content=[
            ToolUseBlock(
                type="tool_use",
                id="toolu_1",
                name="submit_script",
                input={"title": title, "segments": segments},
            )
        ],
        model=SCRIPT_MODEL,
        role="assistant",
        type="message",
        usage=AnthropicUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )
    monkeypatch.setattr(client.messages, "create", AsyncMock(return_value=message))
    return LLM(client, model=SCRIPT_MODEL)


def _llm_raising(monkeypatch: Any, exc: Exception) -> LLM:
    llm = LLM(AsyncAnthropic(api_key="sk-ant-test-not-a-real-key"), model=SCRIPT_MODEL)
    monkeypatch.setattr(llm, "compose_script", AsyncMock(side_effect=exc))
    return llm


async def _make_project(db_session, *, title: str = "Photosynthesis"):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title=title, source_type="pdf"
    )
    return owner, project


async def _add_supported_claims(db_session, project, texts: list[str]):
    source = await uploaded_sources_repo.create(
        db_session,
        project_id=project.id,
        original_filename="notes.pdf",
        storage_bucket="edutoon-uploads",
        storage_key=f"{project.id}/{uuid4()}.pdf",
        byte_size=1024,
        checksum_sha256=uuid4().hex + uuid4().hex[:32],
    )
    chunks = await source_chunks_repo.create_many(
        db_session,
        [
            NewSourceChunk(
                source_id=source.id, project_id=project.id, chunk_index=i, content=text
            )
            for i, text in enumerate(texts)
        ],
    )
    verifications = []
    for text, chunk in zip(texts, chunks, strict=True):
        verifications.append(
            await claim_verifications_repo.create(
                db_session,
                project_id=project.id,
                claim_text=text,
                status="supported",
                embedding_model="text-embedding-3-small",
                llm_model=SCRIPT_MODEL,
                citations=[NewClaimCitation(source_chunk_id=chunk.id, similarity=0.9)],
            )
        )
    return verifications


async def _enqueued(db_session, key: str = "idem-1"):
    owner, project = await _make_project(db_session)
    claims = await _add_supported_claims(
        db_session, project, ["Plants convert light to energy.", "Chlorophyll absorbs light."]
    )
    result = await scripts_service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key=key
    )
    return owner, project, claims, result


# --- enqueue_script_generation --------------------------------------------------


async def test_enqueue_creates_a_pending_script_and_a_queued_job(db_session):
    owner, project, _claims, result = await _enqueued(db_session)

    assert result.replayed is False
    assert result.script.status == "pending"
    assert result.script.project_id == project.id
    assert result.job.kind == "generate_script"
    assert result.job.status == "queued"
    assert result.job.payload == {
        "script_id": str(result.script.id),
        "project_id": str(project.id),
    }


async def test_enqueue_writes_an_audit_log(db_session):
    _owner, _project, _claims, result = await _enqueued(db_session)

    logs = await audit_logs_service.list_audit_logs_for_entity(
        db_session, entity_type="script", entity_id=result.script.id
    )
    assert [log.action for log in logs.items] == ["script.generation_enqueued"]
    assert logs.items[0].context["job_id"] == str(result.job.id)


async def test_enqueue_requires_at_least_one_supported_claim(db_session):
    owner, project = await _make_project(db_session)

    with pytest.raises(ValidationError):
        await scripts_service.enqueue_script_generation(
            db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
        )


async def test_enqueue_ignores_unsupported_claims(db_session):
    owner, project = await _make_project(db_session)
    # A non-"supported" verification must not count towards the requirement.
    await claim_verifications_repo.create(
        db_session,
        project_id=project.id,
        claim_text="Not fully backed.",
        status="partially_supported",
        embedding_model="text-embedding-3-small",
        llm_model=SCRIPT_MODEL,
    )

    with pytest.raises(ValidationError):
        await scripts_service.enqueue_script_generation(
            db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
        )


async def test_enqueue_supersedes_the_current_active_script(db_session):
    owner, project = await _make_project(db_session)
    await _add_supported_claims(db_session, project, ["A fact."])
    stale = await scripts_repo.create(db_session, project_id=project.id, llm_model=SCRIPT_MODEL)

    result = await scripts_service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
    )

    assert (await scripts_repo.get_by_id(db_session, stale.id)).status == "superseded"
    assert (await scripts_repo.get_active(db_session, project.id)).id == result.script.id


async def test_enqueue_is_idempotent_on_the_key(db_session):
    owner, project = await _make_project(db_session)
    await _add_supported_claims(db_session, project, ["A fact."])

    first = await scripts_service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="same"
    )
    second = await scripts_service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="same"
    )

    assert second.replayed is True
    assert second.script.id == first.script.id
    assert second.job.id == first.job.id
    page = await jobs_repo.list_by_project(db_session, project.id)
    assert len(page.items) == 1


async def test_enqueue_rejects_a_project_the_caller_does_not_own(db_session):
    _owner, project = await _make_project(db_session)
    await _add_supported_claims(db_session, project, ["A fact."])
    other = await users_repo.create(db_session, email=f"{uuid4()}@example.com")

    with pytest.raises(NotFoundError):
        await scripts_service.enqueue_script_generation(
            db_session, project_id=project.id, owner_id=other.id, idempotency_key="k"
        )


async def test_enqueue_records_the_configured_script_model(db_session):
    owner, project = await _make_project(db_session)
    await _add_supported_claims(db_session, project, ["A fact."])

    result = await scripts_service.enqueue_script_generation(
        db_session,
        project_id=project.id,
        owner_id=owner.id,
        idempotency_key="k",
        llm_model="claude-opus-5",
    )

    assert result.script.llm_model == "claude-opus-5"


# --- run_generate_script_job ---------------------------------------------------


async def test_run_produces_a_draft_from_the_supported_claims(db_session, monkeypatch):
    _owner, project, claims, enq = await _enqueued(db_session)
    llm = _fake_llm(
        monkeypatch,
        [
            {"narration_text": "An opening line.", "claim_ids": [], "connective": True},
            {
                "narration_text": "Plants turn light into chemical energy for food.",
                "claim_ids": [str(claims[0].id)],
            },
            {
                "narration_text": "The pigment chlorophyll is what captures that light.",
                "claim_ids": [str(claims[1].id)],
            },
        ],
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded"
    assert job.result["segment_count"] == 3
    assert job.result["input_tokens"] == 100
    assert job.result["output_tokens"] == 200
    assert job.attempts == 1

    script = await scripts_repo.get_by_id(db_session, enq.script.id)
    assert script.status == "draft"
    assert script.estimated_duration_ms is not None and script.estimated_duration_ms > 0

    segments = await scripts_repo.list_segments(
        db_session, project_id=project.id, script_id=script.id
    )
    assert [s.segment_index for s in segments] == [0, 1, 2]
    first_links = await scripts_repo.list_segment_claims(
        db_session, project_id=project.id, script_segment_id=segments[0].id
    )
    assert first_links == []
    second_links = await scripts_repo.list_segment_claims(
        db_session, project_id=project.id, script_segment_id=segments[1].id
    )
    assert [c.claim_verification_id for c in second_links] == [claims[0].id]


async def test_run_marks_job_and_script_failed_on_a_provider_error(db_session, monkeypatch):
    _owner, _project, _claims, enq = await _enqueued(db_session)
    llm = _llm_raising(monkeypatch, LLMResponseError("model returned junk"))

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "failed"
    assert "junk" in job.error
    script = await scripts_repo.get_by_id(db_session, enq.script.id)
    assert script.status == "failed"


async def test_run_never_raises_on_a_value_error_from_the_provider(db_session, monkeypatch):
    _owner, _project, _claims, enq = await _enqueued(db_session)
    llm = _llm_raising(monkeypatch, ValueError("too many claims"))

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "failed"


async def test_run_marks_failed_when_a_segment_is_unsourced(db_session, monkeypatch):
    _owner, _project, claims, enq = await _enqueued(db_session)
    llm = _fake_llm(
        monkeypatch,
        [
            {"narration_text": "A properly cited sentence.", "claim_ids": [str(claims[0].id)]},
            {"narration_text": "An unsourced assertion with no citation.", "claim_ids": []},
        ],
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "failed"
    script = await scripts_repo.get_by_id(db_session, enq.script.id)
    assert script.status == "failed"
    # nothing partial persisted
    assert (
        await scripts_repo.list_segments(
            db_session, project_id=enq.job.project_id, script_id=enq.script.id
        )
        == []
    )


async def test_run_raises_not_found_for_a_missing_job(db_session, monkeypatch):
    llm = _fake_llm(monkeypatch, [])

    with pytest.raises(NotFoundError):
        await scripts_service.run_generate_script_job(db_session, llm, job_id=uuid4())


async def test_run_fails_cleanly_if_the_pending_script_was_superseded(db_session, monkeypatch):
    _owner, project, claims, enq = await _enqueued(db_session)
    # Something else superseded the pending script before the worker got to it.
    await scripts_repo.supersede_active(db_session, project_id=project.id)
    llm = _fake_llm(
        monkeypatch,
        [{"narration_text": "A cited sentence here.", "claim_ids": [str(claims[0].id)]}],
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "failed"


async def test_run_increments_attempts_across_runs(db_session, monkeypatch):
    _owner, _project, claims, enq = await _enqueued(db_session)
    llm = _llm_raising(monkeypatch, LLMResponseError("boom"))

    first = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)
    second = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert first.attempts == 1
    assert second.attempts == 2


# --- assemble_script_detail --------------------------------------------------


async def _drafted(db_session, monkeypatch):
    owner, project, claims, enq = await _enqueued(db_session)
    llm = _fake_llm(
        monkeypatch,
        [
            {"narration_text": "Intro without a claim.", "claim_ids": [], "connective": True},
            {
                "narration_text": "Plants make food from light energy.",
                "claim_ids": [str(claims[0].id)],
            },
        ],
    )
    await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)
    return owner, project, claims, enq.script.id


async def test_assemble_returns_the_script_with_segments_and_cited_claims(db_session, monkeypatch):
    owner, project, claims, script_id = await _drafted(db_session, monkeypatch)

    detail = await scripts_service.assemble_script_detail(
        db_session, project_id=project.id, owner_id=owner.id, script_id=script_id
    )

    assert detail.script.id == script_id
    assert detail.script.status == "draft"
    assert [s.segment.segment_index for s in detail.segments] == [0, 1]
    assert detail.segments[0].claims == ()
    assert [c.id for c in detail.segments[1].claims] == [claims[0].id]
    assert detail.segments[1].claims[0].claim_text == "Plants convert light to energy."


async def test_assemble_rejects_a_project_the_caller_does_not_own(db_session, monkeypatch):
    _owner, project, _claims, script_id = await _drafted(db_session, monkeypatch)
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
    owner, project, _claims, script_id = await _drafted(db_session, monkeypatch)

    approved = await scripts_service.approve_script(
        db_session, project_id=project.id, owner_id=owner.id, script_id=script_id
    )

    assert approved.status == "approved"
    logs = await audit_logs_service.list_audit_logs_for_entity(
        db_session, entity_type="script", entity_id=script_id
    )
    assert "script.approved" in {log.action for log in logs.items}


async def test_approve_rejects_a_script_that_is_not_a_draft(db_session):
    owner, project = await _make_project(db_session)
    await _add_supported_claims(db_session, project, ["A fact."])
    result = await scripts_service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
    )

    # still 'pending' - never drafted
    with pytest.raises(ConflictError):
        await scripts_service.approve_script(
            db_session, project_id=project.id, owner_id=owner.id, script_id=result.script.id
        )


async def test_approve_rejects_a_project_the_caller_does_not_own(db_session, monkeypatch):
    _owner, project, _claims, script_id = await _drafted(db_session, monkeypatch)
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
    owner, project, _claims, script_id = await _drafted(db_session, monkeypatch)
    await scripts_service.approve_script(
        db_session, project_id=project.id, owner_id=owner.id, script_id=script_id
    )

    with pytest.raises(ConflictError):
        await scripts_service.approve_script(
            db_session, project_id=project.id, owner_id=owner.id, script_id=script_id
        )


async def test_a_provider_failure_tells_the_owner_a_fixed_sentence_and_nothing_else(
    db_session, monkeypatch
):
    _owner, _project, _claims, enq = await _enqueued(db_session)
    llm = _llm_raising(monkeypatch, leaky_anthropic(429))

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "failed" and job.error == provider_errors.RATE_LIMITED
    assert_clean(job.error)
    script = await scripts_repo.get_by_id(db_session, enq.script.id)
    assert script.status == "failed"


async def test_an_unexpected_failure_while_composing_a_script_is_generic(
    db_session, monkeypatch
):
    _owner, _project, _claims, enq = await _enqueued(db_session)
    llm = _llm_raising(monkeypatch, RuntimeError("password=hunter2 at /srv/app/worker.py"))

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "failed" and job.error == job_errors.GENERIC_ERROR
    assert_clean(job.error)
