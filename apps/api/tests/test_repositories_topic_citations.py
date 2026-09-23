"""``topic_citations`` - the evidence link between a topic and the
``source_chunks`` it was drawn from (migration 0009).

Focus: the project-scoping guarantee (a topic can never cite another
project's material, enforced in the SQL itself), and the cascade behaviour
that distinguishes this table from ``claim_citations`` - removing material or
topics must never be blocked by topic evidence.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from edutoon.core.errors import ConflictError
from edutoon.db.tables import project_topics, topic_citations, uploaded_sources
from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories import topic_citations as topic_citations_repo
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.source_chunks import NewSourceChunk


async def _project_with_chunks(db_session, *, chunks: int = 3):
    """A project with one source of ``chunks`` page-sized chunks (page n =
    chunk n-1), plus one topic in it.
    """
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
    created = await source_chunks_repo.create_many(
        db_session,
        [
            NewSourceChunk(
                source_id=source.id,
                project_id=project.id,
                chunk_index=i,
                content=f"chunk {i}",
                page_from=i + 1,
                page_to=i + 1,
            )
            for i in range(chunks)
        ],
    )
    topic = await project_topics_repo.create(
        db_session, project_id=project.id, title="Topic", origin="source"
    )
    return project, source, sorted(created, key=lambda c: c.chunk_index), topic


async def _citation_count(db_session, topic_id=None) -> int:
    stmt = select(func.count()).select_from(topic_citations)
    if topic_id is not None:
        stmt = stmt.where(topic_citations.c.topic_id == topic_id)
    return int((await db_session.execute(stmt)).scalar_one())


# --- add_many -------------------------------------------------------------------


async def test_add_many_cites_the_given_chunks(db_session):
    project, _source, chunks, topic = await _project_with_chunks(db_session)

    citations = await topic_citations_repo.add_many(
        db_session,
        project_id=project.id,
        topic_id=topic.id,
        source_chunk_ids=[chunks[0].id, chunks[2].id],
    )

    assert {c.source_chunk_id for c in citations} == {chunks[0].id, chunks[2].id}
    assert all(c.topic_id == topic.id for c in citations)
    assert all(c.id is not None and c.created_at is not None for c in citations)
    assert await _citation_count(db_session, topic.id) == 2


async def test_add_many_with_no_ids_is_a_noop(db_session):
    project, _source, _chunks, topic = await _project_with_chunks(db_session)

    assert (
        await topic_citations_repo.add_many(
            db_session, project_id=project.id, topic_id=topic.id, source_chunk_ids=[]
        )
        == []
    )
    assert await _citation_count(db_session, topic.id) == 0


async def test_add_many_collapses_duplicate_ids_in_the_input(db_session):
    project, _source, chunks, topic = await _project_with_chunks(db_session)

    citations = await topic_citations_repo.add_many(
        db_session,
        project_id=project.id,
        topic_id=topic.id,
        source_chunk_ids=[chunks[0].id, chunks[0].id, chunks[0].id],
    )

    assert len(citations) == 1
    assert await _citation_count(db_session, topic.id) == 1


async def test_add_many_leaves_out_ids_that_do_not_exist(db_session):
    project, _source, chunks, topic = await _project_with_chunks(db_session)

    citations = await topic_citations_repo.add_many(
        db_session,
        project_id=project.id,
        topic_id=topic.id,
        source_chunk_ids=[chunks[0].id, uuid4()],
    )

    assert [c.source_chunk_id for c in citations] == [chunks[0].id]


async def test_add_many_never_cites_another_projects_chunk(db_session):
    """The security boundary: project B's chunk offered as evidence for a
    topic in project A is dropped by the SQL itself, not by a caller
    remembering to filter. Nothing is written for it.
    """
    project_a, _source_a, chunks_a, topic_a = await _project_with_chunks(db_session)
    _project_b, _source_b, chunks_b, _topic_b = await _project_with_chunks(db_session)

    citations = await topic_citations_repo.add_many(
        db_session,
        project_id=project_a.id,
        topic_id=topic_a.id,
        source_chunk_ids=[chunks_a[0].id, chunks_b[0].id],
    )

    assert [c.source_chunk_id for c in citations] == [chunks_a[0].id]
    assert await _citation_count(db_session, topic_a.id) == 1


async def test_add_many_cites_nothing_when_every_chunk_is_foreign(db_session):
    project_a, _source_a, _chunks_a, topic_a = await _project_with_chunks(db_session)
    _project_b, _source_b, chunks_b, _topic_b = await _project_with_chunks(db_session)

    citations = await topic_citations_repo.add_many(
        db_session,
        project_id=project_a.id,
        topic_id=topic_a.id,
        source_chunk_ids=[chunk.id for chunk in chunks_b],
    )

    assert citations == []
    assert await _citation_count(db_session, topic_a.id) == 0


async def test_add_many_ignores_a_topic_from_a_different_project(db_session):
    """Naming project B while passing a topic that lives in project A cites
    nothing - the topic must belong to the project the chunks are scoped to.
    """
    _project_a, _source_a, _chunks_a, topic_a = await _project_with_chunks(db_session)
    project_b, _source_b, chunks_b, _topic_b = await _project_with_chunks(db_session)

    citations = await topic_citations_repo.add_many(
        db_session,
        project_id=project_b.id,
        topic_id=topic_a.id,
        source_chunk_ids=[chunks_b[0].id],
    )

    assert citations == []
    assert await _citation_count(db_session, topic_a.id) == 0


async def test_citing_the_same_chunk_twice_is_a_conflict(db_session):
    project, _source, chunks, topic = await _project_with_chunks(db_session)
    await topic_citations_repo.add_many(
        db_session, project_id=project.id, topic_id=topic.id, source_chunk_ids=[chunks[0].id]
    )

    with pytest.raises(ConflictError) as excinfo:
        await topic_citations_repo.add_many(
            db_session,
            project_id=project.id,
            topic_id=topic.id,
            source_chunk_ids=[chunks[0].id],
        )

    assert "already cited by this topic" in excinfo.value.message


async def test_two_topics_may_cite_the_same_chunk(db_session):
    project, _source, chunks, topic = await _project_with_chunks(db_session)
    other_topic = await project_topics_repo.create(
        db_session, project_id=project.id, title="Other", position=1, origin="source"
    )

    await topic_citations_repo.add_many(
        db_session, project_id=project.id, topic_id=topic.id, source_chunk_ids=[chunks[0].id]
    )
    citations = await topic_citations_repo.add_many(
        db_session,
        project_id=project.id,
        topic_id=other_topic.id,
        source_chunk_ids=[chunks[0].id],
    )

    assert len(citations) == 1


# --- list_evidence_for_topics ---------------------------------------------------------


async def test_list_evidence_resolves_source_chunk_and_page_range(db_session):
    project, source, chunks, topic = await _project_with_chunks(db_session)
    await topic_citations_repo.add_many(
        db_session, project_id=project.id, topic_id=topic.id, source_chunk_ids=[chunks[1].id]
    )

    evidence = await topic_citations_repo.list_evidence_for_topics(
        db_session, project_id=project.id, topic_ids=[topic.id]
    )

    [item] = evidence[topic.id]
    assert item.topic_id == topic.id
    assert item.source_chunk_id == chunks[1].id
    assert item.source_id == source.id
    assert item.chunk_index == 1
    assert (item.page_from, item.page_to) == (2, 2)


async def test_list_evidence_groups_by_topic_in_document_order(db_session):
    project, _source, chunks, topic = await _project_with_chunks(db_session, chunks=4)
    other_topic = await project_topics_repo.create(
        db_session, project_id=project.id, title="Other", position=1, origin="source"
    )
    # Cited out of document order on purpose.
    await topic_citations_repo.add_many(
        db_session,
        project_id=project.id,
        topic_id=topic.id,
        source_chunk_ids=[chunks[3].id, chunks[0].id, chunks[2].id],
    )
    await topic_citations_repo.add_many(
        db_session, project_id=project.id, topic_id=other_topic.id, source_chunk_ids=[chunks[1].id]
    )

    evidence = await topic_citations_repo.list_evidence_for_topics(
        db_session, project_id=project.id, topic_ids=[topic.id, other_topic.id]
    )

    assert [e.chunk_index for e in evidence[topic.id]] == [0, 2, 3]
    assert [e.chunk_index for e in evidence[other_topic.id]] == [1]


async def test_list_evidence_omits_topics_with_no_evidence(db_session):
    project, _source, chunks, topic = await _project_with_chunks(db_session)
    bare = await project_topics_repo.create(
        db_session, project_id=project.id, title="Bare", position=1, origin="user"
    )
    await topic_citations_repo.add_many(
        db_session, project_id=project.id, topic_id=topic.id, source_chunk_ids=[chunks[0].id]
    )

    evidence = await topic_citations_repo.list_evidence_for_topics(
        db_session, project_id=project.id, topic_ids=[topic.id, bare.id]
    )

    assert set(evidence) == {topic.id}


async def test_list_evidence_with_no_topic_ids_is_empty(db_session):
    project, _source, _chunks, _topic = await _project_with_chunks(db_session)

    assert (
        await topic_citations_repo.list_evidence_for_topics(
            db_session, project_id=project.id, topic_ids=[]
        )
        == {}
    )


async def test_list_evidence_is_scoped_to_the_named_project(db_session):
    project_a, _source_a, chunks_a, topic_a = await _project_with_chunks(db_session)
    project_b, _source_b, _chunks_b, _topic_b = await _project_with_chunks(db_session)
    await topic_citations_repo.add_many(
        db_session, project_id=project_a.id, topic_id=topic_a.id, source_chunk_ids=[chunks_a[0].id]
    )

    evidence = await topic_citations_repo.list_evidence_for_topics(
        db_session, project_id=project_b.id, topic_ids=[topic_a.id]
    )

    assert evidence == {}


# --- delete_by_project ----------------------------------------------------------------


async def test_delete_by_project_clears_every_topics_evidence_including_user_topics(db_session):
    project, _source, chunks, derived = await _project_with_chunks(db_session)
    user_topic = await project_topics_repo.create(
        db_session, project_id=project.id, title="Mine", position=1, origin="user"
    )
    await topic_citations_repo.add_many(
        db_session, project_id=project.id, topic_id=derived.id, source_chunk_ids=[chunks[0].id]
    )
    await topic_citations_repo.add_many(
        db_session, project_id=project.id, topic_id=user_topic.id, source_chunk_ids=[chunks[1].id]
    )

    deleted = await topic_citations_repo.delete_by_project(db_session, project.id)

    assert deleted == 2
    assert await _citation_count(db_session) == 0


async def test_delete_by_project_leaves_other_projects_evidence_alone(db_session):
    project_a, _source_a, chunks_a, topic_a = await _project_with_chunks(db_session)
    project_b, _source_b, chunks_b, topic_b = await _project_with_chunks(db_session)
    await topic_citations_repo.add_many(
        db_session, project_id=project_a.id, topic_id=topic_a.id, source_chunk_ids=[chunks_a[0].id]
    )
    await topic_citations_repo.add_many(
        db_session, project_id=project_b.id, topic_id=topic_b.id, source_chunk_ids=[chunks_b[0].id]
    )

    await topic_citations_repo.delete_by_project(db_session, project_a.id)

    assert await _citation_count(db_session, topic_a.id) == 0
    assert await _citation_count(db_session, topic_b.id) == 1


# --- cascades: evidence never blocks removal ---------------------------------------------


async def test_deleting_a_topic_deletes_its_citations(db_session):
    project, _source, chunks, topic = await _project_with_chunks(db_session)
    await topic_citations_repo.add_many(
        db_session, project_id=project.id, topic_id=topic.id, source_chunk_ids=[chunks[0].id]
    )

    await db_session.execute(delete(project_topics).where(project_topics.c.id == topic.id))

    assert await _citation_count(db_session) == 0
    # ...and the chunk it pointed at is untouched.
    assert await source_chunks_repo.get_by_id(db_session, chunks[0].id) is not None


async def test_deleting_the_source_material_is_not_blocked_by_topic_evidence(db_session):
    """The deliberate difference from ``claim_citations`` (ON DELETE RESTRICT):
    topic evidence is derived, so removing the source must succeed, taking
    the chunks and the topic's evidence with it and leaving the topic itself
    in place - now visibly evidence-less.
    """
    project, source, chunks, topic = await _project_with_chunks(db_session)
    await topic_citations_repo.add_many(
        db_session,
        project_id=project.id,
        topic_id=topic.id,
        source_chunk_ids=[chunk.id for chunk in chunks],
    )

    await db_session.execute(delete(uploaded_sources).where(uploaded_sources.c.id == source.id))

    assert await _citation_count(db_session) == 0
    assert await source_chunks_repo.get_by_id(db_session, chunks[0].id) is None
    assert await project_topics_repo.get_by_id(db_session, topic.id) == topic


async def test_deleting_derived_topics_removes_their_evidence_but_not_a_user_topics(db_session):
    project, _source, chunks, derived = await _project_with_chunks(db_session)
    user_topic = await project_topics_repo.create(
        db_session, project_id=project.id, title="Mine", position=1, origin="user"
    )
    await topic_citations_repo.add_many(
        db_session, project_id=project.id, topic_id=derived.id, source_chunk_ids=[chunks[0].id]
    )
    await topic_citations_repo.add_many(
        db_session, project_id=project.id, topic_id=user_topic.id, source_chunk_ids=[chunks[1].id]
    )

    await project_topics_repo.delete_derived_by_project(db_session, project.id)

    assert await _citation_count(db_session, derived.id) == 0
    assert await _citation_count(db_session, user_topic.id) == 1
