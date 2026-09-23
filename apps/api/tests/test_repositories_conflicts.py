"""Unique-constraint violations must surface as ConflictError, never a raw
IntegrityError / 500. One test per repository write operation that has a
real unique constraint in the migrations.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from edutoon.core.errors import ConflictError
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.source_chunks import NewSourceChunk


async def _make_project(db_session, *, source_type: str = "topic"):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    return await projects_repo.create(
        db_session, owner_id=owner.id, title="P", source_type=source_type
    )


def _sha256_hex() -> str:
    return uuid4().hex + uuid4().hex[:32]


async def test_duplicate_email_raises_conflict_error(db_session):
    email = f"{uuid4()}@example.com"
    await users_repo.create(db_session, email=email)

    with pytest.raises(ConflictError) as excinfo:
        await users_repo.create(db_session, email=email)

    assert excinfo.value.status_code == 409
    assert "email" in excinfo.value.message.lower()


async def test_duplicate_clerk_user_id_raises_conflict_error(db_session):
    clerk_id = f"clerk_{uuid4()}"
    await users_repo.create(db_session, email=f"{uuid4()}@example.com", clerk_user_id=clerk_id)

    with pytest.raises(ConflictError):
        await users_repo.create(
            db_session, email=f"{uuid4()}@example.com", clerk_user_id=clerk_id
        )


async def test_duplicate_checksum_in_same_project_raises_conflict_error(db_session):
    project = await _make_project(db_session, source_type="pdf")
    checksum = _sha256_hex()
    await uploaded_sources_repo.create(
        db_session,
        project_id=project.id,
        original_filename="a.pdf",
        storage_bucket="edutoon-uploads",
        storage_key=f"{project.id}/a.pdf",
        byte_size=10,
        checksum_sha256=checksum,
    )

    with pytest.raises(ConflictError):
        await uploaded_sources_repo.create(
            db_session,
            project_id=project.id,
            original_filename="b.pdf",
            storage_bucket="edutoon-uploads",
            storage_key=f"{project.id}/b.pdf",
            byte_size=20,
            checksum_sha256=checksum,
        )


async def test_duplicate_idempotency_key_raises_conflict_error(db_session):
    project = await _make_project(db_session)
    await jobs_repo.create(
        db_session, project_id=project.id, kind="extract_topics", idempotency_key="dup-key"
    )

    with pytest.raises(ConflictError):
        await jobs_repo.create(
            db_session, project_id=project.id, kind="extract_topics", idempotency_key="dup-key"
        )


async def test_duplicate_topic_position_raises_conflict_error(db_session):
    project = await _make_project(db_session)
    await project_topics_repo.create(db_session, project_id=project.id, title="First")

    with pytest.raises(ConflictError):
        # Same (project_id, parent_id=None, position=0) as the topic above.
        await project_topics_repo.create(db_session, project_id=project.id, title="Second")


async def test_duplicate_chunk_index_raises_conflict_error(db_session):
    project = await _make_project(db_session, source_type="pdf")
    source = await uploaded_sources_repo.create(
        db_session,
        project_id=project.id,
        original_filename="a.pdf",
        storage_bucket="edutoon-uploads",
        storage_key=f"{project.id}/a.pdf",
        byte_size=10,
        checksum_sha256=_sha256_hex(),
    )
    await source_chunks_repo.create_many(
        db_session,
        [NewSourceChunk(source_id=source.id, project_id=project.id, chunk_index=0, content="X")],
    )

    with pytest.raises(ConflictError):
        await source_chunks_repo.create_many(
            db_session,
            [
                NewSourceChunk(
                    source_id=source.id, project_id=project.id, chunk_index=0, content="Y"
                )
            ],
        )
