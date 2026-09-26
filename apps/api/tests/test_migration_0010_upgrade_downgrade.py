"""Migration 0010 upgraded and downgraded for real, with Alembic, on a scratch database.

``test_migration_0010_schema.py`` checks the finished schema on the shared test
database. What it cannot check is the *journey*: that a database holding projects
written before 0010 upgrades cleanly and those projects come out working, and that
``downgrade`` really takes the change back out. Rolling the shared database back
mid-suite would pull the schema out from under every other test, so each test here
creates its own database, runs the real ``alembic`` command line against it (as
``make migrate`` / ``make migrate-down`` do), and drops it afterwards.

These are plain (sync) tests: ``alembic/env.py`` starts its own event loop, which it
cannot do inside a running one.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.exc import IntegrityError

from tests.migration_helpers import (
    add_owner,
    add_project,
    project_row,
    projects_columns,
    projects_constraints,
    run_alembic,
    scratch_database,
    sql,
)

_NEW_COLUMNS = {"orientation", "target_duration_ms"}
_NEW_CONSTRAINTS = {
    "ck_projects_orientation",
    "ck_projects_target_duration_ms_range",
    "ck_projects_language",
}


@pytest.fixture
def scratch_url() -> Iterator[str]:
    with scratch_database() as url:
        yield url


def test_upgrade_gives_existing_projects_the_defaults_and_keeps_them_working(scratch_url):
    run_alembic(scratch_url, "upgrade", "0009")
    assert not _NEW_COLUMNS & projects_columns(scratch_url)
    assert not _NEW_CONSTRAINTS & projects_constraints(scratch_url)
    owner = add_owner(scratch_url)
    add_project(scratch_url, owner, "legacy-default")  # language takes 0002's default
    add_project(scratch_url, owner, "legacy-en-in", language="en-IN")

    run_alembic(scratch_url, "upgrade", "0010")

    assert projects_columns(scratch_url) >= _NEW_COLUMNS
    assert projects_constraints(scratch_url) >= _NEW_CONSTRAINTS
    for title, language in [("legacy-default", "en-GB"), ("legacy-en-in", "en-IN")]:
        project = project_row(scratch_url, title)
        assert project["orientation"] == "horizontal"
        assert project["target_duration_ms"] == 300_000
        assert project["language"] == language  # a supported language is never touched
    assert len(sql(scratch_url, "SELECT id FROM projects")) == 2
    # And the upgraded database enforces exactly what the new code relies on.
    add_project(scratch_url, owner, "minimum", target_duration_ms=60_000)
    add_project(scratch_url, owner, "maximum", target_duration_ms=3_600_000)
    add_project(scratch_url, owner, "vertical", orientation="vertical")
    add_project(scratch_url, owner, "en-in", language="en-IN")
    for columns, constraint in [
        ({"target_duration_ms": 59_999}, "ck_projects_target_duration_ms_range"),
        ({"target_duration_ms": 3_600_001}, "ck_projects_target_duration_ms_range"),
        ({"orientation": "square"}, "ck_projects_orientation"),
        ({"language": "fr-FR"}, "ck_projects_language"),
    ]:
        with pytest.raises(IntegrityError, match=constraint):
            add_project(scratch_url, owner, "rejected", **columns)


def test_upgrade_resets_a_language_the_old_schema_accepted_but_the_new_one_does_not(scratch_url):
    """``projects.language`` was unconstrained free text before 0010. A row holding
    something outside the supported set is reset to the default so the constraint can
    be added at all; the upgrade must not fail on it.
    """
    run_alembic(scratch_url, "upgrade", "0009")
    owner = add_owner(scratch_url)
    add_project(scratch_url, owner, "french", language="fr-FR")
    add_project(scratch_url, owner, "lowercase", language="en-in")
    add_project(scratch_url, owner, "blank", language="")

    run_alembic(scratch_url, "upgrade", "0010")

    for title in ("french", "lowercase", "blank"):
        assert project_row(scratch_url, title)["language"] == "en-GB"


def test_the_migrations_form_a_single_line_that_upgrades_to_head(scratch_url):
    run_alembic(scratch_url, "upgrade", "head")

    assert run_alembic(scratch_url, "current").count("(head)") == 1
    assert run_alembic(scratch_url, "heads").count("(head)") == 1


def test_downgrade_removes_the_columns_and_constraints_and_keeps_the_projects(scratch_url):
    run_alembic(scratch_url, "upgrade", "0010")
    owner = add_owner(scratch_url)
    add_project(
        scratch_url,
        owner,
        "kept",
        language="en-IN",
        orientation="vertical",
        target_duration_ms=1_800_000,
    )

    run_alembic(scratch_url, "downgrade", "0009")

    assert not _NEW_COLUMNS & projects_columns(scratch_url)
    assert not _NEW_CONSTRAINTS & projects_constraints(scratch_url)
    kept = project_row(scratch_url, "kept")
    assert kept["language"] == "en-IN"
    assert "orientation" not in kept
    # The old schema's free-text language is free text again.
    sql(scratch_url, "UPDATE projects SET language = 'fr-FR' WHERE title = 'kept'")


def test_the_migration_round_trips(scratch_url):
    """upgrade -> downgrade -> upgrade lands on the same schema, and the second upgrade
    copes with data the first downgrade let in.
    """
    run_alembic(scratch_url, "upgrade", "0010")
    columns = projects_columns(scratch_url)
    constraints = projects_constraints(scratch_url)
    owner = add_owner(scratch_url)

    run_alembic(scratch_url, "downgrade", "0009")
    add_project(scratch_url, owner, "written-while-downgraded", language="de-DE")
    run_alembic(scratch_url, "upgrade", "0010")

    assert projects_columns(scratch_url) == columns
    assert projects_constraints(scratch_url) == constraints
    project = project_row(scratch_url, "written-while-downgraded")
    assert project["language"] == "en-GB"
    assert (project["orientation"], project["target_duration_ms"]) == ("horizontal", 300_000)
