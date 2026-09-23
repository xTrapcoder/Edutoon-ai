"""scripts, script_segments and script_segment_claims

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-08

Persists the Script Generation Engine's output. ``scripts`` is a status
machine (``pending -> draft -> approved``, plus ``failed`` / ``superseded``)
much like ``jobs`` / ``projects``. ``script_segments`` and
``script_segment_claims`` are append-only in the same spirit as
``claim_citations``: a regenerated script is a new ``scripts`` row, never an
edit of an existing one, so repositories only ever INSERT or SELECT them.

``script_segment_claims`` is the traceability link back to the Evidence
Engine - every citation points at a real ``claim_verifications`` row, and
the ``ON DELETE RESTRICT`` FK makes "a script's provenance cannot silently
disappear" a DB-level guarantee.

At most one *active* script (``pending`` / ``draft`` / ``approved``) may
exist per project - enforced by the partial unique index
``uq_scripts_project_id_active`` - so a new generation supersedes the
previous one rather than racing it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE scripts (
            id                    uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            project_id            uuid        NOT NULL,
            status                text        NOT NULL DEFAULT 'pending',
            llm_model             text        NOT NULL,
            estimated_duration_ms bigint,
            created_at            timestamptz NOT NULL DEFAULT now(),
            updated_at            timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_scripts_project_id_projects
                FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE,
            CONSTRAINT ck_scripts_status
                CHECK (status IN (
                    'pending', 'draft', 'approved', 'failed', 'superseded'
                )),
            CONSTRAINT ck_scripts_estimated_duration_ms_non_negative
                CHECK (estimated_duration_ms IS NULL OR estimated_duration_ms >= 0)
        );
        """
    )
    op.execute("CREATE INDEX ix_scripts_project_id ON scripts (project_id);")
    op.execute(
        """
        CREATE INDEX ix_scripts_project_id_created_at
            ON scripts (project_id, created_at DESC);
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_scripts_project_id_active
            ON scripts (project_id)
            WHERE status IN ('pending', 'draft', 'approved');
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_scripts_set_updated_at
            BEFORE UPDATE ON scripts
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )

    op.execute(
        """
        CREATE TABLE script_segments (
            id                    uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            script_id             uuid        NOT NULL,
            segment_index         integer     NOT NULL,
            narration_text        text        NOT NULL,
            estimated_duration_ms bigint      NOT NULL,
            topic_id              uuid,
            created_at            timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_script_segments_script_id_scripts
                FOREIGN KEY (script_id) REFERENCES scripts (id) ON DELETE CASCADE,
            CONSTRAINT fk_script_segments_topic_id_project_topics
                FOREIGN KEY (topic_id) REFERENCES project_topics (id) ON DELETE SET NULL,
            CONSTRAINT ck_script_segments_segment_index_non_negative
                CHECK (segment_index >= 0),
            CONSTRAINT ck_script_segments_narration_text_not_blank
                CHECK (length(btrim(narration_text)) > 0),
            CONSTRAINT ck_script_segments_estimated_duration_ms_non_negative
                CHECK (estimated_duration_ms >= 0)
        );
        """
    )
    op.execute("CREATE INDEX ix_script_segments_script_id ON script_segments (script_id);")
    op.execute("CREATE INDEX ix_script_segments_topic_id ON script_segments (topic_id);")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_script_segments_script_id_segment_index
            ON script_segments (script_id, segment_index);
        """
    )

    op.execute(
        """
        CREATE TABLE script_segment_claims (
            id                    uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            script_segment_id     uuid        NOT NULL,
            claim_verification_id uuid        NOT NULL,
            created_at            timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_script_segment_claims_script_segment_id
                FOREIGN KEY (script_segment_id) REFERENCES script_segments (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_script_segment_claims_claim_verification_id
                FOREIGN KEY (claim_verification_id) REFERENCES claim_verifications (id)
                ON DELETE RESTRICT
        );
        """
    )
    op.execute(
        """
        CREATE INDEX ix_script_segment_claims_script_segment_id
            ON script_segment_claims (script_segment_id);
        """
    )
    op.execute(
        """
        CREATE INDEX ix_script_segment_claims_claim_verification_id
            ON script_segment_claims (claim_verification_id);
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_script_segment_claims_segment_verification
            ON script_segment_claims (script_segment_id, claim_verification_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS script_segment_claims;")
    op.execute("DROP TABLE IF EXISTS script_segments;")
    op.execute("DROP TABLE IF EXISTS scripts;")
