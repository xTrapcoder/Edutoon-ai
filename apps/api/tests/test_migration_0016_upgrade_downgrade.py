"""Migration 0016 upgraded and downgraded for real, with Alembic, on a scratch database
(mirrors ``test_migration_0015_upgrade_downgrade.py``).

0016 is purely additive - three new tables - so there is no existing-data transformation to
prove; what matters is that the upgrade / downgrade / upgrade journey is clean and reversible,
and that the finished schema enforces what ``repositories.render`` relies on: unique chunk
fingerprints per project, CHECK-constrained vocabularies (never Postgres enums), one current
output per script and configuration, and cascades that remove every render row with its project.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.exc import IntegrityError

from tests.migration_helpers import add_owner, add_project, run_alembic, scratch_database, sql

TABLES = {"render_chunks", "render_outputs", "render_output_scenes"}


@pytest.fixture
def scratch_url() -> Iterator[str]:
    with scratch_database() as url:
        yield url


def _tables(url: str) -> set[str]:
    rows = sql(url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    return {row["tablename"] for row in rows}


def _project_id(url: str, owner: str, title: str) -> str:
    add_project(url, owner, title)
    [project] = sql(url, "SELECT id FROM projects WHERE title = :t", t=title)
    return str(project["id"])


def _script_id(url: str, project_id: str) -> str:
    [script] = sql(
        url,
        "INSERT INTO scripts (project_id, status, llm_model) VALUES (:p, 'approved', 'm') "
        "RETURNING id",
        p=project_id,
    )
    return str(script["id"])


def _add_chunk(url: str, project_id: str, chunk_hash: str, **overrides: object) -> None:
    values = {
        "p": project_id,
        "h": chunk_hash,
        "o": "horizontal",
        "q": "low",
        "b": 1000,
        "f": 30,
        "d": 1000,
    } | overrides
    sql(
        url,
        "INSERT INTO render_chunks (project_id, chunk_hash, orientation, quality, "
        "renderer_version, storage_bucket, storage_key, byte_size, frame_count, duration_ms) "
        "VALUES (:p, :h, :o, :q, 'v1', 'b', 'projects/x/k', :b, :f, :d)",
        **values,
    )


def _add_output(url: str, project_id: str, script_id: str, **overrides: object) -> str:
    values = {"o": "horizontal", "q": "low", "e": "libx264", "d": 5000} | overrides
    [row] = sql(
        url,
        "INSERT INTO render_outputs (project_id, script_id, orientation, quality, output_hash, "
        "renderer_version, encoder, fps, width, height, duration_ms, byte_size, storage_bucket, "
        "storage_key, probe) VALUES (:p, :s, :o, :q, 'h', 'v1', :e, 30, 960, 540, :d, 10, 'b', "
        "'projects/x/o', '{}'::jsonb) RETURNING id",
        p=project_id,
        s=script_id,
        **values,
    )
    return str(row["id"])


def test_upgrade_creates_the_tables_and_downgrade_removes_only_them(scratch_url):
    run_alembic(scratch_url, "upgrade", "0015")
    assert not TABLES & _tables(scratch_url)

    run_alembic(scratch_url, "upgrade", "0016")
    assert _tables(scratch_url) >= TABLES
    assert run_alembic(scratch_url, "current").count("0016") == 1

    run_alembic(scratch_url, "downgrade", "0015")
    assert not TABLES & _tables(scratch_url)
    assert "script_segment_scenes" in _tables(scratch_url)  # 0015 is untouched

    run_alembic(scratch_url, "upgrade", "head")
    assert _tables(scratch_url) >= TABLES


def test_a_chunk_fingerprint_is_unique_per_project_but_shared_across_projects(scratch_url):
    run_alembic(scratch_url, "upgrade", "head")
    owner = add_owner(scratch_url)
    first = _project_id(scratch_url, owner, "first")
    second = _project_id(scratch_url, owner, "second")

    _add_chunk(scratch_url, first, "abc")
    _add_chunk(scratch_url, second, "abc")  # another project may hold the same fingerprint
    with pytest.raises(IntegrityError):
        _add_chunk(scratch_url, first, "abc")


@pytest.mark.parametrize(
    "overrides",
    [
        {"o": "square"},
        {"q": "ultra"},
        {"b": 0},
        {"f": 0},
        {"d": 0},
    ],
)
def test_chunk_columns_are_check_constrained_not_free_text(scratch_url, overrides):
    run_alembic(scratch_url, "upgrade", "head")
    project = _project_id(scratch_url, add_owner(scratch_url), "p")
    with pytest.raises(IntegrityError):
        _add_chunk(scratch_url, project, "abc", **overrides)


@pytest.mark.parametrize("overrides", [{"o": "square"}, {"q": "ultra"}, {"e": "libx265"}, {"d": 0}])
def test_output_columns_are_check_constrained_not_free_text(scratch_url, overrides):
    run_alembic(scratch_url, "upgrade", "head")
    project = _project_id(scratch_url, add_owner(scratch_url), "p")
    script = _script_id(scratch_url, project)
    with pytest.raises(IntegrityError):
        _add_output(scratch_url, project, script, **overrides)


def test_there_is_at_most_one_output_per_script_and_configuration(scratch_url):
    run_alembic(scratch_url, "upgrade", "head")
    project = _project_id(scratch_url, add_owner(scratch_url), "p")
    script = _script_id(scratch_url, project)
    _add_output(scratch_url, project, script)
    _add_output(scratch_url, project, script, q="high")  # another quality is a different output
    _add_output(scratch_url, project, script, o="vertical")
    with pytest.raises(IntegrityError):
        _add_output(scratch_url, project, script)


def test_deleting_a_project_removes_every_render_row(scratch_url):
    run_alembic(scratch_url, "upgrade", "head")
    project = _project_id(scratch_url, add_owner(scratch_url), "p")
    script = _script_id(scratch_url, project)
    _add_chunk(scratch_url, project, "abc")
    output = _add_output(scratch_url, project, script)
    [segment] = sql(
        scratch_url,
        "INSERT INTO script_segments (script_id, segment_index, narration_text, "
        "estimated_duration_ms) VALUES (:s, 0, 'Hi.', 1000) RETURNING id",
        s=script,
    )
    sql(
        scratch_url,
        "INSERT INTO render_output_scenes (render_output_id, position, script_segment_id, "
        "scene_index, chunk_hash) VALUES (:o, 0, :g, 0, 'abc')",
        o=output,
        g=segment["id"],
    )

    sql(scratch_url, "DELETE FROM projects WHERE id = :p", p=project)

    for table in TABLES:
        assert sql(scratch_url, f"SELECT count(*) AS n FROM {table}")[0]["n"] == 0


def test_an_output_scene_position_is_unique_within_its_output(scratch_url):
    run_alembic(scratch_url, "upgrade", "head")
    project = _project_id(scratch_url, add_owner(scratch_url), "p")
    script = _script_id(scratch_url, project)
    output = _add_output(scratch_url, project, script)
    [segment] = sql(
        scratch_url,
        "INSERT INTO script_segments (script_id, segment_index, narration_text, "
        "estimated_duration_ms) VALUES (:s, 0, 'Hi.', 1000) RETURNING id",
        s=script,
    )
    insert = (
        "INSERT INTO render_output_scenes (render_output_id, position, script_segment_id, "
        "scene_index, chunk_hash) VALUES (:o, 0, :g, 0, 'abc')"
    )
    sql(scratch_url, insert, o=output, g=segment["id"])
    with pytest.raises(IntegrityError):
        sql(scratch_url, insert, o=output, g=segment["id"])
