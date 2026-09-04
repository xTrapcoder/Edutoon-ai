from __future__ import annotations

from uuid import uuid4

from edutoon.repositories import projects as projects_repo
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories import users as users_repo


async def _make_project(db_session):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    return await projects_repo.create(
        db_session, owner_id=owner.id, title="P", source_type="pdf"
    )


def _sha256_hex() -> str:
    return uuid4().hex + uuid4().hex[:32]  # 64 hex chars, matches ck_..._checksum_sha256_shape


async def test_create_and_get_by_id(db_session):
    project = await _make_project(db_session)
    checksum = _sha256_hex()

    source = await uploaded_sources_repo.create(
        db_session,
        project_id=project.id,
        original_filename="notes.pdf",
        storage_bucket="edutoon-uploads",
        storage_key=f"{project.id}/notes.pdf",
        byte_size=1024,
        checksum_sha256=checksum,
    )

    assert source.status == "pending"
    assert source.kind == "pdf"
    assert await uploaded_sources_repo.get_by_id(db_session, source.id) == source


async def test_get_by_checksum(db_session):
    project = await _make_project(db_session)
    checksum = _sha256_hex()
    source = await uploaded_sources_repo.create(
        db_session,
        project_id=project.id,
        original_filename="notes.pdf",
        storage_bucket="edutoon-uploads",
        storage_key=f"{project.id}/notes.pdf",
        byte_size=1024,
        checksum_sha256=checksum,
    )

    found = await uploaded_sources_repo.get_by_checksum(
        db_session, project_id=project.id, checksum_sha256=checksum
    )

    assert found == source
