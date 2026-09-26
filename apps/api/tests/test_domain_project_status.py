"""``domain.project_status`` - the rule that turns a project's pipeline state into its status.

Pure: no database. The rows that feed it are read and the answer stored by
``repositories.projects.refresh_status`` (see ``test_project_status_lifecycle.py``).
"""

from __future__ import annotations

from typing import get_args

import pytest

from edutoon.domain.project_status import (
    STATUS_ARCHIVED,
    STATUS_DRAFT,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_READY,
    ProjectStatus,
    derive_project_status,
)


def test_the_vocabulary_is_exactly_the_projects_check_constraint() -> None:
    # ck_projects_status, migration 0002.
    assert set(get_args(ProjectStatus)) == {"draft", "processing", "ready", "failed", "archived"}


@pytest.mark.parametrize(
    ("active_jobs", "latest_outcome", "expected"),
    [
        (0, None, STATUS_DRAFT),  # nothing has run
        (0, "succeeded", STATUS_READY),
        (0, "failed", STATUS_FAILED),
        (1, None, STATUS_PROCESSING),
        (3, None, STATUS_PROCESSING),
        # Work in flight beats whatever the last outcome was: a retry is "processing", and
        # more work queued after a success is too.
        (1, "failed", STATUS_PROCESSING),
        (1, "succeeded", STATUS_PROCESSING),
    ],
)
def test_status_follows_the_pipeline(active_jobs, latest_outcome, expected) -> None:
    assert (
        derive_project_status(
            current=STATUS_DRAFT, active_jobs=active_jobs, latest_outcome=latest_outcome
        )
        == expected
    )


@pytest.mark.parametrize("active_jobs", [0, 2])
@pytest.mark.parametrize("latest_outcome", [None, "succeeded", "failed"])
def test_an_archived_project_is_never_moved_by_the_pipeline(active_jobs, latest_outcome) -> None:
    assert (
        derive_project_status(
            current=STATUS_ARCHIVED, active_jobs=active_jobs, latest_outcome=latest_outcome
        )
        == STATUS_ARCHIVED
    )


@pytest.mark.parametrize("current", [s for s in get_args(ProjectStatus) if s != STATUS_ARCHIVED])
def test_the_answer_depends_on_the_pipeline_not_on_the_previous_status(current) -> None:
    assert (
        derive_project_status(current=current, active_jobs=0, latest_outcome="succeeded")
        == STATUS_READY
    )
