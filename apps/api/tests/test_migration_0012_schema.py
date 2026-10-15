"""Migration 0012's schema, checked against the live catalogue: same style as
``test_migration_0009_schema.py`` - raw SQL, going around the repositories'
``IntegrityError`` -> ``ConflictError`` translation so each rule is proven to
live in the database itself.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import users as users_repo


async def _make_project(db_session):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    return await projects_repo.create(db_session, owner_id=owner.id, title="P", source_type="topic")


async def _column(db_session, table: str, column: str):
    row = (
        await db_session.execute(
            text(
                "SELECT data_type, udt_name, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        )
    ).one_or_none()
    assert row is not None, f"{table}.{column} does not exist"
    return row


# --- claim_verifications.topic_id -----------------------------------------------------


async def test_topic_id_is_a_nullable_uuid_column(db_session):
    column = await _column(db_session, "claim_verifications", "topic_id")

    assert column.data_type == "uuid"
    assert column.is_nullable == "YES"


async def test_topic_id_foreign_key_sets_null_on_delete_not_restrict(db_session):
    """The documented difference from ``claim_citations``: a claim's own
    verdict outlives the topic it was extracted for.
    """
    row = (
        await db_session.execute(
            text(
                "SELECT c.confdeltype::text AS confdeltype, r.relname AS referenced "
                "FROM pg_constraint c JOIN pg_class r ON r.oid = c.confrelid "
                "WHERE c.conname = 'fk_claim_verifications_topic_id_project_topics' "
                "AND c.contype = 'f'"
            )
        )
    ).one_or_none()

    assert row is not None, "missing foreign key fk_claim_verifications_topic_id_project_topics"
    assert row.confdeltype == "n"  # SET NULL
    assert row.referenced == "project_topics"


async def test_topic_id_index_exists(db_session):
    rows = (
        (
            await db_session.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname = 'public' AND tablename = 'claim_verifications'"
                )
            )
        )
        .scalars()
        .all()
    )

    assert "ix_claim_verifications_topic_id" in rows


async def test_topic_id_rejects_a_reference_to_a_missing_topic(db_session):
    project = await _make_project(db_session)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO claim_verifications "
                    "(project_id, claim_text, status, embedding_model, llm_model, topic_id) "
                    "VALUES (:p, 'A claim.', 'unsupported', 'e', 'l', :t)"
                ),
                {"p": project.id, "t": uuid4()},
            )


async def test_deleting_a_topic_sets_topic_id_to_null_rather_than_failing(db_session):
    project = await _make_project(db_session)
    topic = await project_topics_repo.create(db_session, project_id=project.id, title="T")
    row = (
        await db_session.execute(
            text(
                "INSERT INTO claim_verifications "
                "(project_id, claim_text, status, embedding_model, llm_model, topic_id) "
                "VALUES (:p, 'A claim.', 'unsupported', 'e', 'l', :t) RETURNING id"
            ),
            {"p": project.id, "t": topic.id},
        )
    ).one()

    await db_session.execute(text("DELETE FROM project_topics WHERE id = :t"), {"t": topic.id})

    remaining = (
        await db_session.execute(
            text("SELECT topic_id FROM claim_verifications WHERE id = :i"), {"i": row.id}
        )
    ).one()
    assert remaining.topic_id is None


async def test_existing_style_insert_without_topic_id_still_works(db_session):
    """Backwards compatibility: an INSERT that predates migration 0012 (and so
    names no ``topic_id``) is still valid and defaults to NULL.
    """
    project = await _make_project(db_session)

    row = (
        await db_session.execute(
            text(
                "INSERT INTO claim_verifications "
                "(project_id, claim_text, status, embedding_model, llm_model) "
                "VALUES (:p, 'Legacy claim.', 'unsupported', 'e', 'l') RETURNING topic_id"
            ),
            {"p": project.id},
        )
    ).one()

    assert row.topic_id is None


# --- jobs.kind widened to include extract_claims --------------------------------------


async def test_jobs_kind_check_accepts_extract_claims(db_session):
    project = await _make_project(db_session)

    await db_session.execute(
        text("INSERT INTO jobs (project_id, kind) VALUES (:p, 'extract_claims')"),
        {"p": project.id},
    )


@pytest.mark.parametrize(
    "kind",
    [
        "parse_source",
        "extract_topics",
        "build_evidence",
        "generate_script",
        "synthesise_voice",
        "render_video",
    ],
)
async def test_jobs_kind_check_still_accepts_every_original_value(db_session, kind):
    project = await _make_project(db_session)

    await db_session.execute(
        text("INSERT INTO jobs (project_id, kind) VALUES (:p, :k)"), {"p": project.id, "k": kind}
    )


async def test_jobs_kind_check_still_rejects_an_unknown_value(db_session):
    project = await _make_project(db_session)

    with pytest.raises(IntegrityError) as excinfo:
        async with db_session.begin_nested():
            await db_session.execute(
                text("INSERT INTO jobs (project_id, kind) VALUES (:p, 'not_a_real_kind')"),
                {"p": project.id},
            )

    assert "ck_jobs_kind" in str(excinfo.value)
