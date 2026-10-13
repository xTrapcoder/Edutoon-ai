"""Migration 0020 (training courses, modules, enrolments) upgraded and downgraded for real, with
Alembic, on a scratch database (mirrors ``test_migration_0016_upgrade_downgrade.py``).

0020 is purely additive - three new tables - so there is no existing-data transformation to prove.
What matters: the upgrade -> downgrade -> upgrade journey is clean and leaves everything that was
already there exactly as it was; and the finished schema enforces what the training code will rely
on - a module cannot exist outside a course, a module's video key is its own and never a project's,
the same render cannot be published twice into a course, a published video survives its source
render and project, and enumerations are CHECK-constrained text, never Postgres enums.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from tests.migration_helpers import add_owner, add_project, run_alembic, scratch_database, sql

TABLES = {"training_courses", "training_modules", "course_enrolments"}
UNPUBLISHED_AT = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)


@pytest.fixture
def scratch_url() -> Iterator[str]:
    with scratch_database() as url:
        yield url


@pytest.fixture
def head_url(scratch_url) -> str:
    run_alembic(scratch_url, "upgrade", "head", env={"ADMIN_BOOTSTRAP_EMAILS": ""})
    return scratch_url


def _tables(url: str) -> set[str]:
    rows = sql(url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    return {row["tablename"] for row in rows}


# --- helpers: the smallest valid rows --------------------------------------------------------


def _course(url: str, owner: Any, **overrides: Any) -> str:
    values = {"t": "Safety", "s": "draft", "o": owner} | overrides
    [row] = sql(
        url,
        "INSERT INTO training_courses (title, status, created_by) VALUES (:t, :s, :o) RETURNING id",
        **values,
    )
    return str(row["id"])


def _render_output(url: str, owner: Any, title: str = "Source") -> tuple[str, str]:
    """A project, an approved script and its render output; returns (project_id, output_id)."""
    add_project(url, owner, title)
    [project] = sql(url, "SELECT id FROM projects WHERE title = :t", t=title)
    [script] = sql(
        url,
        "INSERT INTO scripts (project_id, status, llm_model) VALUES (:p, 'approved', 'm') "
        "RETURNING id",
        p=project["id"],
    )
    [output] = sql(
        url,
        "INSERT INTO render_outputs (project_id, script_id, orientation, quality, output_hash, "
        "renderer_version, encoder, fps, width, height, duration_ms, byte_size, storage_bucket, "
        "storage_key, probe) VALUES (:p, :s, 'horizontal', 'high', 'h', 'v1', 'libx264', 30, "
        "1280, 720, 5000, 10, 'b', 'projects/x/o', '{}'::jsonb) RETURNING id",
        p=project["id"],
        s=script["id"],
    )
    return str(project["id"]), str(output["id"])


def _module(url: str, course: str, owner: Any, **overrides: Any) -> str:
    """A published module with a module-owned key unless told otherwise. Override any column by
    its SQL name; ``video_key=None`` plus ``status='unpublished'`` makes an unpublished one."""
    module_id = overrides.pop("id", str(uuid4()))
    unpublished = overrides.get("status") == "unpublished"
    values: dict[str, Any] = {
        "id": module_id,
        "course_id": course,
        "position": 1,
        "title": "Module",
        "status": "published",
        "source_render_output_id": None,
        "video_bucket": None if unpublished else "outputs",
        "video_key": None if unpublished else f"training/modules/{module_id}/{uuid4()}.mp4",
        "video_byte_size": 1000,
        "video_duration_ms": 5000,
        "video_width": 1280,
        "video_height": 720,
        "video_fps": 30,
        "video_output_hash": "h",
        "published_by": owner,
    } | overrides
    if unpublished and "unpublished_at" not in values:
        values["unpublished_at"] = UNPUBLISHED_AT
    names = list(values)
    placeholders = [f":{n}" for n in names]
    sql(
        url,
        f"INSERT INTO training_modules ({', '.join(names)}) VALUES ({', '.join(placeholders)})",
        **values,
    )
    return str(module_id)


def _enrol(url: str, course: str, user: Any, assigned_by: Any, **overrides: Any) -> None:
    values = {"c": course, "u": user, "a": assigned_by, "s": "active"} | overrides
    sql(
        url,
        "INSERT INTO course_enrolments (course_id, user_id, assigned_by, status) "
        "VALUES (:c, :u, :a, :s)",
        **values,
    )


def _count(url: str, table: str) -> int:
    return int(sql(url, f"SELECT count(*) AS n FROM {table}")[0]["n"])


# --- the journey -----------------------------------------------------------------------------


def test_upgrade_creates_the_tables_and_downgrade_removes_only_them(scratch_url):
    run_alembic(scratch_url, "upgrade", "0019", env={"ADMIN_BOOTSTRAP_EMAILS": ""})
    assert not TABLES & _tables(scratch_url)

    run_alembic(scratch_url, "upgrade", "0020")
    assert _tables(scratch_url) >= TABLES
    assert run_alembic(scratch_url, "current").count("0020") == 1

    run_alembic(scratch_url, "downgrade", "0019")
    assert not TABLES & _tables(scratch_url)
    assert {"invitations", "render_outputs", "users"} <= _tables(scratch_url)  # 0019 untouched
    assert run_alembic(scratch_url, "current").count("0019") == 1

    run_alembic(scratch_url, "upgrade", "head")
    assert _tables(scratch_url) >= TABLES


def test_the_round_trip_leaves_existing_users_projects_and_renders_exactly_as_they_were(
    scratch_url,
):
    run_alembic(scratch_url, "upgrade", "0019", env={"ADMIN_BOOTSTRAP_EMAILS": ""})
    owner = add_owner(scratch_url)
    project_id, output_id = _render_output(scratch_url, owner)
    queries = {
        "users": "SELECT id, email, role, status FROM users ORDER BY id",
        "projects": "SELECT id, owner_id, title, source_type, status FROM projects ORDER BY id",
        "outputs": "SELECT * FROM render_outputs ORDER BY id",
    }
    before = {name: sql(scratch_url, query) for name, query in queries.items()}
    assert before["outputs"] and str(before["outputs"][0]["id"]) == output_id

    run_alembic(scratch_url, "upgrade", "0020")
    run_alembic(scratch_url, "downgrade", "0019")
    run_alembic(scratch_url, "upgrade", "0020")

    after = {name: sql(scratch_url, query) for name, query in queries.items()}
    assert after == before
    assert str(project_id) == str(before["projects"][0]["id"])


def test_a_downgrade_with_training_data_in_it_succeeds_and_drops_it(scratch_url):
    run_alembic(scratch_url, "upgrade", "head", env={"ADMIN_BOOTSTRAP_EMAILS": ""})
    owner = add_owner(scratch_url)
    course = _course(scratch_url, owner)
    _module(scratch_url, course, owner)
    _enrol(scratch_url, course, owner, owner)

    run_alembic(scratch_url, "downgrade", "0019")

    assert not TABLES & _tables(scratch_url)
    assert _count(scratch_url, "users") == 1  # the people are untouched


def test_no_postgres_enum_types_are_created(head_url):
    enums = sql(head_url, "SELECT typname FROM pg_type WHERE typtype = 'e'")

    assert enums == []


# --- courses ---------------------------------------------------------------------------------


@pytest.mark.parametrize("overrides", [{"s": "live"}, {"s": ""}, {"t": "   "}, {"t": ""}])
def test_course_columns_are_check_constrained(head_url, overrides):
    owner = add_owner(head_url)
    with pytest.raises(IntegrityError):
        _course(head_url, owner, **overrides)


def test_a_course_defaults_to_draft(head_url):
    owner = add_owner(head_url)
    [row] = sql(
        head_url,
        "INSERT INTO training_courses (title, created_by) VALUES ('x', :o) RETURNING status",
        o=owner,
    )

    assert row["status"] == "draft"


def test_updated_at_moves_on_update(head_url):
    owner = add_owner(head_url)
    course = _course(head_url, owner)
    [before] = sql(
        head_url, "SELECT created_at, updated_at FROM training_courses WHERE id = :i", i=course
    )

    sql(head_url, "UPDATE training_courses SET title = 'Renamed' WHERE id = :i", i=course)
    [after] = sql(head_url, "SELECT updated_at FROM training_courses WHERE id = :i", i=course)

    assert after["updated_at"] > before["updated_at"]


# --- modules: membership, ordering, vocabulary -----------------------------------------------


def test_a_module_cannot_exist_without_a_course(head_url):
    owner = add_owner(head_url)

    with pytest.raises(IntegrityError):
        _module(head_url, None, owner)  # NOT NULL
    with pytest.raises(IntegrityError):
        _module(head_url, str(uuid4()), owner)  # no such course


def test_deleting_a_course_deletes_its_modules_and_enrolments(head_url):
    owner = add_owner(head_url)
    learner = add_owner(head_url)
    course = _course(head_url, owner)
    other = _course(head_url, owner, t="Other")
    _module(head_url, course, owner)
    _module(head_url, other, owner)
    _enrol(head_url, course, learner, owner)
    _enrol(head_url, other, learner, owner)

    sql(head_url, "DELETE FROM training_courses WHERE id = :i", i=course)

    assert _count(head_url, "training_modules") == 1
    assert _count(head_url, "course_enrolments") == 1


def test_positions_are_unique_per_course_and_start_at_one(head_url):
    owner = add_owner(head_url)
    course = _course(head_url, owner)
    other = _course(head_url, owner, t="Other")
    _module(head_url, course, owner, position=1)

    with pytest.raises(IntegrityError):
        _module(head_url, course, owner, position=1)
    _module(head_url, other, owner, position=1)  # another course may reuse a position
    with pytest.raises(IntegrityError):
        _module(head_url, course, owner, position=0)
    with pytest.raises(IntegrityError):
        _module(head_url, course, owner, position=-1)


def test_two_modules_can_swap_positions_in_one_statement(head_url):
    """The position uniqueness is deferrable, so a reorder is one atomic statement."""
    owner = add_owner(head_url)
    course = _course(head_url, owner)
    first = _module(head_url, course, owner, position=1, title="A")
    second = _module(head_url, course, owner, position=2, title="B")

    sql(
        head_url,
        "UPDATE training_modules SET position = 3 - position WHERE course_id = :c",
        c=course,
    )

    rows = {
        str(r["id"]): r["position"]
        for r in sql(head_url, "SELECT id, position FROM training_modules")
    }
    assert rows == {first: 2, second: 1}


def test_a_swap_that_ends_with_a_duplicate_position_is_still_refused(head_url):
    owner = add_owner(head_url)
    course = _course(head_url, owner)
    _module(head_url, course, owner, position=1)
    _module(head_url, course, owner, position=2)

    with pytest.raises(IntegrityError):
        sql(head_url, "UPDATE training_modules SET position = 1 WHERE course_id = :c", c=course)


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "draft"},
        {"status": ""},
        {"title": "  "},
        {"video_byte_size": 0},
        {"video_duration_ms": 0},
        {"video_width": 0},
        {"video_height": 0},
        {"video_fps": 0},
    ],
)
def test_module_columns_are_check_constrained(head_url, overrides):
    owner = add_owner(head_url)
    course = _course(head_url, owner)
    with pytest.raises(IntegrityError):
        _module(head_url, course, owner, **overrides)


# --- modules: the video state and the module-owned key ---------------------------------------


def test_a_published_module_must_have_its_video(head_url):
    owner = add_owner(head_url)
    course = _course(head_url, owner)

    with pytest.raises(IntegrityError):
        _module(head_url, course, owner, video_bucket=None)
    with pytest.raises(IntegrityError):
        _module(head_url, course, owner, video_key=None, position=2)


def test_an_unpublished_module_has_no_video_file_but_keeps_its_description(head_url):
    owner = add_owner(head_url)
    course = _course(head_url, owner)

    module = _module(head_url, course, owner, status="unpublished")

    [row] = sql(head_url, "SELECT * FROM training_modules WHERE id = :i", i=module)
    assert row["video_key"] is None and row["video_bucket"] is None
    assert row["video_duration_ms"] == 5000 and row["video_width"] == 1280  # history kept


def test_an_unpublished_module_cannot_still_point_at_a_file_or_lack_its_timestamp(head_url):
    owner = add_owner(head_url)
    course = _course(head_url, owner)
    module_id = str(uuid4())

    with pytest.raises(IntegrityError):  # still has a key
        _module(
            head_url,
            course,
            owner,
            id=module_id,
            status="unpublished",
            video_bucket="outputs",
            video_key=f"training/modules/{module_id}/x.mp4",
            position=2,
        )
    with pytest.raises(IntegrityError):  # no unpublished_at
        _module(head_url, course, owner, status="unpublished", unpublished_at=None, position=3)


def test_a_published_module_cannot_carry_an_unpublished_timestamp(head_url):
    owner = add_owner(head_url)
    course = _course(head_url, owner)

    with pytest.raises(IntegrityError):
        _module(head_url, course, owner, unpublished_at=UNPUBLISHED_AT)


@pytest.mark.parametrize(
    "key",
    [
        "projects/00000000-0000-0000-0000-000000000000/renders/s/horizontal-high.mp4",
        "training/modules/not-this-module/x.mp4",
        "training/modules/",
        "other/x.mp4",
        "",
    ],
)
def test_a_modules_video_key_must_be_inside_its_own_prefix(head_url, key):
    owner = add_owner(head_url)
    course = _course(head_url, owner)

    with pytest.raises(IntegrityError):
        _module(head_url, course, owner, video_key=key)


def test_a_key_under_another_modules_prefix_is_refused(head_url):
    owner = add_owner(head_url)
    course = _course(head_url, owner)

    with pytest.raises(IntegrityError):
        _module(head_url, course, owner, video_key=f"training/modules/{uuid4()}/x.mp4")


def test_two_modules_can_never_share_one_stored_object(head_url):
    """A shared object would let deleting one module's video break the other's. The key check
    already ties a key to one module id; the unique index guards a module row duplicated by id
    mistakes - proven by reusing the same key under the same id on a second course."""
    owner = add_owner(head_url)
    course = _course(head_url, owner)
    module_id = str(uuid4())
    key = f"training/modules/{module_id}/same.mp4"
    _module(head_url, course, owner, id=module_id, video_key=key)

    with pytest.raises(IntegrityError):
        _module(
            head_url, course, owner, id=str(uuid4()), video_key=key, position=2
        )  # wrong owner id
    with pytest.raises(IntegrityError):
        _module(head_url, course, owner, id=module_id, video_key=key, position=2)  # same id


# --- modules: provenance, duplicates, snapshot isolation -------------------------------------


def test_the_same_render_cannot_be_published_twice_into_one_course(head_url):
    owner = add_owner(head_url)
    _, output = _render_output(head_url, owner)
    course = _course(head_url, owner)
    other = _course(head_url, owner, t="Other")
    _module(head_url, course, owner, position=1, source_render_output_id=output)

    with pytest.raises(IntegrityError):
        _module(head_url, course, owner, position=2, source_render_output_id=output)
    _module(head_url, other, owner, position=1, source_render_output_id=output)  # another course


def test_an_unpublished_module_does_not_block_publishing_the_same_render_again(head_url):
    owner = add_owner(head_url)
    _, output = _render_output(head_url, owner)
    course = _course(head_url, owner)
    _module(
        head_url, course, owner, position=1, status="unpublished", source_render_output_id=output
    )

    _module(head_url, course, owner, position=2, source_render_output_id=output)


def test_modules_without_a_known_source_do_not_collide(head_url):
    owner = add_owner(head_url)
    course = _course(head_url, owner)

    _module(head_url, course, owner, position=1, source_render_output_id=None)
    _module(head_url, course, owner, position=2, source_render_output_id=None)


def test_deleting_the_source_render_clears_the_provenance_and_nothing_else(head_url):
    owner = add_owner(head_url)
    _, output = _render_output(head_url, owner)
    course = _course(head_url, owner)
    module = _module(head_url, course, owner, source_render_output_id=output)
    [before] = sql(head_url, "SELECT * FROM training_modules WHERE id = :i", i=module)

    sql(head_url, "DELETE FROM render_outputs WHERE id = :i", i=output)

    [after] = sql(head_url, "SELECT * FROM training_modules WHERE id = :i", i=module)
    assert after["source_render_output_id"] is None
    assert {
        k: v for k, v in after.items() if k not in ("source_render_output_id", "updated_at")
    } == {k: v for k, v in before.items() if k not in ("source_render_output_id", "updated_at")}


def test_deleting_the_source_project_leaves_the_published_module_with_its_snapshot(head_url):
    owner = add_owner(head_url)
    project, output = _render_output(head_url, owner)
    course = _course(head_url, owner)
    module = _module(head_url, course, owner, source_render_output_id=output)
    [before] = sql(head_url, "SELECT * FROM training_modules WHERE id = :i", i=module)

    sql(head_url, "DELETE FROM projects WHERE id = :i", i=project)

    assert _count(head_url, "render_outputs") == 0  # the render went with its project
    [after] = sql(head_url, "SELECT * FROM training_modules WHERE id = :i", i=module)
    assert after["status"] == "published" and after["source_render_output_id"] is None
    for column in (
        "video_bucket",
        "video_key",
        "video_byte_size",
        "video_duration_ms",
        "video_width",
        "video_height",
        "video_fps",
        "video_output_hash",
    ):
        assert after[column] == before[column]


# --- people are never deleted out from under training rows -----------------------------------


def test_a_user_who_created_published_or_enrolled_cannot_be_hard_deleted(head_url):
    admin = add_owner(head_url)
    learner = add_owner(head_url)
    course = _course(head_url, admin)
    _module(head_url, course, admin)
    _enrol(head_url, course, learner, admin)

    with pytest.raises(IntegrityError):
        sql(head_url, "DELETE FROM users WHERE id = :i", i=learner)  # enrolled
    with pytest.raises(IntegrityError):
        sql(head_url, "DELETE FROM users WHERE id = :i", i=admin)  # created / published / assigned


# --- enrolments ------------------------------------------------------------------------------


def test_a_person_is_enrolled_in_a_course_at_most_once(head_url):
    admin = add_owner(head_url)
    learner = add_owner(head_url)
    course = _course(head_url, admin)
    _enrol(head_url, course, learner, admin)

    with pytest.raises(IntegrityError):
        _enrol(head_url, course, learner, admin)
    _enrol(head_url, _course(head_url, admin, t="Other"), learner, admin)  # another course is fine


@pytest.mark.parametrize("status", ["pending", "removed", ""])
def test_enrolment_status_is_check_constrained(head_url, status):
    admin = add_owner(head_url)
    learner = add_owner(head_url)
    course = _course(head_url, admin)

    with pytest.raises(IntegrityError):
        _enrol(head_url, course, learner, admin, s=status)


def test_enrolment_needs_a_real_course_and_a_real_person(head_url):
    admin = add_owner(head_url)
    course = _course(head_url, admin)

    with pytest.raises(IntegrityError):
        _enrol(head_url, str(uuid4()), admin, admin)
    with pytest.raises(IntegrityError):
        _enrol(head_url, course, str(uuid4()), admin)


def test_withdrawing_keeps_the_enrolment_row(head_url):
    admin = add_owner(head_url)
    learner = add_owner(head_url)
    course = _course(head_url, admin)
    _enrol(head_url, course, learner, admin)

    sql(
        head_url, "UPDATE course_enrolments SET status = 'withdrawn' WHERE course_id = :c", c=course
    )

    [row] = sql(head_url, "SELECT status FROM course_enrolments WHERE course_id = :c", c=course)
    assert row["status"] == "withdrawn"
