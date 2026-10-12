"""Migration 0018 upgraded and downgraded for real, with Alembic, on a scratch database
(mirrors ``test_migration_0016_upgrade_downgrade.py``).

0018 changes the delete rule of two foreign keys and nothing else, so what matters is the
before / after of exactly those two rules, that nothing else about deletion moved, and that a
project holding claims, citations, segments and their links really deletes at 0018 - and does
not at 0017, which is the bug it fixes.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.exc import IntegrityError

from tests.migration_helpers import add_owner, add_project, run_alembic, scratch_database, sql

CHANGED = {
    "fk_claim_citations_source_chunk_id_source_chunks": "claim_citations",
    "fk_script_segment_claims_claim_verification_id": "script_segment_claims",
}


@pytest.fixture
def scratch_url() -> Iterator[str]:
    with scratch_database() as url:
        yield url


def _delete_rules(url: str) -> dict[str, str]:
    """``confdeltype`` of every foreign key in the public schema, by constraint name."""
    rows = sql(
        url,
        "SELECT conname, confdeltype::text AS rule FROM pg_constraint "
        "WHERE contype = 'f' AND connamespace = 'public'::regnamespace",
    )
    return {row["conname"]: row["rule"] for row in rows}


def _build_project_with_claims(url: str) -> str:
    """A project with a chunk, a claim citing it, and a script segment linked to that claim."""
    owner = add_owner(url)
    add_project(url, owner, "with claims")
    [project] = sql(url, "SELECT id FROM projects WHERE title = 'with claims'")
    project_id = str(project["id"])
    [source] = sql(
        url,
        "INSERT INTO uploaded_sources (project_id, kind, original_filename, content_type, "
        "storage_bucket, storage_key, byte_size, checksum_sha256) "
        "VALUES (:p, 'pdf', 'a.pdf', 'application/pdf', 'b', 'projects/x/a.pdf', 1, :c) "
        "RETURNING id",
        p=project_id,
        c="c" * 64,
    )
    [chunk] = sql(
        url,
        "INSERT INTO source_chunks (source_id, project_id, chunk_index, content) "
        "VALUES (:s, :p, 0, 'Text.') RETURNING id",
        s=source["id"],
        p=project_id,
    )
    [claim] = sql(
        url,
        "INSERT INTO claim_verifications (project_id, claim_text, status, embedding_model, "
        "llm_model) VALUES (:p, 'A claim.', 'supported', 'none', 'deck-engine/1') RETURNING id",
        p=project_id,
    )
    sql(
        url,
        "INSERT INTO claim_citations (claim_verification_id, source_chunk_id, similarity) "
        "VALUES (:c, :k, 1)",
        c=claim["id"],
        k=chunk["id"],
    )
    [script] = sql(
        url,
        "INSERT INTO scripts (project_id, status, llm_model) VALUES (:p, 'draft', 'm') "
        "RETURNING id",
        p=project_id,
    )
    [segment] = sql(
        url,
        "INSERT INTO script_segments (script_id, segment_index, narration_text, "
        "estimated_duration_ms) VALUES (:s, 0, 'Hi.', 1000) RETURNING id",
        s=script["id"],
    )
    sql(
        url,
        "INSERT INTO script_segment_claims (script_segment_id, claim_verification_id) "
        "VALUES (:g, :c)",
        g=segment["id"],
        c=claim["id"],
    )
    return project_id


def test_upgrade_cascades_exactly_the_two_keys_and_downgrade_restores_restrict(scratch_url):
    run_alembic(scratch_url, "upgrade", "0017")
    before = _delete_rules(scratch_url)
    assert {name: before[name] for name in CHANGED} == dict.fromkeys(CHANGED, "r")

    run_alembic(scratch_url, "upgrade", "0018")
    after = _delete_rules(scratch_url)
    assert {name: after[name] for name in CHANGED} == dict.fromkeys(CHANGED, "c")
    # Nothing else about deletion moved: same keys, same rules, apart from those two.
    assert {k: v for k, v in after.items() if k not in CHANGED} == {
        k: v for k, v in before.items() if k not in CHANGED
    }

    run_alembic(scratch_url, "downgrade", "0017")
    assert _delete_rules(scratch_url) == before

    run_alembic(scratch_url, "upgrade", "head")
    assert {name: _delete_rules(scratch_url)[name] for name in CHANGED} == dict.fromkeys(
        CHANGED, "c"
    )


def test_a_project_with_claims_cannot_be_deleted_at_0017(scratch_url):
    """The bug, kept as a regression anchor for what 0018 fixes."""
    run_alembic(scratch_url, "upgrade", "0017")
    project_id = _build_project_with_claims(scratch_url)

    with pytest.raises(IntegrityError):
        sql(scratch_url, "DELETE FROM projects WHERE id = :p", p=project_id)


def test_a_project_with_claims_deletes_at_0018_and_takes_everything_with_it(scratch_url):
    run_alembic(scratch_url, "upgrade", "head")
    project_id = _build_project_with_claims(scratch_url)

    sql(scratch_url, "DELETE FROM projects WHERE id = :p", p=project_id)

    for table in (
        "uploaded_sources",
        "source_chunks",
        "claim_verifications",
        "claim_citations",
        "scripts",
        "script_segments",
        "script_segment_claims",
    ):
        assert sql(scratch_url, f"SELECT count(*) AS n FROM {table}")[0]["n"] == 0, table
    # The owner is not part of the project's tree.
    assert sql(scratch_url, "SELECT count(*) AS n FROM users")[0]["n"] == 1
