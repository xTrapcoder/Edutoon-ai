"""Migration 0023 (the ``text`` source kind and project type) upgraded and downgraded for real,
with Alembic, on a scratch database.

0023 only widens two CHECKs (rule 7: text columns, not enums), so what matters is that the old
vocabulary still works, the new word is accepted only after the upgrade, and a downgrade is
refused - rather than rewriting or deleting anything - while a text project or source exists.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from tests.migration_helpers import (
    add_owner,
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


def _project(url: str, source_type: str) -> dict:
    title = f"p-{source_type}-{uuid4()}"
    owner = add_owner(url)
    sql(
        url,
        "INSERT INTO projects (owner_id, title, source_type) VALUES (:o, :t, :s)",
        o=owner,
        t=title,
        s=source_type,
    )
    return project_row(url, title)


def _source(url: str, project_id, kind: str) -> None:
    checksum = uuid4().hex + uuid4().hex
    sql(
        url,
        "INSERT INTO uploaded_sources (project_id, kind, original_filename, storage_bucket, "
        "storage_key, byte_size, checksum_sha256) "
        "VALUES (:p, :k, 'f', 'b', :key, 1, :c)",
        p=project_id,
        k=kind,
        key=f"k-{checksum}",
        c=checksum,
    )


def test_text_is_refused_before_the_upgrade_and_accepted_after(scratch_url):
    run_alembic(scratch_url, "upgrade", "0022", env=_ENV)
    with pytest.raises(IntegrityError):
        _project(scratch_url, "text")
    pdf = _project(scratch_url, "pdf")
    with pytest.raises(IntegrityError):
        _source(scratch_url, pdf["id"], "text")

    run_alembic(scratch_url, "upgrade", "0023")

    text_project = _project(scratch_url, "text")
    _source(scratch_url, text_project["id"], "text")
    for old in ("pdf", "topic", "pptx"):  # the old vocabulary is untouched
        _project(scratch_url, old)
    _source(scratch_url, pdf["id"], "pptx")
    with pytest.raises(IntegrityError):
        _project(scratch_url, "made_up")
    with pytest.raises(IntegrityError):
        _source(scratch_url, pdf["id"], "made_up")
    assert run_alembic(scratch_url, "current").count("0023") == 1


def test_a_downgrade_with_no_text_rows_restores_the_old_vocabulary(scratch_url):
    run_alembic(scratch_url, "upgrade", "0023", env=_ENV)
    pdf = _project(scratch_url, "pdf")
    _source(scratch_url, pdf["id"], "pdf")

    run_alembic(scratch_url, "downgrade", "0022")

    assert run_alembic(scratch_url, "current").count("0022") == 1
    with pytest.raises(IntegrityError):
        _project(scratch_url, "text")
    [row] = sql(scratch_url, "SELECT kind FROM uploaded_sources")
    assert row["kind"] == "pdf"  # the data is exactly as it was


def test_a_downgrade_is_refused_while_a_text_project_exists(scratch_url):
    run_alembic(scratch_url, "upgrade", "0023", env=_ENV)
    _project(scratch_url, "text")

    with pytest.raises(AssertionError):  # run_alembic asserts a zero exit code
        run_alembic(scratch_url, "downgrade", "0022")

    [row] = sql(scratch_url, "SELECT source_type FROM projects")
    assert row["source_type"] == "text"  # nothing was rewritten or deleted
    assert run_alembic(scratch_url, "current").count("0023") == 1


def test_a_downgrade_is_refused_while_a_text_source_exists(scratch_url):
    run_alembic(scratch_url, "upgrade", "0023", env=_ENV)
    pdf = _project(scratch_url, "pdf")
    _source(scratch_url, pdf["id"], "text")

    with pytest.raises(AssertionError):
        run_alembic(scratch_url, "downgrade", "0022")

    [row] = sql(scratch_url, "SELECT kind FROM uploaded_sources")
    assert row["kind"] == "text"
    assert run_alembic(scratch_url, "current").count("0023") == 1
