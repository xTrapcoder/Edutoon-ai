"""deleting a project removes its claims' evidence and script links

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-07

Two foreign keys were ``ON DELETE RESTRICT`` from the Phase 4 and Phase 7 baselines, which made
``DELETE /v1/projects/{id}`` fail with a 500 for any project that had verified claims:

* ``claim_citations.source_chunk_id -> source_chunks`` - deleting the project cascades to its
  ``source_chunks`` and to its ``claim_verifications`` independently, and the citation rows
  sitting between the two blocked the chunk delete;
* ``script_segment_claims.claim_verification_id -> claim_verifications`` - the same shape, between
  a script's segments and the claims they cite.

Both become ``ON DELETE CASCADE``, so the whole project tree now goes in one statement. Nothing
else about deletion changes: no other foreign key is touched, the repositories expose no way to
delete a single claim, source chunk or uploaded source on its own, and the ledger-style tables
(``audit_logs``, which has no foreign keys) are unaffected.

``downgrade`` restores ``RESTRICT``; that cannot fail against existing data.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_foreign_key(
    table: str, constraint: str, column: str, referenced: str, on_delete: str
) -> None:
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint};")
    op.execute(
        f"""
        ALTER TABLE {table}
            ADD CONSTRAINT {constraint}
                FOREIGN KEY ({column}) REFERENCES {referenced} (id)
                ON DELETE {on_delete};
        """
    )


def upgrade() -> None:
    _replace_foreign_key(
        "claim_citations",
        "fk_claim_citations_source_chunk_id_source_chunks",
        "source_chunk_id",
        "source_chunks",
        "CASCADE",
    )
    _replace_foreign_key(
        "script_segment_claims",
        "fk_script_segment_claims_claim_verification_id",
        "claim_verification_id",
        "claim_verifications",
        "CASCADE",
    )


def downgrade() -> None:
    _replace_foreign_key(
        "script_segment_claims",
        "fk_script_segment_claims_claim_verification_id",
        "claim_verification_id",
        "claim_verifications",
        "RESTRICT",
    )
    _replace_foreign_key(
        "claim_citations",
        "fk_claim_citations_source_chunk_id_source_chunks",
        "source_chunk_id",
        "source_chunks",
        "RESTRICT",
    )
