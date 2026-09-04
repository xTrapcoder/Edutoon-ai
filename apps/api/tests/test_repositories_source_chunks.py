from __future__ import annotations

from uuid import uuid4

import pytest

from edutoon.repositories import projects as projects_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.source_chunks import NewSourceChunk


async def _make_source(db_session):
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
    return project, source


async def test_create_many_returns_all_rows_in_order(db_session):
    project, source = await _make_source(db_session)

    chunks = await source_chunks_repo.create_many(
        db_session,
        [
            NewSourceChunk(
                source_id=source.id, project_id=project.id, chunk_index=0, content="First"
            ),
            NewSourceChunk(
                source_id=source.id, project_id=project.id, chunk_index=1, content="Second"
            ),
        ],
    )

    assert [chunk.content for chunk in chunks] == ["First", "Second"]
    assert all(chunk.embedding_model is None for chunk in chunks)


async def test_create_many_with_empty_list_is_a_noop(db_session):
    assert await source_chunks_repo.create_many(db_session, []) == []


async def test_list_by_source(db_session):
    project, source = await _make_source(db_session)
    [chunk] = await source_chunks_repo.create_many(
        db_session,
        [NewSourceChunk(source_id=source.id, project_id=project.id, chunk_index=0, content="X")],
    )

    page = await source_chunks_repo.list_by_source(db_session, source.id)

    assert [item.id for item in page.items] == [chunk.id]


async def test_update_embedding_sets_both_columns_together(db_session):
    project, source = await _make_source(db_session)
    [chunk] = await source_chunks_repo.create_many(
        db_session,
        [NewSourceChunk(source_id=source.id, project_id=project.id, chunk_index=0, content="X")],
    )
    assert chunk.embedding_model is None

    updated = await source_chunks_repo.update_embedding(
        db_session,
        chunk_id=chunk.id,
        embedding=[0.1] * 1536,
        embedding_model="text-embedding-3-small",
    )

    assert updated is not None
    assert updated.embedding_model == "text-embedding-3-small"


async def test_update_embedding_returns_none_when_missing(db_session):
    assert (
        await source_chunks_repo.update_embedding(
            db_session, chunk_id=uuid4(), embedding=[0.1] * 1536, embedding_model="m"
        )
        is None
    )


async def test_list_missing_embedding_by_source_only_returns_unembedded_chunks(db_session):
    project, source = await _make_source(db_session)
    embedded, unembedded = await source_chunks_repo.create_many(
        db_session,
        [
            NewSourceChunk(source_id=source.id, project_id=project.id, chunk_index=0, content="A"),
            NewSourceChunk(source_id=source.id, project_id=project.id, chunk_index=1, content="B"),
        ],
    )
    await source_chunks_repo.update_embedding(
        db_session, chunk_id=embedded.id, embedding=[0.1] * 1536, embedding_model="m"
    )

    remaining = await source_chunks_repo.list_missing_embedding_by_source(db_session, source.id)

    assert [chunk.id for chunk in remaining] == [unembedded.id]


# --- search_by_similarity -----------------------------------------------------------


def _unit_vector(*nonzero_at: int, dimensions: int = 1536) -> list[float]:
    """A vector with ``1.0`` at each given index and ``0.0`` elsewhere -
    lets tests reason about cosine similarity/distance by hand instead of
    trusting opaque real-world embeddings.
    """
    vector = [0.0] * dimensions
    for index in nonzero_at:
        vector[index] = 1.0
    return vector


async def _make_embedded_chunk(
    db_session, *, project=None, embedding: list[float] | None, content: str = "X"
):
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
    [chunk] = await source_chunks_repo.create_many(
        db_session,
        [
            NewSourceChunk(
                source_id=source.id, project_id=project.id, chunk_index=0, content=content
            )
        ],
    )
    if embedding is not None:
        chunk = await source_chunks_repo.update_embedding(
            db_session, chunk_id=chunk.id, embedding=embedding, embedding_model="test-model"
        )
        assert chunk is not None
    return project, chunk


async def test_search_by_similarity_orders_by_cosine_similarity_descending(db_session):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="P", source_type="pdf"
    )
    # query = e0. identical -> similarity 1; 45 degrees off -> ~0.707; orthogonal -> 0.
    _, identical = await _make_embedded_chunk(
        db_session, project=project, embedding=_unit_vector(0), content="identical"
    )
    _, diagonal = await _make_embedded_chunk(
        db_session, project=project, embedding=_unit_vector(0, 1), content="diagonal"
    )
    _, orthogonal = await _make_embedded_chunk(
        db_session, project=project, embedding=_unit_vector(1), content="orthogonal"
    )

    results = await source_chunks_repo.search_by_similarity(
        db_session, project_id=project.id, query_embedding=_unit_vector(0)
    )

    assert [chunk.id for chunk, _ in results] == [identical.id, diagonal.id, orthogonal.id]
    similarities = [similarity for _, similarity in results]
    assert similarities[0] == pytest.approx(1.0)
    assert similarities[1] == pytest.approx(0.7071, abs=1e-3)
    assert similarities[2] == pytest.approx(0.0, abs=1e-6)
    assert similarities == sorted(similarities, reverse=True)


async def test_search_by_similarity_never_returns_another_projects_chunks(db_session):
    """The security boundary: two chunks with the *identical* embedding in
    different projects - without the project_id filter both would tie for
    first place, so this proves the filter is real, not just plausible.
    """
    shared_embedding = _unit_vector(0)
    project_a, chunk_a = await _make_embedded_chunk(db_session, embedding=shared_embedding)
    _project_b, _chunk_b = await _make_embedded_chunk(db_session, embedding=shared_embedding)

    results = await source_chunks_repo.search_by_similarity(
        db_session, project_id=project_a.id, query_embedding=shared_embedding
    )

    assert [chunk.id for chunk, _ in results] == [chunk_a.id]
    assert all(chunk.project_id == project_a.id for chunk, _ in results)


async def test_search_by_similarity_excludes_chunks_without_an_embedding(db_session):
    project, embedded = await _make_embedded_chunk(db_session, embedding=_unit_vector(0))
    await _make_embedded_chunk(db_session, project=project, embedding=None, content="not indexed")

    results = await source_chunks_repo.search_by_similarity(
        db_session, project_id=project.id, query_embedding=_unit_vector(0)
    )

    assert [chunk.id for chunk, _ in results] == [embedded.id]


async def test_search_by_similarity_with_no_embedded_chunks_returns_empty(db_session):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="Empty", source_type="pdf"
    )

    results = await source_chunks_repo.search_by_similarity(
        db_session, project_id=project.id, query_embedding=_unit_vector(0)
    )

    assert results == []


async def test_search_by_similarity_respects_limit(db_session):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="P", source_type="pdf"
    )
    for i in range(5):
        await _make_embedded_chunk(
            db_session, project=project, embedding=_unit_vector(0, 100 + i), content=f"c{i}"
        )

    results = await source_chunks_repo.search_by_similarity(
        db_session, project_id=project.id, query_embedding=_unit_vector(0), limit=2
    )

    assert len(results) == 2
