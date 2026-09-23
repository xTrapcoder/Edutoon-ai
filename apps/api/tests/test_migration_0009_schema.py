"""Migration 0009's schema, checked against the live catalogue.

The repositories deliberately turn any ``IntegrityError`` into a
``ConflictError``, which hides *which* constraint fired. These tests go around
the repositories with raw SQL so each rule is proven to live in the database
itself - not merely in application code that a future caller might skip.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from edutoon.repositories import projects as projects_repo
from edutoon.repositories import users as users_repo


async def _make_project(db_session):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    return await projects_repo.create(
        db_session, owner_id=owner.id, title="P", source_type="topic"
    )


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


# --- project_topics.origin ---------------------------------------------------------


async def test_origin_is_a_text_column_defaulting_to_user_and_never_null(db_session):
    column = await _column(db_session, "project_topics", "origin")

    assert column.data_type == "text"
    assert column.is_nullable == "NO"
    assert "'user'" in column.column_default


async def test_origin_is_not_a_postgres_enum(db_session):
    """Rule 7: enumerations are text + CHECK. No enum type may exist in the
    schema at all, so neither this migration nor a future one can slip one in.
    """
    enum_types = (
        await db_session.execute(
            text(
                "SELECT t.typname FROM pg_type t "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE t.typtype = 'e' AND n.nspname = 'public'"
            )
        )
    ).scalars().all()

    assert enum_types == []


@pytest.mark.parametrize("origin", ["source", "user", "suggested"])
async def test_origin_check_accepts_each_allowed_value(db_session, origin):
    project = await _make_project(db_session)

    await db_session.execute(
        text("INSERT INTO project_topics (project_id, title, origin) VALUES (:p, 'T', :o)"),
        {"p": project.id, "o": origin},
    )


@pytest.mark.parametrize("origin", ["", "User", "SOURCE", "model", "hallucinated"])
async def test_origin_check_rejects_anything_else(db_session, origin):
    project = await _make_project(db_session)

    with pytest.raises(IntegrityError) as excinfo:
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO project_topics (project_id, title, origin) "
                    "VALUES (:p, 'T', :o)"
                ),
                {"p": project.id, "o": origin},
            )

    assert "ck_project_topics_origin" in str(excinfo.value)


async def test_existing_style_insert_without_origin_still_works_and_is_a_user_topic(db_session):
    """Backwards compatibility: an INSERT that predates migration 0009 (and so
    names neither new column) is still valid.
    """
    project = await _make_project(db_session)

    row = (
        await db_session.execute(
            text(
                "INSERT INTO project_topics (project_id, title) VALUES (:p, 'Legacy') "
                "RETURNING origin, keywords"
            ),
            {"p": project.id},
        )
    ).one()

    assert row.origin == "user"
    assert row.keywords == []


# --- project_topics.keywords ---------------------------------------------------------


async def test_keywords_is_a_native_text_array_defaulting_to_empty_and_never_null(db_session):
    column = await _column(db_session, "project_topics", "keywords")

    assert column.data_type == "ARRAY"
    assert column.udt_name == "_text"
    assert column.is_nullable == "NO"
    assert "'{}'" in column.column_default


async def test_keywords_rejects_a_null_column_value(db_session):
    project = await _make_project(db_session)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO project_topics (project_id, title, keywords) "
                    "VALUES (:p, 'T', NULL)"
                ),
                {"p": project.id},
            )


async def test_keywords_rejects_a_null_element(db_session):
    project = await _make_project(db_session)

    with pytest.raises(IntegrityError) as excinfo:
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO project_topics (project_id, title, keywords) "
                    "VALUES (:p, 'T', ARRAY['ok', NULL]::text[])"
                ),
                {"p": project.id},
            )

    assert "ck_project_topics_keywords_no_null_elements" in str(excinfo.value)


async def test_keywords_accepts_an_empty_array_and_a_populated_one(db_session):
    project = await _make_project(db_session)

    await db_session.execute(
        text(
            "INSERT INTO project_topics (project_id, title, position, keywords) VALUES "
            "(:p, 'Empty', 0, '{}'), (:p, 'Full', 1, ARRAY['a', 'b'])"
        ),
        {"p": project.id},
    )


# --- topic_citations -------------------------------------------------------------------


async def test_topic_citations_has_the_expected_columns(db_session):
    id_column = await _column(db_session, "topic_citations", "id")
    assert id_column.data_type == "uuid"
    assert "uuid_generate_v7" in id_column.column_default

    for name, data_type in [
        ("topic_id", "uuid"),
        ("source_chunk_id", "uuid"),
        ("created_at", "timestamp with time zone"),
    ]:
        column = await _column(db_session, "topic_citations", name)
        assert column.data_type == data_type
        assert column.is_nullable == "NO"


@pytest.mark.parametrize(
    ("constraint", "referenced_table"),
    [
        ("fk_topic_citations_topic_id_project_topics", "project_topics"),
        ("fk_topic_citations_source_chunk_id_source_chunks", "source_chunks"),
    ],
)
async def test_topic_citations_foreign_keys_cascade_on_delete(
    db_session, constraint, referenced_table
):
    """Both FKs are ON DELETE CASCADE (confdeltype 'c'), not RESTRICT - the
    documented difference from claim_citations.
    """
    row = (
        await db_session.execute(
            text(
                "SELECT c.confdeltype::text AS confdeltype, r.relname AS referenced "
                "FROM pg_constraint c JOIN pg_class r ON r.oid = c.confrelid "
                "WHERE c.conname = :name AND c.contype = 'f'"
            ),
            {"name": constraint},
        )
    ).one_or_none()

    assert row is not None, f"missing foreign key {constraint}"
    assert row.confdeltype == "c"
    assert row.referenced == referenced_table


async def test_topic_citations_indexes_exist_and_the_pair_is_unique(db_session):
    rows = (
        await db_session.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = 'topic_citations'"
            )
        )
    ).all()
    indexes = {row.indexname: row.indexdef for row in rows}

    assert "ix_topic_citations_topic_id" in indexes
    assert "ix_topic_citations_source_chunk_id" in indexes
    assert "UNIQUE" in indexes["uq_topic_citations_topic_id_source_chunk_id"]
    assert "(topic_id, source_chunk_id)" in indexes["uq_topic_citations_topic_id_source_chunk_id"]


async def test_topic_citations_rejects_a_reference_to_a_missing_topic_or_chunk(db_session):
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO topic_citations (topic_id, source_chunk_id) "
                    "VALUES (:t, :c)"
                ),
                {"t": uuid4(), "c": uuid4()},
            )
