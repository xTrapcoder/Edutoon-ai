"""Migration 0022 (claim status ``general_knowledge``) upgraded and downgraded for real, with
Alembic, on a scratch database.

0022 only widens a CHECK on ``claim_verifications.status`` (rule 7: text column, not an enum), so
what matters is that the old vocabulary still works, the new word is accepted only after the
upgrade, and a downgrade is refused - rather than rewriting or deleting anything - while a claim
that uses the new word exists.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from tests.migration_helpers import (
    add_owner,
    add_project,
    project_row,
    run_alembic,
    scratch_database,
    sql,
)

_ENV = {"ADMIN_BOOTSTRAP_EMAILS": ""}


@pytest.fixture
def scratch_url() -> Iterator[str]:
    with scratch_database() as url:
        yield url


def _claim(url: str, status: str) -> None:
    title = f"p-{status}-{uuid4()}"
    add_project(url, add_owner(url), title)
    project = project_row(url, title)
    sql(
        url,
        "INSERT INTO claim_verifications "
        "(project_id, claim_text, status, embedding_model, llm_model) "
        "VALUES (:p, 'A claim.', :s, 'e', 'l')",
        p=project["id"],
        s=status,
    )


def test_the_new_status_is_refused_before_the_upgrade_and_accepted_after(scratch_url):
    run_alembic(scratch_url, "upgrade", "0021", env=_ENV)
    with pytest.raises(IntegrityError):
        _claim(scratch_url, "general_knowledge")

    run_alembic(scratch_url, "upgrade", "0022")
    _claim(scratch_url, "general_knowledge")
    _claim(scratch_url, "supported")  # the old vocabulary is untouched
    with pytest.raises(IntegrityError):
        _claim(scratch_url, "made_up")
    assert run_alembic(scratch_url, "current").count("0022") == 1


def test_a_downgrade_with_no_such_claim_restores_the_old_vocabulary(scratch_url):
    run_alembic(scratch_url, "upgrade", "0022", env=_ENV)
    _claim(scratch_url, "unsupported")

    run_alembic(scratch_url, "downgrade", "0021")

    assert run_alembic(scratch_url, "current").count("0021") == 1
    with pytest.raises(IntegrityError):
        _claim(scratch_url, "general_knowledge")
    [row] = sql(scratch_url, "SELECT status FROM claim_verifications")
    assert row["status"] == "unsupported"  # the data is exactly as it was


def test_a_downgrade_is_refused_while_a_general_knowledge_claim_exists(scratch_url):
    run_alembic(scratch_url, "upgrade", "0022", env=_ENV)
    _claim(scratch_url, "general_knowledge")

    with pytest.raises(AssertionError) as refused:  # run_alembic asserts a zero exit code
        run_alembic(scratch_url, "downgrade", "0021")

    assert "Cannot downgrade: general_knowledge claims exist" in str(refused.value)
    [row] = sql(scratch_url, "SELECT status FROM claim_verifications")
    assert row["status"] == "general_knowledge"  # nothing was rewritten or deleted
    assert run_alembic(scratch_url, "current").count("0022") == 1
