"""Indexing a source in batches: what is durable when a batch fails, and how a retry works.

The real ``Embeddings`` / ``AsyncOpenAI`` pair with only ``embeddings.create`` replaced (the pattern
of ``test_evidence_service.py``), against the real database. What is held here:

* a source is embedded in several requests when its size needs it, and each batch is committed
  as soon as it is stored - a batch that fails costs only itself;
* a failed job says how far it got, and retrying it embeds only what was left: no chunk is
  embedded twice, no second copy of anything is created, the job's identity is unchanged;
* a retry cannot be started while the job is queued, running or done;
* one project's indexing never touches another project's chunks.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from openai import AsyncOpenAI
from openai.types import CreateEmbeddingResponse, Embedding
from openai.types.create_embedding_response import Usage

from edutoon.core.errors import ConflictError, NotFoundError
from edutoon.domain.embedding_batches import EMBEDDING_DIMENSIONS, BatchLimits
from edutoon.providers.embeddings import Embeddings
from edutoon.providers.retry import RetryPolicy
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.source_chunks import NewSourceChunk
from edutoon.services import evidence as evidence_service
from tests.leaky_errors import assert_clean, leaky_openai

# Stands in for an external provider (OpenAI): these tests prove how our code handles a response,
# never how a real provider behaves. See "Provider-mocked tests" in apps/api/README.md.
pytestmark = pytest.mark.provider_mocked

MODEL = "text-embedding-3-small"


class RecordingCreate:
    """A stand-in for ``client.embeddings.create`` that records the texts of every request and
    can fail chosen calls (1 = the first). Each vector has the database's real width."""

    def __init__(self, fail_at: dict[int, BaseException] | None = None, width: int = 1536) -> None:
        self.requests: list[list[str]] = []
        self.fail_at = dict(fail_at or {})
        self.width = width

    async def __call__(self, *, input: list[str], model: str, **_kwargs: Any):  # noqa: A002
        self.requests.append(list(input))
        if len(self.requests) in self.fail_at:
            raise self.fail_at.pop(len(self.requests))
        return CreateEmbeddingResponse(
            object="list",
            model=model,
            usage=Usage(prompt_tokens=0, total_tokens=0),
            data=[
                Embedding(object="embedding", index=i, embedding=[0.5] * self.width)
                for i in range(len(input))
            ],
        )

    @property
    def texts_sent(self) -> list[str]:
        return [text for request in self.requests for text in request]


def _embeddings(monkeypatch: Any, create: RecordingCreate, *, max_inputs: int = 2) -> Embeddings:
    client = AsyncOpenAI(api_key="sk-test-not-a-real-key")
    monkeypatch.setattr(client.embeddings, "create", create)

    async def _no_sleep(_seconds: float) -> None:
        return None

    return Embeddings(
        client,
        model=MODEL,
        limits=BatchLimits(max_inputs=max_inputs),
        retry=RetryPolicy(max_attempts=2, base_delay_seconds=0, max_delay_seconds=0),
        sleep=_no_sleep,
    )


async def _make_source(db_session: Any, num_chunks: int, *, project: Any = None):
    if project is None:
        owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
        project = await projects_repo.create(
            db_session, owner_id=owner.id, title="P", source_type="pdf"
        )
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
                source_id=source.id,
                project_id=project.id,
                chunk_index=i,
                content=f"chunk {i} of {source.id}",
            )
            for i in range(num_chunks)
        ],
    )
    return project, source, chunks


async def _missing(db_session: Any, source: Any) -> list[int]:
    return [
        chunk.chunk_index
        for chunk in await source_chunks_repo.list_missing_embedding_by_source(
            db_session, source.id
        )
    ]


async def _queue(db_session: Any, project: Any, source: Any):
    return await evidence_service.enqueue_indexing_job(
        db_session, project_id=project.id, source_id=source.id
    )


# --- batching ---------------------------------------------------------------------------------


async def test_a_source_larger_than_one_request_is_embedded_in_several(db_session, monkeypatch):
    _, source, chunks = await _make_source(db_session, 5)
    create = RecordingCreate()

    embedded = await evidence_service.index_source(
        db_session, _embeddings(monkeypatch, create), source_id=source.id
    )

    assert embedded == 5
    assert [len(request) for request in create.requests] == [2, 2, 1]
    assert create.texts_sent == [chunk.content for chunk in chunks]  # document order, each once
    assert await _missing(db_session, source) == []


async def test_a_small_source_is_a_single_request(db_session, monkeypatch):
    _, source, _ = await _make_source(db_session, 2)
    create = RecordingCreate()

    await evidence_service.index_source(
        db_session, _embeddings(monkeypatch, create, max_inputs=2048), source_id=source.id
    )

    assert len(create.requests) == 1


async def test_the_stored_vector_keeps_the_column_width_and_the_model(db_session, monkeypatch):
    _, source, chunks = await _make_source(db_session, 1)
    await evidence_service.index_source(
        db_session, _embeddings(monkeypatch, RecordingCreate()), source_id=source.id
    )

    stored = await source_chunks_repo.get_by_id(db_session, chunks[0].id)
    assert stored is not None and stored.embedding_model == MODEL
    assert EMBEDDING_DIMENSIONS == 1536


async def test_progress_is_recorded_batch_by_batch(db_session, monkeypatch):
    _, source, _ = await _make_source(db_session, 5)
    progress = evidence_service.IndexProgress()

    await evidence_service.index_source(
        db_session,
        _embeddings(monkeypatch, RecordingCreate()),
        source_id=source.id,
        progress=progress,
    )

    assert (progress.total, progress.embedded, progress.remaining) == (5, 5, 0)


# --- a failed batch ---------------------------------------------------------------------------


async def test_a_failed_batch_keeps_every_earlier_batch_and_writes_nothing_of_itself(
    db_session, monkeypatch
):
    project, source, _ = await _make_source(db_session, 5)
    job = await _queue(db_session, project, source)
    create = RecordingCreate(fail_at={2: leaky_openai(400)})  # a permanent failure, 2nd request

    completed = await evidence_service.run_build_evidence_job(
        db_session, _embeddings(monkeypatch, create), job_id=job.id
    )

    assert completed.status == "failed"
    assert_clean(completed.error)  # the provider's text never reaches the owner
    assert completed.result == {"chunks_embedded": 2, "chunks_remaining": 3}
    assert await _missing(db_session, source) == [2, 3, 4]  # batch 1 kept; batch 2 wrote nothing


async def test_a_transient_failure_inside_a_batch_is_retried_and_the_job_succeeds(
    db_session, monkeypatch
):
    project, source, _ = await _make_source(db_session, 3)
    job = await _queue(db_session, project, source)
    create = RecordingCreate(fail_at={2: leaky_openai(503)})

    completed = await evidence_service.run_build_evidence_job(
        db_session, _embeddings(monkeypatch, create), job_id=job.id
    )

    assert completed.status == "succeeded"
    assert completed.result == {"chunks_embedded": 3}
    assert len(create.requests) == 3  # batch 1, batch 2 (failed), batch 2 again
    assert await _missing(db_session, source) == []


async def test_vectors_of_the_wrong_width_are_refused_before_anything_is_stored(
    db_session, monkeypatch
):
    project, source, _ = await _make_source(db_session, 2)
    job = await _queue(db_session, project, source)

    completed = await evidence_service.run_build_evidence_job(
        db_session, _embeddings(monkeypatch, RecordingCreate(width=3)), job_id=job.id
    )

    assert completed.status == "failed"
    assert completed.result == {"chunks_embedded": 0, "chunks_remaining": 2}
    assert await _missing(db_session, source) == [0, 1]


# --- retrying a failed job ----------------------------------------------------------------------


async def test_a_failed_job_can_be_retried_and_only_the_missing_chunks_are_embedded(
    db_session, monkeypatch
):
    project, source, chunks = await _make_source(db_session, 5)
    job = await _queue(db_session, project, source)
    first = RecordingCreate(fail_at={2: leaky_openai(400)})
    failed = await evidence_service.run_build_evidence_job(
        db_session, _embeddings(monkeypatch, first), job_id=job.id
    )
    assert failed.status == "failed"

    requeued = await evidence_service.retry_indexing_job(
        db_session, project_id=project.id, source_id=source.id
    )
    second = RecordingCreate()
    done = await evidence_service.run_build_evidence_job(
        db_session, _embeddings(monkeypatch, second), job_id=job.id
    )

    assert requeued.id == job.id  # the same job: still one indexing job per source
    assert (requeued.status, requeued.attempts, requeued.error, requeued.result) == (
        "queued",
        0,
        None,
        None,
    )
    assert done.status == "succeeded" and done.result == {"chunks_embedded": 3}
    # Nothing was embedded twice: the retry sent exactly the three chunks that were missing.
    assert second.texts_sent == [chunk.content for chunk in chunks[2:]]
    assert await _missing(db_session, source) == []


async def test_a_retry_never_creates_a_second_indexing_job_or_duplicate_chunks(
    db_session, monkeypatch
):
    project, source, _ = await _make_source(db_session, 3)
    job = await _queue(db_session, project, source)
    await evidence_service.run_build_evidence_job(
        db_session,
        _embeddings(monkeypatch, RecordingCreate(fail_at={1: leaky_openai(400)})),
        job_id=job.id,
    )
    await evidence_service.retry_indexing_job(
        db_session, project_id=project.id, source_id=source.id
    )
    await evidence_service.run_build_evidence_job(
        db_session, _embeddings(monkeypatch, RecordingCreate()), job_id=job.id
    )

    page = await jobs_repo.list_by_project(db_session, project.id)
    assert [j.id for j in page.items] == [job.id]
    chunks = await source_chunks_repo.list_by_source_in_order(db_session, source.id)
    assert len(chunks) == 3  # a vector lives on the chunk itself: there is nothing to duplicate


async def test_a_job_that_failed_three_times_can_still_be_retried(db_session, monkeypatch):
    """``ck_jobs_attempts_within_max``: a job that used every attempt would crash on its next run
    unless a retry starts a fresh series."""
    project, source, _ = await _make_source(db_session, 1)
    job = await _queue(db_session, project, source)
    for _ in range(3):
        failed = await evidence_service.run_build_evidence_job(
            db_session,
            _embeddings(monkeypatch, RecordingCreate(fail_at={1: leaky_openai(400)})),
            job_id=job.id,
        )
        assert failed.status == "failed"
    assert failed.attempts == 3

    await evidence_service.retry_indexing_job(
        db_session, project_id=project.id, source_id=source.id
    )
    done = await evidence_service.run_build_evidence_job(
        db_session, _embeddings(monkeypatch, RecordingCreate()), job_id=job.id
    )

    assert done.status == "succeeded" and done.attempts == 1


async def test_retrying_twice_in_a_row_is_a_conflict_the_second_time(db_session, monkeypatch):
    project, source, _ = await _make_source(db_session, 1)
    job = await _queue(db_session, project, source)
    await evidence_service.run_build_evidence_job(
        db_session,
        _embeddings(monkeypatch, RecordingCreate(fail_at={1: leaky_openai(400)})),
        job_id=job.id,
    )
    await evidence_service.retry_indexing_job(
        db_session, project_id=project.id, source_id=source.id
    )

    with pytest.raises(ConflictError, match="already being indexed"):
        await evidence_service.retry_indexing_job(
            db_session, project_id=project.id, source_id=source.id
        )


async def test_a_queued_job_cannot_be_retried(db_session):
    project, source, _ = await _make_source(db_session, 1)
    await _queue(db_session, project, source)

    with pytest.raises(ConflictError, match="already being indexed"):
        await evidence_service.retry_indexing_job(
            db_session, project_id=project.id, source_id=source.id
        )


async def test_a_running_job_cannot_be_retried(db_session):
    project, source, _ = await _make_source(db_session, 1)
    job = await _queue(db_session, project, source)
    await jobs_repo.update(db_session, job_id=job.id, status="running")

    with pytest.raises(ConflictError, match="already being indexed"):
        await evidence_service.retry_indexing_job(
            db_session, project_id=project.id, source_id=source.id
        )


async def test_a_succeeded_job_cannot_be_retried(db_session, monkeypatch):
    project, source, _ = await _make_source(db_session, 1)
    job = await _queue(db_session, project, source)
    await evidence_service.run_build_evidence_job(
        db_session, _embeddings(monkeypatch, RecordingCreate()), job_id=job.id
    )

    with pytest.raises(ConflictError, match="already indexed"):
        await evidence_service.retry_indexing_job(
            db_session, project_id=project.id, source_id=source.id
        )


async def test_a_source_that_was_never_queued_has_nothing_to_retry(db_session):
    project, source, _ = await _make_source(db_session, 1)

    with pytest.raises(NotFoundError):
        await evidence_service.retry_indexing_job(
            db_session, project_id=project.id, source_id=source.id
        )


async def test_a_retry_is_scoped_to_its_project(db_session, monkeypatch):
    project, source, _ = await _make_source(db_session, 1)
    other_project, _, _ = await _make_source(db_session, 1)
    job = await _queue(db_session, project, source)
    await evidence_service.run_build_evidence_job(
        db_session,
        _embeddings(monkeypatch, RecordingCreate(fail_at={1: leaky_openai(400)})),
        job_id=job.id,
    )

    with pytest.raises(NotFoundError):  # the job is not found under another project's id
        await evidence_service.retry_indexing_job(
            db_session, project_id=other_project.id, source_id=source.id
        )


# --- isolation --------------------------------------------------------------------------------


async def test_indexing_one_projects_source_never_touches_another_projects_chunks(
    db_session, monkeypatch
):
    project_a, source_a, _ = await _make_source(db_session, 3)
    project_b, source_b, _ = await _make_source(db_session, 3)
    create = RecordingCreate()

    await evidence_service.index_source(
        db_session, _embeddings(monkeypatch, create), source_id=source_a.id
    )

    assert await _missing(db_session, source_a) == []
    assert await _missing(db_session, source_b) == [0, 1, 2]  # untouched
    assert all(str(source_a.id) in text for text in create.texts_sent)
    # And retrieval is still scoped: project B has nothing indexed to find.
    found_in_b = await source_chunks_repo.search_by_similarity(
        db_session, project_id=project_b.id, query_embedding=[0.5] * 1536
    )
    assert found_in_b == []
    found_in_a = await source_chunks_repo.search_by_similarity(
        db_session, project_id=project_a.id, query_embedding=[0.5] * 1536
    )
    assert len(found_in_a) == 3
