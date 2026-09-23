"""claim_verifications and claim_citations

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-05

Persists the Evidence/Verification Engine's output: a claim's verdict and
the specific source_chunks rows an LLM actually cited in reaching it.
Append-only by design (a re-verification is a new row, not an update) -
same spirit as audit_logs, but with real FKs since these rows reference
live, owned resources rather than a loose historical trail.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE claim_verifications (
            id              uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            project_id      uuid        NOT NULL,
            claim_text      text        NOT NULL,
            status          text        NOT NULL,
            rationale       text,
            embedding_model text        NOT NULL,
            llm_model       text        NOT NULL,
            created_at      timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_claim_verifications_project_id_projects
                FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE,
            CONSTRAINT ck_claim_verifications_status
                CHECK (status IN (
                    'supported', 'partially_supported', 'unsupported', 'contradicted'
                )),
            CONSTRAINT ck_claim_verifications_claim_text_not_blank
                CHECK (length(btrim(claim_text)) > 0)
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_claim_verifications_project_id ON claim_verifications (project_id);"
    )
    op.execute(
        """
        CREATE INDEX ix_claim_verifications_project_id_created_at
            ON claim_verifications (project_id, created_at DESC);
        """
    )

    op.execute(
        """
        CREATE TABLE claim_citations (
            id                     uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
            claim_verification_id  uuid NOT NULL,
            source_chunk_id        uuid NOT NULL,
            similarity             real NOT NULL,
            created_at             timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_claim_citations_claim_verification_id_claim_verifications
                FOREIGN KEY (claim_verification_id) REFERENCES claim_verifications (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_claim_citations_source_chunk_id_source_chunks
                FOREIGN KEY (source_chunk_id) REFERENCES source_chunks (id)
                ON DELETE RESTRICT,
            CONSTRAINT ck_claim_citations_similarity_range
                CHECK (similarity >= -1 AND similarity <= 1)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX ix_claim_citations_claim_verification_id
            ON claim_citations (claim_verification_id);
        """
    )
    op.execute(
        "CREATE INDEX ix_claim_citations_source_chunk_id ON claim_citations (source_chunk_id);"
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_claim_citations_claim_verification_id_source_chunk_id
            ON claim_citations (claim_verification_id, source_chunk_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS claim_citations;")
    op.execute("DROP TABLE IF EXISTS claim_verifications;")
