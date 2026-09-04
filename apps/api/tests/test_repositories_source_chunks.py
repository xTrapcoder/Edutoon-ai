from __future__ import annotations

from uuid import uuid4

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
