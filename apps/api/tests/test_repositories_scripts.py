"""``scripts`` / ``script_segments`` / ``script_segment_claims`` - Script
Generation Engine SG-1 (repository + migration only). Focus: persistence
shape, the status-machine, and the FK/CHECK/unique constraints that make a
script's traceability back to verified claims a DB-level guarantee.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from edutoon.core.errors import ConflictError
from edutoon.db.tables import claim_verifications, project_topics
from edutoon.db.tables import scripts as scripts_table
from edutoon.domain.script_provenance import ValidatedScriptDraft, ValidatedScriptSegment
from edutoon.repositories import claim_verifications as claim_verifications_repo
from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.claim_verifications import NewClaimCitation
from edutoon.repositories.scripts import NewScriptSegment
from edutoon.repositories.source_chunks import NewSourceChunk

MODEL = "claude-sonnet-5"


async def _make_project(db_session, *, source_type: str = "pdf"):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    return await projects_repo.create(
        db_session, owner_id=owner.id, title="P", source_type=source_type
    )


async def _make_verified_claim(db_session, project):
    """A supported claim_verification with one real citation - the kind of
    row a script segment is allowed to cite.
    """
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
        [NewSourceChunk(source_id=source.id, project_id=project.id, chunk_index=0, content="X")],
    )
    return await claim_verifications_repo.create(
        db_session,
        project_id=project.id,
        claim_text="The sky is blue.",
        status="supported",
        embedding_model="text-embedding-3-small",
        llm_model=MODEL,
        citations=[NewClaimCitation(source_chunk_id=chunk.id, similarity=0.9)],
    )


# --- scripts: persistence + status machine ------------------------------------------


async def test_create_returns_the_persisted_row(db_session):
    project = await _make_project(db_session)

    script = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)

    assert script.id is not None
    assert script.project_id == project.id
    assert script.status == "pending"
    assert script.llm_model == MODEL
    assert script.estimated_duration_ms is None
    assert await scripts_repo.get_by_id(db_session, script.id) == script


async def test_get_by_id_returns_none_when_missing(db_session):
    assert await scripts_repo.get_by_id(db_session, uuid4()) is None


async def test_get_by_id_for_project_is_ownership_scoped(db_session):
    project_a = await _make_project(db_session)
    project_b = await _make_project(db_session)
    script = await scripts_repo.create(db_session, project_id=project_a.id, llm_model=MODEL)

    assert await scripts_repo.get_by_id_for_project(db_session, script.id, project_a.id) == script
    assert await scripts_repo.get_by_id_for_project(db_session, script.id, project_b.id) is None


async def test_list_by_project_never_leaks_another_project(db_session):
    project_a = await _make_project(db_session)
    project_b = await _make_project(db_session)
    await scripts_repo.create(db_session, project_id=project_a.id, llm_model=MODEL)
    await scripts_repo.create(db_session, project_id=project_b.id, llm_model=MODEL)

    page = await scripts_repo.list_by_project(db_session, project_a.id)

    assert [s.project_id for s in page.items] == [project_a.id]


async def test_set_status_advances_the_state_machine(db_session):
    project = await _make_project(db_session)
    script = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)

    drafted = await scripts_repo.set_status(
        db_session,
        project_id=project.id,
        script_id=script.id,
        status="draft",
        estimated_duration_ms=90_000,
    )
    assert drafted is not None
    assert drafted.status == "draft"
    assert drafted.estimated_duration_ms == 90_000

    approved = await scripts_repo.set_status(
        db_session, project_id=project.id, script_id=script.id, status="approved"
    )
    assert approved is not None
    assert approved.status == "approved"
    assert approved.estimated_duration_ms == 90_000  # unchanged


async def test_set_status_returns_none_when_missing(db_session):
    assert (
        await scripts_repo.set_status(
            db_session, project_id=uuid4(), script_id=uuid4(), status="draft"
        )
        is None
    )


async def test_set_status_is_project_scoped(db_session):
    project_a = await _make_project(db_session)
    project_b = await _make_project(db_session)
    script = await scripts_repo.create(db_session, project_id=project_a.id, llm_model=MODEL)

    # Right script, wrong project - looks like it does not exist (rule 9).
    assert (
        await scripts_repo.set_status(
            db_session, project_id=project_b.id, script_id=script.id, status="draft"
        )
        is None
    )
    assert (await scripts_repo.get_by_id(db_session, script.id)).status == "pending"


async def test_set_status_rejects_an_unknown_status(db_session):
    project = await _make_project(db_session)
    script = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)

    with pytest.raises(ConflictError):
        await scripts_repo.set_status(
            db_session, project_id=project.id, script_id=script.id, status="publishing"
        )


# --- one active script per project -------------------------------------------------


async def test_second_active_script_for_a_project_conflicts(db_session):
    project = await _make_project(db_session)
    await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)

    with pytest.raises(ConflictError) as excinfo:
        await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)

    assert excinfo.value.status_code == 409
    assert "active script" in excinfo.value.message.lower()


async def test_supersede_active_frees_the_slot_for_a_new_script(db_session):
    project = await _make_project(db_session)
    first = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)

    superseded_count = await scripts_repo.supersede_active(db_session, project_id=project.id)
    assert superseded_count == 1
    assert (await scripts_repo.get_by_id(db_session, first.id)).status == "superseded"

    second = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)
    assert (await scripts_repo.get_active(db_session, project.id)).id == second.id


async def test_supersede_active_can_spare_one_script(db_session):
    project = await _make_project(db_session)
    keep = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)

    changed = await scripts_repo.supersede_active(
        db_session, project_id=project.id, except_id=keep.id
    )

    assert changed == 0
    assert (await scripts_repo.get_by_id(db_session, keep.id)).status == "pending"


async def test_superseded_and_failed_scripts_do_not_block_a_new_one(db_session):
    project = await _make_project(db_session)
    s1 = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)
    await scripts_repo.set_status(
        db_session, project_id=project.id, script_id=s1.id, status="failed"
    )
    s2 = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)
    await scripts_repo.set_status(
        db_session, project_id=project.id, script_id=s2.id, status="superseded"
    )

    # Neither a failed nor a superseded script counts as active.
    s3 = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)
    assert s3.status == "pending"


# --- segments + claim citations ---------------------------------------------------


async def test_create_with_segments_and_claims_persists_the_whole_tree(db_session):
    project = await _make_project(db_session)
    verification = await _make_verified_claim(db_session, project)

    script = await scripts_repo.create(
        db_session,
        project_id=project.id,
        llm_model=MODEL,
        estimated_duration_ms=12_000,
        segments=[
            NewScriptSegment(
                segment_index=0,
                narration_text="Water covers most of the planet.",
                estimated_duration_ms=7_000,
                claim_verification_ids=(verification.id,),
            ),
            NewScriptSegment(
                segment_index=1,
                narration_text="And now, a recap.",
                estimated_duration_ms=5_000,
            ),
        ],
    )

    segments = await scripts_repo.list_segments(
        db_session, project_id=project.id, script_id=script.id
    )
    assert [s.segment_index for s in segments] == [0, 1]
    assert segments[0].narration_text == "Water covers most of the planet."
    assert segments[1].topic_id is None

    claims = await scripts_repo.list_segment_claims(
        db_session, project_id=project.id, script_segment_id=segments[0].id
    )
    assert [c.claim_verification_id for c in claims] == [verification.id]
    assert (
        await scripts_repo.list_segment_claims(
            db_session, project_id=project.id, script_segment_id=segments[1].id
        )
        == []
    )


async def test_add_segments_maps_claims_to_the_right_segment(db_session):
    project = await _make_project(db_session)
    v1 = await _make_verified_claim(db_session, project)
    v2 = await _make_verified_claim(db_session, project)
    script = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)

    segments = await scripts_repo.add_segments(
        db_session,
        script_id=script.id,
        segments=[
            NewScriptSegment(0, "First.", 1_000, claim_verification_ids=(v1.id,)),
            NewScriptSegment(1, "Second.", 1_000, claim_verification_ids=(v2.id, v1.id)),
        ],
    )

    first_claims = await scripts_repo.list_segment_claims(
        db_session, project_id=project.id, script_segment_id=segments[0].id
    )
    second_claims = await scripts_repo.list_segment_claims(
        db_session, project_id=project.id, script_segment_id=segments[1].id
    )
    assert {c.claim_verification_id for c in first_claims} == {v1.id}
    assert {c.claim_verification_id for c in second_claims} == {v1.id, v2.id}


async def test_a_segment_may_link_a_topic(db_session):
    project = await _make_project(db_session)
    topic = await project_topics_repo.create(db_session, project_id=project.id, title="Oceans")
    script = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)

    [segment] = await scripts_repo.add_segments(
        db_session,
        script_id=script.id,
        segments=[NewScriptSegment(0, "Oceans are vast.", 3_000, topic_id=topic.id)],
    )

    assert segment.topic_id == topic.id


# --- CHECK / FK / unique behaviour -----------------------------------------------


async def test_blank_narration_is_rejected(db_session):
    project = await _make_project(db_session)
    script = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)

    with pytest.raises(ConflictError):
        await scripts_repo.add_segments(
            db_session,
            script_id=script.id,
            segments=[NewScriptSegment(0, "   ", 1_000)],
        )


async def test_negative_segment_duration_is_rejected(db_session):
    project = await _make_project(db_session)
    script = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)

    with pytest.raises(ConflictError):
        await scripts_repo.add_segments(
            db_session,
            script_id=script.id,
            segments=[NewScriptSegment(0, "Fine.", -1)],
        )


async def test_negative_script_duration_is_rejected(db_session):
    project = await _make_project(db_session)

    with pytest.raises(ConflictError):
        await scripts_repo.create(
            db_session, project_id=project.id, llm_model=MODEL, estimated_duration_ms=-5
        )


async def test_duplicate_segment_index_in_a_script_conflicts(db_session):
    project = await _make_project(db_session)
    script = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)
    await scripts_repo.add_segments(
        db_session, script_id=script.id, segments=[NewScriptSegment(0, "First.", 1_000)]
    )

    with pytest.raises(ConflictError):
        await scripts_repo.add_segments(
            db_session, script_id=script.id, segments=[NewScriptSegment(0, "Clash.", 1_000)]
        )


async def test_a_segment_cannot_cite_the_same_claim_twice(db_session):
    project = await _make_project(db_session)
    verification = await _make_verified_claim(db_session, project)
    script = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)

    with pytest.raises(ConflictError) as excinfo:
        await scripts_repo.add_segments(
            db_session,
            script_id=script.id,
            segments=[
                NewScriptSegment(
                    0, "Cited twice.", 1_000,
                    claim_verification_ids=(verification.id, verification.id),
                )
            ],
        )

    assert "already cited" in excinfo.value.message.lower()


async def test_a_segment_citing_a_missing_verification_conflicts(db_session):
    project = await _make_project(db_session)
    script = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)

    with pytest.raises(ConflictError):
        await scripts_repo.add_segments(
            db_session,
            script_id=script.id,
            segments=[NewScriptSegment(0, "Ghost.", 1_000, claim_verification_ids=(uuid4(),))],
        )


async def test_deleting_a_cited_verification_is_restricted(db_session):
    project = await _make_project(db_session)
    verification = await _make_verified_claim(db_session, project)
    script = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)
    await scripts_repo.add_segments(
        db_session,
        script_id=script.id,
        segments=[
            NewScriptSegment(0, "Cites it.", 1_000, claim_verification_ids=(verification.id,))
        ],
    )

    with pytest.raises(IntegrityError):
        await db_session.execute(
            delete(claim_verifications).where(claim_verifications.c.id == verification.id)
        )


async def test_deleting_a_script_cascades_to_segments_and_claims(db_session):
    project = await _make_project(db_session)
    verification = await _make_verified_claim(db_session, project)
    script = await scripts_repo.create(
        db_session,
        project_id=project.id,
        llm_model=MODEL,
        segments=[NewScriptSegment(0, "Doomed.", 1_000, claim_verification_ids=(verification.id,))],
    )
    [segment] = await scripts_repo.list_segments(
        db_session, project_id=project.id, script_id=script.id
    )

    await db_session.execute(delete(scripts_table).where(scripts_table.c.id == script.id))

    assert (
        await scripts_repo.list_segments(
            db_session, project_id=project.id, script_id=script.id
        )
        == []
    )
    assert (
        await scripts_repo.list_segment_claims(
            db_session, project_id=project.id, script_segment_id=segment.id
        )
        == []
    )
    # the cited verification itself is untouched by the cascade
    assert await claim_verifications_repo.get_by_id(db_session, verification.id) is not None


async def test_deleting_a_topic_nulls_the_segment_link(db_session):
    project = await _make_project(db_session)
    topic = await project_topics_repo.create(db_session, project_id=project.id, title="T")
    script = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)
    [segment] = await scripts_repo.add_segments(
        db_session,
        script_id=script.id,
        segments=[NewScriptSegment(0, "Has a topic.", 1_000, topic_id=topic.id)],
    )

    await db_session.execute(
        delete(project_topics).where(project_topics.c.id == topic.id)
    )

    [refetched] = await scripts_repo.list_segments(
        db_session, project_id=project.id, script_id=script.id
    )
    assert refetched.id == segment.id
    assert refetched.topic_id is None


# --- create_script: atomic validated-draft persistence (SG-3) --------------------


def _validated_segment(
    index: int,
    narration: str,
    claim_ids: tuple,
    *,
    connective: bool = False,
    duration_ms: int = 2_000,
) -> ValidatedScriptSegment:
    return ValidatedScriptSegment(
        segment_index=index,
        narration_text=narration,
        connective=connective,
        claim_verification_ids=claim_ids,
        estimated_duration_ms=duration_ms,
    )


def _validated_draft(*segments: ValidatedScriptSegment, title: str = "A script"):
    return ValidatedScriptDraft(
        title=title,
        segments=tuple(segments),
        estimated_duration_ms=sum(s.estimated_duration_ms for s in segments),
    )


async def test_create_script_persists_the_validated_draft(db_session):
    project = await _make_project(db_session)
    v1 = await _make_verified_claim(db_session, project)
    draft = _validated_draft(
        _validated_segment(0, "Opening.", (), connective=True, duration_ms=1_000),
        _validated_segment(1, "A sourced fact.", (v1.id,), duration_ms=4_000),
    )

    script = await scripts_repo.create_script(
        db_session, project_id=project.id, llm_model=MODEL, draft=draft
    )

    assert script.status == "pending"
    assert script.estimated_duration_ms == 5_000
    segments = await scripts_repo.list_segments(
        db_session, project_id=project.id, script_id=script.id
    )
    assert [s.segment_index for s in segments] == [0, 1]
    assert await scripts_repo.list_segment_claims(
        db_session, project_id=project.id, script_segment_id=segments[0].id
    ) == []
    linked = await scripts_repo.list_segment_claims(
        db_session, project_id=project.id, script_segment_id=segments[1].id
    )
    assert [c.claim_verification_id for c in linked] == [v1.id]


async def test_create_script_supersedes_the_previous_active_script(db_session):
    project = await _make_project(db_session)
    v1 = await _make_verified_claim(db_session, project)
    first = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)

    second = await scripts_repo.create_script(
        db_session,
        project_id=project.id,
        llm_model=MODEL,
        draft=_validated_draft(_validated_segment(0, "Fresh take.", (v1.id,))),
    )

    assert (await scripts_repo.get_by_id(db_session, first.id)).status == "superseded"
    assert (await scripts_repo.get_active(db_session, project.id)).id == second.id


async def test_create_script_rolls_back_atomically_on_failure(db_session):
    project = await _make_project(db_session)
    incumbent = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)

    # A validated draft whose citation points at a verification that does
    # not exist - the script_segment_claims FK rejects the insert.
    doomed = _validated_draft(_validated_segment(0, "Cites a ghost.", (uuid4(),)))

    with pytest.raises(ConflictError):
        await scripts_repo.create_script(
            db_session, project_id=project.id, llm_model=MODEL, draft=doomed
        )

    # Nothing partial survived: the incumbent is still active and un-superseded,
    # and no new script row exists.
    assert (await scripts_repo.get_active(db_session, project.id)).id == incumbent.id
    page = await scripts_repo.list_by_project(db_session, project.id)
    assert [s.id for s in page.items] == [incumbent.id]


async def test_create_script_without_supersede_conflicts_on_the_active_slot(db_session):
    project = await _make_project(db_session)
    v1 = await _make_verified_claim(db_session, project)
    await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)

    with pytest.raises(ConflictError):
        await scripts_repo.create_script(
            db_session,
            project_id=project.id,
            llm_model=MODEL,
            draft=_validated_draft(_validated_segment(0, "Second active.", (v1.id,))),
            supersede=False,
        )
