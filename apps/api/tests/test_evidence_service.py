"""``services/evidence.py`` - Phase B: indexing orchestration.

Uses a real ``Embeddings`` wrapping a real ``AsyncOpenAI`` client with only
``embeddings.create`` monkeypatched (same pattern as
``test_embeddings_provider.py``) - never a real network call, but the
orchestration is exercised exactly as it runs in production.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from openai import AsyncOpenAI
from openai.types import CreateEmbeddingResponse, Embedding
from openai.types.create_embedding_response import Usage

from edutoon.core.errors import ConflictError, NotFoundError
from edutoon.providers.embeddings import Embeddings
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.source_chunks import NewSourceChunk
from edutoon.services import evidence as evidence_service

MODEL = "text-embedding-3-small"


def _fake_embeddings(monkeypatch, vector_for: dict[str, list[float]] | None = None) -> Embeddings:
    """A real Embeddings/AsyncOpenAI pair whose ``create`` call returns a
    deterministic vector per input string (defaulting to an all-zero vector
    of the right dimensionality when ``vector_for`` doesn't cover it).
    """
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
    return Embeddings(client, model=MODEL)


async def _make_source(db_session, num_chunks: int = 2):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="P", source_type="pdf"
    )
    source = await uploaded_sources_repo.create(
        db_session,
        project_id=project.id,
        original_filename="notes.pdf",
        storage_bucket="edutoon-uploads",
        storage_key=f"{project.id}/notes.pdf",
        byte_size=1024,
        checksum_sha256=uuid4().hex + uuid4().hex[:32],
    )
    chunks = await source_chunks_repo.create_many(
        db_session,
        [
            NewSourceChunk(
                source_id=source.id, project_id=project.id, chunk_index=i, content=f"chunk {i}"
            )
            for i in range(num_chunks)
        ],
    )
    return project, source, chunks


# --- enqueue_indexing_job ------------------------------------------------------------


async def test_enqueue_indexing_job_creates_a_build_evidence_job(db_session):
    project, source, _ = await _make_source(db_session)

    job = await evidence_service.enqueue_indexing_job(
        db_session, project_id=project.id, source_id=source.id
    )

    assert job.kind == "build_evidence"
    assert job.status == "queued"
    assert job.payload == {"source_id": str(source.id)}


async def test_enqueuing_twice_for_the_same_source_raises_conflict_error(db_session):
    project, source, _ = await _make_source(db_session)
    await evidence_service.enqueue_indexing_job(
        db_session, project_id=project.id, source_id=source.id
    )

    with pytest.raises(ConflictError):
        await evidence_service.enqueue_indexing_job(
            db_session, project_id=project.id, source_id=source.id
        )


# --- index_source ----------------------------------------------------------------


async def test_index_source_embeds_every_chunk(db_session, monkeypatch):
    _, source, chunks = await _make_source(db_session, num_chunks=3)
    embeddings = _fake_embeddings(
        monkeypatch, {f"chunk {i}": [float(i)] * 1536 for i in range(3)}
    )

    count = await evidence_service.index_source(db_session, embeddings, source_id=source.id)

    assert count == 3
    remaining = await source_chunks_repo.list_missing_embedding_by_source(db_session, source.id)
    assert remaining == []
    for chunk in chunks:
        updated = await source_chunks_repo.get_by_id(db_session, chunk.id)
        assert updated is not None
        assert updated.embedding_model == MODEL


async def test_index_source_only_touches_chunks_missing_an_embedding(db_session, monkeypatch):
    """Re-running indexing (e.g. after a partial failure) must not re-embed
    - and therefore not re-spend an API call on - chunks that already have
    a vector.
    """
    _, source, chunks = await _make_source(db_session, num_chunks=2)
    await source_chunks_repo.update_embedding(
        db_session, chunk_id=chunks[0].id, embedding=[9.0] * 1536, embedding_model="pre-existing"
    )
    embeddings = _fake_embeddings(monkeypatch)
    create_spy = AsyncMock(wraps=embeddings._client.embeddings.create)  # noqa: SLF001
    monkeypatch.setattr(embeddings._client.embeddings, "create", create_spy)  # noqa: SLF001

    count = await evidence_service.index_source(db_session, embeddings, source_id=source.id)

    assert count == 1
    create_spy.assert_called_once()
    (call_kwargs,) = [call.kwargs for call in create_spy.call_args_list]
    assert call_kwargs["input"] == ["chunk 1"]
    untouched = await source_chunks_repo.get_by_id(db_session, chunks[0].id)
    assert untouched is not None
    assert untouched.embedding_model == "pre-existing"  # left exactly as it was


async def test_index_source_with_nothing_to_embed_is_a_noop(db_session, monkeypatch):
    _, source, _ = await _make_source(db_session, num_chunks=0)
    embeddings = _fake_embeddings(monkeypatch)

    assert await evidence_service.index_source(db_session, embeddings, source_id=source.id) == 0


# --- run_build_evidence_job --------------------------------------------------------


async def test_run_build_evidence_job_succeeds_and_records_the_chunk_count(db_session, monkeypatch):
    project, source, _ = await _make_source(db_session, num_chunks=2)
    embeddings = _fake_embeddings(monkeypatch)
    job = await evidence_service.enqueue_indexing_job(
        db_session, project_id=project.id, source_id=source.id
    )

    completed = await evidence_service.run_build_evidence_job(
        db_session, embeddings, job_id=job.id
    )

    assert completed.status == "succeeded"
    assert completed.result == {"chunks_embedded": 2}
    assert completed.attempts == 1
    assert completed.started_at is not None
    assert completed.finished_at is not None


async def test_run_build_evidence_job_marks_failed_on_embedding_error(db_session):
    project, source, _ = await _make_source(db_session, num_chunks=1)
    job = await evidence_service.enqueue_indexing_job(
        db_session, project_id=project.id, source_id=source.id
    )
    client = AsyncOpenAI(api_key="sk-test-not-a-real-key")

    async def _boom(**_kwargs):
        raise RuntimeError("openai is unreachable")

    client.embeddings.create = _boom  # type: ignore[method-assign]
    embeddings = Embeddings(client, model=MODEL)

    completed = await evidence_service.run_build_evidence_job(
        db_session, embeddings, job_id=job.id
    )

    assert completed.status == "failed"
    assert completed.error == "openai is unreachable"
    remaining = await source_chunks_repo.list_missing_embedding_by_source(db_session, source.id)
    assert len(remaining) == 1  # nothing was embedded


async def test_run_build_evidence_job_raises_not_found_for_a_missing_job(db_session, monkeypatch):
    embeddings = _fake_embeddings(monkeypatch)

    with pytest.raises(NotFoundError):
        await evidence_service.run_build_evidence_job(db_session, embeddings, job_id=uuid4())


async def test_run_build_evidence_job_increments_attempts_on_retry(db_session, monkeypatch):
    project, source, _ = await _make_source(db_session, num_chunks=1)
    embeddings = _fake_embeddings(monkeypatch)
    job = await evidence_service.enqueue_indexing_job(
        db_session, project_id=project.id, source_id=source.id
    )

    first = await evidence_service.run_build_evidence_job(db_session, embeddings, job_id=job.id)
    # A second run is safe (idempotent indexing underneath) even though a
    # real system wouldn't normally re-run an already-succeeded job.
    second = await evidence_service.run_build_evidence_job(db_session, embeddings, job_id=job.id)

    assert first.attempts == 1
    assert second.attempts == 2
    assert second.result == {"chunks_embedded": 0}  # nothing left to embed the second time
