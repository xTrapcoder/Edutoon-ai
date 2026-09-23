"""``worker`` - the background job poll/claim/dispatch loop (Phase C/D).

``process_one_job`` needs to open more than one session per call (a short
claim transaction, then a separate one for the handler), so these tests
give it a real ``async_sessionmaker`` rather than the single ``db_session``
instance most other tests use directly. ``worker_sessionmaker`` below binds
that sessionmaker to ``db_session``'s own connection, in the same
``create_savepoint`` mode the ``db_session`` fixture itself uses - every
session `process_one_job` opens shares the one outer transaction that the
``db_session`` fixture rolls back at the end, so nothing written by these
tests persists past them, exactly like every other test in this suite.

The real cross-connection concurrency test at the bottom
(``test_claim_next_is_never_won_by_two_concurrent_claimants``) is the one
exception: it needs two genuinely independent connections to exercise
``FOR UPDATE SKIP LOCKED`` for real, so it follows ``test_session_commit.py``'s
pattern instead - the real engine, real commits, manual cleanup.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from anthropic import AsyncAnthropic
from anthropic.types import Message, ToolUseBlock
from anthropic.types import Usage as AnthropicUsage
from openai import AsyncOpenAI
from openai.types import CreateEmbeddingResponse, Embedding
from openai.types.create_embedding_response import Usage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import edutoon.db.session as session_module
from edutoon.db.database import dispose_engine, get_engine
from edutoon.db.session import get_sessionmaker
from edutoon.db.tables import jobs as jobs_table
from edutoon.db.tables import projects as projects_table
from edutoon.db.tables import users as users_table
from edutoon.providers.embeddings import Embeddings
from edutoon.providers.llm import LLM
from edutoon.repositories import claim_verifications as claim_verifications_repo
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.claim_verifications import NewClaimCitation
from edutoon.repositories.source_chunks import NewSourceChunk
from edutoon.services import evidence as evidence_service
from edutoon.services import scripts as scripts_service
from edutoon.services import topic_extraction as topic_extraction_service
from edutoon.worker.dispatch import JOB_HANDLERS, WorkerProviders
from edutoon.worker.runner import process_one_job

EMBEDDING_MODEL = "text-embedding-3-small"
LLM_MODEL = "claude-sonnet-5"


@pytest.fixture
async def worker_sessionmaker(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    connection = await db_session.connection()
    return async_sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


def _fake_embeddings(
    monkeypatch: Any, vector_for: dict[str, list[float]] | None = None
) -> Embeddings:
    client = AsyncOpenAI(api_key="sk-test-not-a-real-key")

    async def _create(*, input, model, **_kwargs):  # noqa: ANN001, ARG001 - test double
        vectors = [(vector_for or {}).get(text, [0.0] * 1536) for text in input]
        return CreateEmbeddingResponse(
            object="list",
            model=model,
            usage=Usage(prompt_tokens=0, total_tokens=0),
            data=[
                Embedding(object="embedding", index=i, embedding=v) for i, v in enumerate(vectors)
            ],
        )

    monkeypatch.setattr(client.embeddings, "create", _create)
    return Embeddings(client, model=EMBEDDING_MODEL)


def _idle_llm() -> LLM:
    """An LLM client never expected to be called - for tests that only
    exercise the ``build_evidence`` handler but still need to fill
    ``WorkerProviders.llm``.
    """
    return LLM(AsyncAnthropic(api_key="sk-ant-test-not-a-real-key"), model=LLM_MODEL)


def _idle_embeddings() -> Embeddings:
    return Embeddings(AsyncOpenAI(api_key="sk-test-not-a-real-key"), model=EMBEDDING_MODEL)


def _fake_llm(monkeypatch: Any, segments: list[dict[str, Any]], *, title: str = "A title") -> LLM:
    client = AsyncAnthropic(api_key="sk-ant-test-not-a-real-key")
    message = Message(
        id="msg_1",
        content=[
            ToolUseBlock(
                type="tool_use", id="toolu_1", name="submit_script",
                input={"title": title, "segments": segments},
            )
        ],
        model=LLM_MODEL,
        role="assistant",
        type="message",
        usage=AnthropicUsage(input_tokens=10, output_tokens=20),
    )
    monkeypatch.setattr(client.messages, "create", AsyncMock(return_value=message))
    return LLM(client, model=LLM_MODEL)


def _fake_topics_llm(monkeypatch: Any, topics: list[dict[str, Any]]) -> LLM:
    client = AsyncAnthropic(api_key="sk-ant-test-not-a-real-key")
    message = Message(
        id="msg_1",
        content=[
            ToolUseBlock(
                type="tool_use", id="toolu_1", name="submit_topics", input={"topics": topics}
            )
        ],
        model=LLM_MODEL,
        role="assistant",
        type="message",
        usage=AnthropicUsage(input_tokens=11, output_tokens=22),
    )
    monkeypatch.setattr(client.messages, "create", AsyncMock(return_value=message))
    return LLM(client, model=LLM_MODEL)


async def _make_project(db_session: AsyncSession):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    return await projects_repo.create(
        db_session, owner_id=owner.id, title="P", source_type="pdf"
    )


async def _make_source_with_chunks(db_session: AsyncSession, project, num_chunks: int = 2):
    source = await uploaded_sources_repo.create(
        db_session,
        project_id=project.id,
        original_filename="notes.pdf",
        storage_bucket="edutoon-uploads",
        storage_key=f"{project.id}/{uuid4()}.pdf",
        byte_size=1024,
        checksum_sha256=uuid4().hex + uuid4().hex[:32],
    )
    await source_chunks_repo.create_many(
        db_session,
        [
            NewSourceChunk(
                source_id=source.id, project_id=project.id, chunk_index=i, content=f"chunk {i}"
            )
            for i in range(num_chunks)
        ],
    )
    return source


async def _project_with_supported_claim(db_session: AsyncSession):
    project = await _make_project(db_session)
    source = await uploaded_sources_repo.create(
        db_session,
        project_id=project.id,
        original_filename="notes.pdf",
        storage_bucket="edutoon-uploads",
        storage_key=f"{project.id}/{uuid4()}.pdf",
        byte_size=1024,
        checksum_sha256=uuid4().hex + uuid4().hex[:32],
    )
    [chunk] = await source_chunks_repo.create_many(
        db_session,
        [
            NewSourceChunk(
                source_id=source.id, project_id=project.id, chunk_index=0, content="A fact."
            )
        ],
    )
    claim = await claim_verifications_repo.create(
        db_session,
        project_id=project.id,
        claim_text="A fact.",
        status="supported",
        embedding_model=EMBEDDING_MODEL,
        llm_model=LLM_MODEL,
        citations=[NewClaimCitation(source_chunk_id=chunk.id, similarity=0.9)],
    )
    return project, claim


# --- process_one_job: empty queue -------------------------------------------------


async def test_process_one_job_returns_false_when_queue_is_empty(worker_sessionmaker):
    providers = WorkerProviders(embeddings=_idle_embeddings(), llm=_idle_llm())

    claimed_something = await process_one_job(worker_sessionmaker, providers)

    assert claimed_something is False


# --- process_one_job: build_evidence -----------------------------------------------


async def test_process_one_job_runs_the_build_evidence_handler(
    db_session, worker_sessionmaker, monkeypatch
):
    project = await _make_project(db_session)
    source = await _make_source_with_chunks(db_session, project, num_chunks=2)
    job = await evidence_service.enqueue_indexing_job(
        db_session, project_id=project.id, source_id=source.id
    )
    await db_session.commit()  # release the savepoint so the worker's own session sees it

    providers = WorkerProviders(
        embeddings=_fake_embeddings(monkeypatch), llm=_idle_llm()
    )
    claimed_something = await process_one_job(worker_sessionmaker, providers)

    assert claimed_something is True
    finished = await jobs_repo.get_by_id(db_session, job.id)
    assert finished is not None
    assert finished.status == "succeeded"
    assert finished.result == {"chunks_embedded": 2}
    assert finished.attempts == 1

    remaining = await source_chunks_repo.list_missing_embedding_by_source(db_session, source.id)
    assert remaining == []


async def test_process_one_job_marks_build_evidence_failed_on_a_provider_error(
    db_session, worker_sessionmaker
):
    project = await _make_project(db_session)
    source = await _make_source_with_chunks(db_session, project, num_chunks=1)
    job = await evidence_service.enqueue_indexing_job(
        db_session, project_id=project.id, source_id=source.id
    )
    await db_session.commit()

    client = AsyncOpenAI(api_key="sk-test-not-a-real-key")

    async def _boom(**_kwargs):
        raise RuntimeError("openai is unreachable")

    client.embeddings.create = _boom  # type: ignore[method-assign]
    providers = WorkerProviders(
        embeddings=Embeddings(client, model=EMBEDDING_MODEL), llm=_idle_llm()
    )

    claimed_something = await process_one_job(worker_sessionmaker, providers)

    assert claimed_something is True
    finished = await jobs_repo.get_by_id(db_session, job.id)
    assert finished is not None
    assert finished.status == "failed"
    assert finished.error == "openai is unreachable"


# --- process_one_job: generate_script -----------------------------------------------


async def test_process_one_job_runs_the_generate_script_handler(
    db_session, worker_sessionmaker, monkeypatch
):
    project, claim = await _project_with_supported_claim(db_session)
    enqueued = await scripts_service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=project.owner_id, idempotency_key="k"
    )
    await db_session.commit()

    llm = _fake_llm(
        monkeypatch,
        [{"narration_text": "A cited sentence.", "claim_ids": [str(claim.id)]}],
    )
    providers = WorkerProviders(embeddings=_idle_embeddings(), llm=llm)

    claimed_something = await process_one_job(worker_sessionmaker, providers)

    assert claimed_something is True
    finished = await jobs_repo.get_by_id(db_session, enqueued.job.id)
    assert finished is not None
    assert finished.status == "succeeded"
    assert finished.result["segment_count"] == 1


# --- process_one_job: extract_topics -----------------------------------------------


async def _project_with_a_user_topic(db_session: AsyncSession):
    project = await _make_project(db_session)
    seed = await project_topics_repo.create(
        db_session, project_id=project.id, title="Photosynthesis", status="accepted"
    )
    return project, seed


def test_the_worker_has_a_handler_for_each_implemented_job_kind_and_no_others():
    assert set(JOB_HANDLERS) == {"build_evidence", "generate_script", "extract_topics"}


async def test_process_one_job_runs_the_extract_topics_handler(
    db_session, worker_sessionmaker, monkeypatch
):
    project, seed = await _project_with_a_user_topic(db_session)
    enqueued = await topic_extraction_service.enqueue_topic_extraction(
        db_session, project_id=project.id, owner_id=project.owner_id, idempotency_key="k"
    )
    await db_session.commit()  # release the savepoint so the worker's own session sees it

    llm = _fake_topics_llm(
        monkeypatch,
        [
            {
                "ref": "t1",
                "title": "Photosynthesis",
                "summary": "How plants make food.",
                "keywords": ["light"],
                "evidence_chunk_ids": [],
                "seed_ref": str(seed.id),
            },
            {
                "ref": "t2",
                "title": "Light reactions",
                "parent_ref": "t1",
                "summary": "",
                "keywords": [],
                "evidence_chunk_ids": [],
            },
        ],
    )
    providers = WorkerProviders(embeddings=_idle_embeddings(), llm=llm)

    claimed_something = await process_one_job(worker_sessionmaker, providers)

    assert claimed_something is True
    finished = await jobs_repo.get_by_id(db_session, enqueued.job.id)
    assert finished is not None
    assert finished.status == "succeeded"
    assert finished.result == {
        "mode": "keywords",
        "topic_count": 2,
        "chunks_total": 0,
        "chunks_used": 0,
        "truncated": False,
        "dropped_uncited": 0,
        "replaced": False,
        "input_tokens": 11,
        "output_tokens": 22,
        "llm_model": LLM_MODEL,
    }
    stored = await project_topics_repo.list_all_by_project(db_session, project.id)
    topics = {t.title: t for t in stored}
    assert topics["Light reactions"].parent_id == seed.id
    assert topics["Light reactions"].origin == "suggested"


async def test_process_one_job_marks_extract_topics_failed_on_a_provider_error(
    db_session, worker_sessionmaker
):
    project, seed = await _project_with_a_user_topic(db_session)
    enqueued = await topic_extraction_service.enqueue_topic_extraction(
        db_session, project_id=project.id, owner_id=project.owner_id, idempotency_key="k"
    )
    await db_session.commit()

    client = AsyncAnthropic(api_key="sk-ant-test-not-a-real-key")
    client.messages.create = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("anthropic is unreachable")
    )
    providers = WorkerProviders(embeddings=_idle_embeddings(), llm=LLM(client, model=LLM_MODEL))

    claimed_something = await process_one_job(worker_sessionmaker, providers)

    assert claimed_something is True
    finished = await jobs_repo.get_by_id(db_session, enqueued.job.id)
    assert finished is not None
    assert finished.status == "failed"
    assert finished.error == "anthropic is unreachable"
    assert finished.finished_at is not None
    # The user's own topic is exactly as it was.
    stored = await project_topics_repo.list_all_by_project(db_session, project.id)
    assert [t.id for t in stored] == [seed.id]


async def test_process_one_job_marks_a_corrupt_extract_topics_payload_failed_without_crashing(
    db_session, worker_sessionmaker
):
    project = await _make_project(db_session)
    job = await jobs_repo.create(db_session, project_id=project.id, kind="extract_topics")
    await db_session.commit()

    providers = WorkerProviders(embeddings=_idle_embeddings(), llm=_idle_llm())
    claimed_something = await process_one_job(worker_sessionmaker, providers)

    assert claimed_something is True
    finished = await jobs_repo.get_by_id(db_session, job.id)
    assert finished is not None
    assert finished.status == "failed"
    assert "project_id" in finished.error
    assert finished.finished_at is not None


# --- process_one_job: unknown job kind ----------------------------------------------


async def test_process_one_job_marks_an_unknown_job_kind_failed_without_crashing(
    db_session, worker_sessionmaker
):
    # ``render_video`` is a valid ``jobs.kind`` with no handler registered - the
    # case this test is about. (It used ``extract_topics`` until that gained a
    # real handler; any still-unimplemented kind exercises the same path.)
    project = await _make_project(db_session)
    job = await jobs_repo.create(db_session, project_id=project.id, kind="render_video")
    await db_session.commit()

    providers = WorkerProviders(embeddings=_idle_embeddings(), llm=_idle_llm())
    claimed_something = await process_one_job(worker_sessionmaker, providers)

    assert claimed_something is True
    finished = await jobs_repo.get_by_id(db_session, job.id)
    assert finished is not None
    assert finished.status == "failed"
    assert "render_video" in finished.error
    assert finished.finished_at is not None


# --- real concurrency: FOR UPDATE SKIP LOCKED actually prevents a double claim -----


async def test_claim_next_is_never_won_by_two_concurrent_claimants():
    """Two independent connections racing ``claim_next`` against the same
    single queued job must never both win it - exactly the scenario
    ``FOR UPDATE SKIP LOCKED`` exists to prevent, and the one thing the
    savepoint-sharing ``worker_sessionmaker`` fixture above cannot exercise
    (it's one connection, so its statements are already serialised).
    Follows ``test_session_commit.py``'s real-engine, real-commit, manual
    cleanup pattern rather than the transactional ``db_session`` fixture.
    """
    engine = get_engine()
    sessionmaker = get_sessionmaker()

    async with sessionmaker() as setup_session:
        owner = await users_repo.create(setup_session, email=f"{uuid4()}@example.com")
        project = await projects_repo.create(
            setup_session, owner_id=owner.id, title="P", source_type="pdf"
        )
        job = await jobs_repo.create(setup_session, project_id=project.id, kind="build_evidence")
        await setup_session.commit()

    try:
        async def _claim() -> jobs_repo.JobRecord | None:
            async with sessionmaker() as session:
                claimed = await jobs_repo.claim_next(session)
                await session.commit()
                return claimed

        results = await asyncio.gather(_claim(), _claim())

        winners = [r for r in results if r is not None]
        assert len(winners) == 1
        assert winners[0].id == job.id
    finally:
        async with engine.begin() as cleanup_conn:
            await cleanup_conn.execute(jobs_table.delete().where(jobs_table.c.id == job.id))
            await cleanup_conn.execute(
                projects_table.delete().where(projects_table.c.id == project.id)
            )
            await cleanup_conn.execute(users_table.delete().where(users_table.c.id == owner.id))
        await dispose_engine()
        session_module._sessionmaker = None
