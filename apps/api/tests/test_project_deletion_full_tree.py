"""Deleting a project that holds a whole pipeline's worth of rows.

Before migration 0018 ``claim_citations -> source_chunks`` and
``script_segment_claims -> claim_verifications`` were ``ON DELETE RESTRICT``, so
``DELETE /v1/projects/{id}`` failed for any project with verified claims - which, since the
PowerPoint path quotes a claim from every slide, was every deck. This builds one project with
every kind of row the pipeline writes (sources and chunks, topics and their citations, claims and
their citations, a script with segments, claim links, narration audio, scenes, and rendered
video chunks and output) and proves the one cascading delete removes all of it, and the stored
audio and video objects with it.

``db_session`` is the transactional fixture, so nothing persists; storage is the real local MinIO
and every object a test puts there is removed again by the test itself.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from edutoon.core.config import get_settings
from edutoon.domain.render import chunk_storage_key, output_storage_key
from edutoon.providers.storage import Storage, get_storage_client
from edutoon.repositories import claim_verifications as claims_repo
from edutoon.repositories import project_topics as topics_repo
from edutoon.repositories import render as render_repo
from edutoon.repositories import source_chunks as chunks_repo
from edutoon.repositories import topic_citations as topic_citations_repo
from edutoon.repositories import uploaded_sources as sources_repo
from edutoon.repositories.claim_verifications import NewClaimCitation
from edutoon.repositories.render import OutputScene
from edutoon.repositories.source_chunks import NewSourceChunk
from edutoon.services import projects as projects_service
from tests.render_helpers import Built, build_project

CHUNK_HASH = "a" * 64


@pytest.fixture
async def storage() -> AsyncIterator[Storage]:
    settings = get_settings()
    yield get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )


async def _populate(db_session, storage: Storage) -> Built:
    """Everything a finished project holds, linked together the way the pipeline links it."""
    settings = get_settings()
    # An approved script with narration audio (in storage) and a storyboard of scenes.
    built = await build_project(db_session, storage)
    project_id = built.project.id

    source = await sources_repo.create(
        db_session,
        project_id=project_id,
        original_filename="deck.pptx",
        storage_bucket=settings.BUCKET_UPLOADS,
        storage_key=f"projects/{project_id}/{uuid4().hex}.pptx",
        byte_size=10,
        checksum_sha256=uuid4().hex + uuid4().hex,
        kind="pptx",
    )
    chunks = await chunks_repo.create_many(
        db_session,
        [
            NewSourceChunk(
                source_id=source.id, project_id=project_id, chunk_index=i, content=f"Slide {i}."
            )
            for i in range(2)
        ],
    )

    topic = await topics_repo.create(db_session, project_id=project_id, title="Topic")
    await topic_citations_repo.add_many(
        db_session,
        project_id=project_id,
        topic_id=topic.id,
        source_chunk_ids=[chunk.id for chunk in chunks],
    )

    claim_ids: list[UUID] = []
    for chunk in chunks:
        claim = await claims_repo.create(
            db_session,
            project_id=project_id,
            claim_text=f"A claim quoted from {chunk.id}.",
            status="supported",
            embedding_model="none",
            llm_model="deck-engine/1",
            topic_id=topic.id,
            citations=[NewClaimCitation(source_chunk_id=chunk.id, similarity=1.0)],
        )
        claim_ids.append(claim.id)
    # The script's segments cite those claims - the link the second RESTRICT used to guard.
    for segment in built.segments:
        for claim_id in claim_ids:
            await db_session.execute(
                text(
                    "INSERT INTO script_segment_claims (script_segment_id, claim_verification_id) "
                    "VALUES (:s, :c)"
                ),
                {"s": segment.id, "c": claim_id},
            )

    # A rendered chunk and the finished video, both with objects in storage.
    chunk_key = chunk_storage_key(project_id, "horizontal", "low", CHUNK_HASH)
    output_key = output_storage_key(project_id, built.script.id, "horizontal", "low")
    for key in (chunk_key, output_key):
        await storage.put_object(
            bucket=settings.BUCKET_OUTPUTS, key=key, body=b"video", content_type="video/mp4"
        )
    await render_repo.add_chunk(
        db_session,
        project_id=project_id,
        chunk_hash=CHUNK_HASH,
        orientation="horizontal",
        quality="low",
        renderer_version="test",
        storage_bucket=settings.BUCKET_OUTPUTS,
        storage_key=chunk_key,
        byte_size=5,
        frame_count=30,
        duration_ms=1000,
    )
    await render_repo.replace_output(
        db_session,
        project_id=project_id,
        script_id=built.script.id,
        job_id=None,
        orientation="horizontal",
        quality="low",
        output_hash="h",
        renderer_version="test",
        encoder="libx264",
        fps=30,
        width=640,
        height=360,
        duration_ms=1000,
        byte_size=5,
        storage_bucket=settings.BUCKET_OUTPUTS,
        storage_key=output_key,
        probe={},
        scenes=[
            OutputScene(
                position=0,
                script_segment_id=built.segments[0].id,
                scene_index=0,
                chunk_hash=CHUNK_HASH,
            )
        ],
    )
    built.storage_keys.extend(
        [(settings.BUCKET_OUTPUTS, chunk_key), (settings.BUCKET_OUTPUTS, output_key)]
    )
    return built


async def _remove_objects(storage: Storage, built: Built) -> None:
    """Remove what the test put in storage, from the keys it recorded - never from the database,
    so a failed delete (an aborted transaction) cannot hide its own error behind this cleanup."""
    for bucket, key in built.storage_keys:
        await storage.delete_object(bucket=bucket, key=key)


# What the project owns, each counted by whatever ties it to the project (a table with no
# project_id of its own is reached through its parent). Every one must be non-zero before the
# delete - so a quietly empty fixture cannot make the test pass - and zero after it.
_OWNED = {
    "uploaded_sources": "project_id = :p",
    "source_chunks": "project_id = :p",
    "project_topics": "project_id = :p",
    "topic_citations": "topic_id IN (SELECT id FROM project_topics WHERE project_id = :p)",
    "claim_verifications": "project_id = :p",
    "claim_citations": "claim_verification_id IN "
    "(SELECT id FROM claim_verifications WHERE project_id = :p)",
    "scripts": "project_id = :p",
    "script_segments": "script_id IN (SELECT id FROM scripts WHERE project_id = :p)",
    "script_segment_claims": "script_segment_id IN (SELECT ss.id FROM script_segments ss "
    "JOIN scripts s ON s.id = ss.script_id WHERE s.project_id = :p)",
    "script_segment_audio": "script_segment_id IN (SELECT ss.id FROM script_segments ss "
    "JOIN scripts s ON s.id = ss.script_id WHERE s.project_id = :p)",
    "script_segment_scenes": "script_segment_id IN (SELECT ss.id FROM script_segments ss "
    "JOIN scripts s ON s.id = ss.script_id WHERE s.project_id = :p)",
    "render_chunks": "project_id = :p",
    "render_outputs": "project_id = :p",
    "render_output_scenes": "render_output_id IN "
    "(SELECT id FROM render_outputs WHERE project_id = :p)",
}


async def _counts(db_session, project_id: UUID) -> dict[str, int]:
    return {
        table: (
            await db_session.execute(
                text(f"SELECT count(*) FROM {table} WHERE {where}"),  # noqa: S608 - fixed above
                {"p": project_id},
            )
        ).scalar_one()
        for table, where in _OWNED.items()
    }


async def test_a_project_with_claims_citations_segments_audio_and_video_deletes(
    db_session, storage
):
    built = await _populate(db_session, storage)
    project = built.project
    try:
        before = await _counts(db_session, project.id)
        assert all(n > 0 for n in before.values()), before

        files = await projects_service.delete_project(
            db_session, project_id=project.id, owner_id=project.owner_id
        )

        assert await _counts(db_session, project.id) == dict.fromkeys(_OWNED, 0)

        # The stored audio, rendered chunk and finished video were all reported for removal...
        reported = {(file.bucket, file.key) for file in files}
        assert set(built.storage_keys) <= reported
        # ... and removing them leaves nothing behind.
        await projects_service.remove_stored_files(storage, project_id=project.id, files=files)
        for bucket, key in built.storage_keys:
            assert await storage.object_exists(bucket=bucket, key=key) is False
    finally:
        await _remove_objects(storage, built)


async def test_deleting_another_users_project_still_deletes_nothing(db_session, storage):
    from edutoon.core.errors import NotFoundError

    built = await _populate(db_session, storage)
    try:
        with pytest.raises(NotFoundError):
            await projects_service.delete_project(
                db_session, project_id=built.project.id, owner_id=uuid4()
            )

        assert all(n > 0 for n in (await _counts(db_session, built.project.id)).values())
    finally:
        await _remove_objects(storage, built)
