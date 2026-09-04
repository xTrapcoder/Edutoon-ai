"""``claim_verifications`` / ``claim_citations`` - Phase A of the Evidence /
Verification Engine (repository + migration only; no service or API layer
yet). Focus: persistence shape, and the FK/CHECK constraints that make
"a citation always points at a real chunk" a DB-level guarantee, not just an
application-level convention.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from edutoon.core.errors import ConflictError
from edutoon.db.tables import source_chunks
from edutoon.repositories import claim_verifications as claim_verifications_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.claim_verifications import NewClaimCitation
from edutoon.repositories.source_chunks import NewSourceChunk


async def _make_chunk(db_session):
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
    [chunk] = await source_chunks_repo.create_many(
        db_session,
        [NewSourceChunk(source_id=source.id, project_id=project.id, chunk_index=0, content="X")],
    )
    return project, chunk


# --- persistence --------------------------------------------------------------------


async def test_create_returns_the_persisted_row(db_session):
    project, _ = await _make_chunk(db_session)

    verification = await claim_verifications_repo.create(
        db_session,
        project_id=project.id,
        claim_text="The mitochondria is the powerhouse of the cell.",
        status="supported",
        embedding_model="text-embedding-3-small",
        llm_model="claude-sonnet-5",
        rationale="Directly stated in the source.",
    )

    assert verification.id is not None
    assert verification.project_id == project.id
    assert verification.status == "supported"
    assert verification.rationale == "Directly stated in the source."


async def test_create_with_citations_persists_both(db_session):
    project, chunk = await _make_chunk(db_session)

    verification = await claim_verifications_repo.create(
        db_session,
        project_id=project.id,
        claim_text="Claim backed by one chunk.",
        status="supported",
        embedding_model="text-embedding-3-small",
        llm_model="claude-sonnet-5",
        citations=[NewClaimCitation(source_chunk_id=chunk.id, similarity=0.92)],
    )

    citations = await claim_verifications_repo.list_citations(db_session, verification.id)

    assert [c.source_chunk_id for c in citations] == [chunk.id]
    assert citations[0].similarity == pytest.approx(0.92)
    assert citations[0].claim_verification_id == verification.id


async def test_create_with_empty_citations_is_valid(db_session):
    """A claim can legitimately be verified as unsupported with zero
    citations - the DB must not require at least one, since forcing a
    citation onto an unsupported claim would fabricate false traceability.
    """
    project, _ = await _make_chunk(db_session)

    verification = await claim_verifications_repo.create(
        db_session,
        project_id=project.id,
        claim_text="Claim not backed by anything in the source.",
        status="unsupported",
        embedding_model="text-embedding-3-small",
        llm_model="claude-sonnet-5",
    )

    assert await claim_verifications_repo.list_citations(db_session, verification.id) == []


async def test_get_by_id_round_trips(db_session):
    project, _ = await _make_chunk(db_session)
    created = await claim_verifications_repo.create(
        db_session,
        project_id=project.id,
        claim_text="Round trip claim.",
        status="unsupported",
        embedding_model="text-embedding-3-small",
        llm_model="claude-sonnet-5",
    )

    fetched = await claim_verifications_repo.get_by_id(db_session, created.id)

    assert fetched == created


async def test_get_by_id_returns_none_when_missing(db_session):
    assert await claim_verifications_repo.get_by_id(db_session, uuid4()) is None


async def test_list_by_project_is_scoped_and_paginated(db_session):
    project_a, _ = await _make_chunk(db_session)
    project_b, _ = await _make_chunk(db_session)
    await claim_verifications_repo.create(
        db_session,
        project_id=project_a.id,
        claim_text="Claim for project A.",
        status="unsupported",
        embedding_model="text-embedding-3-small",
        llm_model="claude-sonnet-5",
    )
    await claim_verifications_repo.create(
        db_session,
        project_id=project_b.id,
        claim_text="Claim for project B.",
        status="unsupported",
        embedding_model="text-embedding-3-small",
        llm_model="claude-sonnet-5",
    )

    page = await claim_verifications_repo.list_by_project(db_session, project_a.id)

    assert [v.project_id for v in page.items] == [project_a.id]  # never leaks project B's row


# --- FK / CHECK behaviour ------------------------------------------------------------


async def test_citation_referencing_missing_verification_id_fails(db_session):
    _, chunk = await _make_chunk(db_session)

    with pytest.raises(ConflictError):
        await claim_verifications_repo.add_citations(
            db_session,
            claim_verification_id=uuid4(),
            citations=[NewClaimCitation(source_chunk_id=chunk.id, similarity=0.5)],
        )


async def test_citation_referencing_missing_source_chunk_id_fails(db_session):
    project, _ = await _make_chunk(db_session)
    verification = await claim_verifications_repo.create(
        db_session,
        project_id=project.id,
        claim_text="Claim awaiting a citation.",
        status="supported",
        embedding_model="text-embedding-3-small",
        llm_model="claude-sonnet-5",
    )

    with pytest.raises(ConflictError):
        await claim_verifications_repo.add_citations(
            db_session,
            claim_verification_id=verification.id,
            citations=[NewClaimCitation(source_chunk_id=uuid4(), similarity=0.5)],
        )


async def test_duplicate_citation_for_same_chunk_raises_conflict_error(db_session):
    project, chunk = await _make_chunk(db_session)
    verification = await claim_verifications_repo.create(
        db_session,
        project_id=project.id,
        claim_text="Claim citing the same chunk twice.",
        status="supported",
        embedding_model="text-embedding-3-small",
        llm_model="claude-sonnet-5",
        citations=[NewClaimCitation(source_chunk_id=chunk.id, similarity=0.7)],
    )

    with pytest.raises(ConflictError) as excinfo:
        await claim_verifications_repo.add_citations(
            db_session,
            claim_verification_id=verification.id,
            citations=[NewClaimCitation(source_chunk_id=chunk.id, similarity=0.8)],
        )

    assert excinfo.value.status_code == 409
    assert "already cited" in excinfo.value.message.lower()


async def test_similarity_out_of_range_fails(db_session):
    project, chunk = await _make_chunk(db_session)
    verification = await claim_verifications_repo.create(
        db_session,
        project_id=project.id,
        claim_text="Claim with a malformed similarity score.",
        status="supported",
        embedding_model="text-embedding-3-small",
        llm_model="claude-sonnet-5",
    )

    with pytest.raises(ConflictError):
        await claim_verifications_repo.add_citations(
            db_session,
            claim_verification_id=verification.id,
            citations=[NewClaimCitation(source_chunk_id=chunk.id, similarity=1.5)],
        )


async def test_deleting_a_cited_source_chunk_is_restricted(db_session):
    project, chunk = await _make_chunk(db_session)
    await claim_verifications_repo.create(
        db_session,
        project_id=project.id,
        claim_text="Claim that cites a chunk we will try to delete.",
        status="supported",
        embedding_model="text-embedding-3-small",
        llm_model="claude-sonnet-5",
        citations=[NewClaimCitation(source_chunk_id=chunk.id, similarity=0.6)],
    )

    with pytest.raises(IntegrityError):
        await db_session.execute(delete(source_chunks).where(source_chunks.c.id == chunk.id))
