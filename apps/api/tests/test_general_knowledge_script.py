"""A topic-only lesson (``source_type: topic``, no source material) end to end through script
generation: its claims are the model's general knowledge, so the script may rest on them *only
there*, opens with the fixed spoken notice, and is recorded as a general-knowledge lesson. A project
with source material never takes this path."""

from __future__ import annotations

from uuid import uuid4

import pytest

from edutoon.core.errors import ValidationError
from edutoon.domain import general_knowledge as gk
from edutoon.repositories import claim_verifications as claim_verifications_repo
from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories import users as users_repo
from edutoon.services import scripts as scripts_service
from tests.test_services_scripts import (
    SCRIPT_MODEL,
    _add_supported_claim,
    _mock_llm,
    _segment,
)

pytestmark = pytest.mark.provider_mocked

CLAIM = (
    "Machine learning lets a computer find patterns in examples instead of following fixed rules."
)


async def _topic_project(db_session):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="Machine learning", source_type="topic"
    )
    topic = await project_topics_repo.create(
        db_session, project_id=project.id, title="Machine learning", status="accepted", position=0
    )
    claim = await claim_verifications_repo.create(
        db_session,
        project_id=project.id,
        claim_text=CLAIM,
        status=gk.STATUS,
        embedding_model=gk.EMBEDDING_MODEL_NONE,
        llm_model=SCRIPT_MODEL,
        rationale=gk.RATIONALE,
        topic_id=topic.id,
    )
    return owner, project, topic, claim


async def test_a_topic_only_lesson_is_written_from_general_knowledge_and_opens_with_the_notice(
    db_session, monkeypatch
):
    owner, project, topic, claim = await _topic_project(db_session)
    enq = await scripts_service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="gk-1"
    )
    llm, _outline, section = _mock_llm(
        monkeypatch,
        sections_by_topic={
            topic.id: [
                [
                    _segment(
                        "Machine learning lets a computer find patterns in examples.",
                        [str(claim.id)],
                    )
                ]
            ]
        },
    )

    job = await scripts_service.run_generate_script_job(db_session, llm, job_id=enq.job.id)

    assert job.status == "succeeded"
    assert job.result["general_knowledge"] is True
    segments = await scripts_repo.list_segments(
        db_session, project_id=project.id, script_id=enq.script.id
    )
    assert segments[0].narration_text == gk.LESSON_NOTICE
    assert segments[0].topic_id == topic.id
    assert segments[1].narration_text.startswith("Machine learning lets a computer")
    cited = await scripts_repo.list_segment_claims(
        db_session, project_id=project.id, script_segment_id=segments[1].id
    )
    assert [c.claim_verification_id for c in cited] == [claim.id]
    # The writer is told what the claims are.
    brief = section.await_args.args[0]
    assert brief.general_knowledge is True


async def test_a_topic_only_lesson_is_refused_when_none_of_its_topics_has_a_claim(db_session):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="Machine learning", source_type="topic"
    )
    await project_topics_repo.create(
        db_session, project_id=project.id, title="Machine learning", status="accepted", position=0
    )

    with pytest.raises(ValidationError, match="no verified evidence yet"):
        await scripts_service.enqueue_script_generation(
            db_session, project_id=project.id, owner_id=owner.id, idempotency_key="gk-2"
        )


async def test_a_project_with_source_material_never_writes_from_general_knowledge(db_session):
    """A keyword project that has since gained material verifies against it: a stray
    general-knowledge claim is not enough to write a script from."""
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="Machine learning", source_type="topic"
    )
    topic = await project_topics_repo.create(
        db_session, project_id=project.id, title="Machine learning", status="accepted", position=0
    )
    await claim_verifications_repo.create(
        db_session,
        project_id=project.id,
        claim_text=CLAIM,
        status=gk.STATUS,
        embedding_model=gk.EMBEDDING_MODEL_NONE,
        llm_model=SCRIPT_MODEL,
        topic_id=topic.id,
    )
    other = await project_topics_repo.create(
        db_session, project_id=project.id, title="Other", status="rejected", position=1
    )
    await _add_supported_claim(db_session, project, other, "An unrelated verified fact.")

    with pytest.raises(ValidationError, match="no verified evidence yet"):
        await scripts_service.enqueue_script_generation(
            db_session, project_id=project.id, owner_id=owner.id, idempotency_key="gk-3"
        )


async def test_the_flag_off_means_no_general_knowledge_even_for_a_topic_project(
    db_session, monkeypatch
):
    from edutoon.core.config import get_settings

    monkeypatch.setenv("KEYWORD_ONLY_PROJECTS_ENABLED", "false")
    get_settings.cache_clear()
    try:
        owner, project, _topic, _claim = await _topic_project(db_session)
        with pytest.raises(ValidationError, match="no verified evidence yet"):
            await scripts_service.enqueue_script_generation(
                db_session, project_id=project.id, owner_id=owner.id, idempotency_key="gk-4"
            )
    finally:
        get_settings.cache_clear()
