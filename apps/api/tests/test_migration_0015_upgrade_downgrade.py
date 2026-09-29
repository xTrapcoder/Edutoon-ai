"""Migration 0015 upgraded and downgraded for real, with Alembic, on a scratch
database (mirrors ``test_migration_0010_upgrade_downgrade.py``).

Unlike 0010, 0015 is purely additive - a new table plus a widened
``ck_jobs_kind`` - so there is no existing-data transformation to prove;
what matters here is that the upgrade/downgrade/upgrade journey itself is
clean and reversible, and that the finished schema enforces exactly what
``repositories.script_segment_scenes`` relies on.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.exc import IntegrityError

from tests.migration_helpers import add_owner, add_project, run_alembic, scratch_database, sql

_TEMPLATES = (
    "title_card",
    "bullet_points",
    "labelled_diagram",
    "split_text_illustration",
    "full_bleed_illustration",
    "formula_or_code",
)


@pytest.fixture
def scratch_url() -> Iterator[str]:
    with scratch_database() as url:
        yield url


def _tables(url: str) -> set[str]:
    rows = sql(url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    return {row["tablename"] for row in rows}


def _jobs_kind_check(url: str) -> str:
    [row] = sql(
        url,
        "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
        "WHERE conname = 'ck_jobs_kind' AND conrelid = 'public.jobs'::regclass",
    )
    return row["def"]


def _add_project_id(url: str, owner: str, title: str) -> str:
    add_project(url, owner, title)
    [project] = sql(url, "SELECT id FROM projects WHERE title = :t", t=title)
    return str(project["id"])


def _add_segment_id(url: str, owner: str, title: str) -> str:
    project_id = _add_project_id(url, owner, title)
    [script] = sql(
        url,
        "INSERT INTO scripts (project_id, status, llm_model) "
        "VALUES (:p, 'approved', 'm') RETURNING id",
        p=project_id,
    )
    [segment] = sql(
        url,
        "INSERT INTO script_segments (script_id, segment_index, narration_text, "
        "estimated_duration_ms) VALUES (:s, 0, 'Hi.', 1000) RETURNING id",
        s=script["id"],
    )
    return str(segment["id"])


def test_the_migrations_form_a_single_line_that_upgrades_to_head(scratch_url):
    run_alembic(scratch_url, "upgrade", "head")

    assert run_alembic(scratch_url, "current").count("(head)") == 1
    assert run_alembic(scratch_url, "heads").count("(head)") == 1


def test_upgrade_creates_the_table_and_widens_job_kind(scratch_url):
    run_alembic(scratch_url, "upgrade", "0014")
    assert "script_segment_scenes" not in _tables(scratch_url)
    assert "generate_storyboard" not in _jobs_kind_check(scratch_url)

    run_alembic(scratch_url, "upgrade", "0015")

    assert "script_segment_scenes" in _tables(scratch_url)
    assert "generate_storyboard" in _jobs_kind_check(scratch_url)
    owner = add_owner(scratch_url)
    project_id = _add_project_id(scratch_url, owner, "P")
    sql(
        scratch_url,
        "INSERT INTO jobs (project_id, kind) VALUES (:p, 'generate_storyboard')",
        p=project_id,
    )


def test_the_new_table_accepts_every_documented_template(scratch_url):
    run_alembic(scratch_url, "upgrade", "0015")
    owner = add_owner(scratch_url)
    segment_id = _add_segment_id(scratch_url, owner, "P")

    for index, template in enumerate(_TEMPLATES):
        sql(
            scratch_url,
            "INSERT INTO script_segment_scenes (script_segment_id, scene_index, start_ms, "
            "duration_ms, template, content, source_audio_content_hash) "
            "VALUES (:seg, :idx, :idx * 1000, 1000, :template, '{}'::jsonb, 'h')",
            seg=segment_id,
            idx=index,
            template=template,
        )

    rows = sql(
        scratch_url,
        "SELECT template FROM script_segment_scenes "
        "WHERE script_segment_id = :s ORDER BY scene_index",
        s=segment_id,
    )
    assert [row["template"] for row in rows] == list(_TEMPLATES)


def test_the_new_table_rejects_an_unknown_template(scratch_url):
    run_alembic(scratch_url, "upgrade", "0015")
    owner = add_owner(scratch_url)
    segment_id = _add_segment_id(scratch_url, owner, "P")

    with pytest.raises(IntegrityError, match="ck_script_segment_scenes_template"):
        sql(
            scratch_url,
            "INSERT INTO script_segment_scenes (script_segment_id, scene_index, start_ms, "
            "duration_ms, template, content, source_audio_content_hash) "
            "VALUES (:seg, 0, 0, 1000, 'section_recap', '{}'::jsonb, 'h')",
            seg=segment_id,
        )


def test_the_new_table_rejects_a_non_positive_duration(scratch_url):
    run_alembic(scratch_url, "upgrade", "0015")
    owner = add_owner(scratch_url)
    segment_id = _add_segment_id(scratch_url, owner, "P")

    with pytest.raises(IntegrityError, match="ck_script_segment_scenes_duration_ms_positive"):
        sql(
            scratch_url,
            "INSERT INTO script_segment_scenes (script_segment_id, scene_index, start_ms, "
            "duration_ms, template, content, source_audio_content_hash) "
            "VALUES (:seg, 0, 0, 0, 'title_card', '{}'::jsonb, 'h')",
            seg=segment_id,
        )


def test_deleting_the_segment_cascades_to_its_scenes(scratch_url):
    run_alembic(scratch_url, "upgrade", "0015")
    owner = add_owner(scratch_url)
    segment_id = _add_segment_id(scratch_url, owner, "P")
    sql(
        scratch_url,
        "INSERT INTO script_segment_scenes (script_segment_id, scene_index, start_ms, "
        "duration_ms, template, content, source_audio_content_hash) "
        "VALUES (:seg, 0, 0, 1000, 'title_card', '{}'::jsonb, 'h')",
        seg=segment_id,
    )

    sql(scratch_url, "DELETE FROM script_segments WHERE id = :s", s=segment_id)

    assert sql(scratch_url, "SELECT id FROM script_segment_scenes") == []


def test_downgrade_removes_the_table_and_narrows_job_kind(scratch_url):
    run_alembic(scratch_url, "upgrade", "0015")

    run_alembic(scratch_url, "downgrade", "0014")

    assert "script_segment_scenes" not in _tables(scratch_url)
    assert "generate_storyboard" not in _jobs_kind_check(scratch_url)
    owner = add_owner(scratch_url)
    project_id = _add_project_id(scratch_url, owner, "P")
    with pytest.raises(IntegrityError, match="ck_jobs_kind"):
        sql(
            scratch_url,
            "INSERT INTO jobs (project_id, kind) VALUES (:p, 'generate_storyboard')",
            p=project_id,
        )


def test_the_migration_round_trips(scratch_url):
    """upgrade -> downgrade -> upgrade lands on exactly the same schema."""
    run_alembic(scratch_url, "upgrade", "0015")
    tables_before = _tables(scratch_url)
    jobs_kind_before = _jobs_kind_check(scratch_url)

    run_alembic(scratch_url, "downgrade", "0014")
    run_alembic(scratch_url, "upgrade", "0015")

    assert _tables(scratch_url) == tables_before
    assert _jobs_kind_check(scratch_url) == jobs_kind_before
    # And the round-tripped schema still works end to end.
    owner = add_owner(scratch_url)
    segment_id = _add_segment_id(scratch_url, owner, "P")
    sql(
        scratch_url,
        "INSERT INTO script_segment_scenes (script_segment_id, scene_index, start_ms, "
        "duration_ms, template, content, source_audio_content_hash) "
        "VALUES (:seg, 0, 0, 1000, 'title_card', '{}'::jsonb, 'h')",
        seg=segment_id,
    )
    assert len(sql(scratch_url, "SELECT id FROM script_segment_scenes")) == 1
