"""Migration 0011 - ``en-IN`` becomes the default project language - upgraded and
downgraded for real on a scratch database (see ``migration_helpers.py``).

The point of the migration is what it must *not* do: change only the column default,
never a row. So these compare whole rows - ``updated_at`` included, which the
``set_updated_at`` trigger would bump on any ``UPDATE`` - before and after.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.exc import IntegrityError

from tests.migration_helpers import (
    add_owner,
    add_project,
    project_row,
    run_alembic,
    scratch_database,
)


@pytest.fixture
def scratch_url() -> Iterator[str]:
    with scratch_database() as url:
        yield url


def test_upgrade_makes_en_in_the_default_without_rewriting_any_existing_project(scratch_url):
    run_alembic(scratch_url, "upgrade", "0010")
    owner = add_owner(scratch_url)
    add_project(scratch_url, owner, "existing-default")  # took 0002's default, en-GB
    add_project(scratch_url, owner, "existing-en-gb", language="en-GB")
    add_project(scratch_url, owner, "existing-en-in", language="en-IN")
    titles = ["existing-default", "existing-en-gb", "existing-en-in"]
    before = {title: project_row(scratch_url, title) for title in titles}
    assert before["existing-default"]["language"] == "en-GB"

    run_alembic(scratch_url, "upgrade", "0011")

    assert {title: project_row(scratch_url, title) for title in titles} == before
    add_project(scratch_url, owner, "new-without-language")
    assert project_row(scratch_url, "new-without-language")["language"] == "en-IN"
    # en-GB is still a supported choice, and nothing else has become one.
    add_project(scratch_url, owner, "new-en-gb", language="en-GB")
    assert project_row(scratch_url, "new-en-gb")["language"] == "en-GB"
    with pytest.raises(IntegrityError, match="ck_projects_language"):
        add_project(scratch_url, owner, "unsupported", language="fr-FR")


def test_downgrade_restores_the_en_gb_default_and_leaves_every_project_alone(scratch_url):
    run_alembic(scratch_url, "upgrade", "0011")
    owner = add_owner(scratch_url)
    add_project(scratch_url, owner, "created-as-default")  # en-IN
    add_project(scratch_url, owner, "created-as-en-gb", language="en-GB")
    titles = ["created-as-default", "created-as-en-gb"]
    before = {title: project_row(scratch_url, title) for title in titles}
    assert before["created-as-default"]["language"] == "en-IN"

    run_alembic(scratch_url, "downgrade", "0010")

    assert {title: project_row(scratch_url, title) for title in titles} == before
    add_project(scratch_url, owner, "after-downgrade")
    assert project_row(scratch_url, "after-downgrade")["language"] == "en-GB"

    run_alembic(scratch_url, "upgrade", "0011")  # and it goes forward again
    add_project(scratch_url, owner, "after-re-upgrade")
    assert project_row(scratch_url, "after-re-upgrade")["language"] == "en-IN"
