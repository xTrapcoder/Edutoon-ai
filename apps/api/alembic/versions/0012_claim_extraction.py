"""claim_verifications.topic_id, and the extract_claims job kind

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-28

Schema for Phase 6's candidate-claim extraction: turning a project's
*accepted* topics into candidate factual claims that are then run through
the existing Evidence Engine (``services.evidence.verify_claim`` /
``providers.llm.judge_claim``, both unchanged) exactly as a hand-typed claim
already is. Purely additive: no existing column, constraint or index is
altered, so everything written before this migration keeps its meaning.

``claim_verifications.topic_id`` records which topic a claim was extracted
for. Nullable and ``ON DELETE SET NULL``: a claim created through the
existing manual ``POST /claim-verifications`` route has no topic behind it
and keeps ``NULL`` forever, unchanged; deleting a topic never blocks on, or
silently destroys, the verified claims extracted from it - the ledger row
stays, only its topic link clears (the same reasoning as
``script_segments.topic_id``, not ``claim_citations``' ``RESTRICT``, since a
claim's citations are its own evidence while its topic is only where the
candidate came from).

``ck_jobs_kind`` (migration 0003) is widened to allow the new
``extract_claims`` job kind. Every existing row's ``kind`` is still one of
the original six values, so widening the constraint cannot fail against
current data.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_JOB_KINDS = (
    "parse_source",
    "extract_topics",
    "build_evidence",
    "generate_script",
    "synthesise_voice",
    "render_video",
)
_NEW_JOB_KINDS = (*_OLD_JOB_KINDS, "extract_claims")


def upgrade() -> None:
    op.execute("ALTER TABLE claim_verifications ADD COLUMN topic_id uuid;")
    op.execute(
        """
        ALTER TABLE claim_verifications
            ADD CONSTRAINT fk_claim_verifications_topic_id_project_topics
                FOREIGN KEY (topic_id) REFERENCES project_topics (id) ON DELETE SET NULL;
        """
    )
    op.execute("CREATE INDEX ix_claim_verifications_topic_id ON claim_verifications (topic_id);")

    op.execute("ALTER TABLE jobs DROP CONSTRAINT ck_jobs_kind;")
    op.execute(
        f"""
        ALTER TABLE jobs
            ADD CONSTRAINT ck_jobs_kind CHECK (kind IN (
                {", ".join(f"'{kind}'" for kind in _NEW_JOB_KINDS)}
            ));
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS ck_jobs_kind;")
    op.execute(
        f"""
        ALTER TABLE jobs
            ADD CONSTRAINT ck_jobs_kind CHECK (kind IN (
                {", ".join(f"'{kind}'" for kind in _OLD_JOB_KINDS)}
            ));
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_claim_verifications_topic_id;")
    op.execute(
        "ALTER TABLE claim_verifications DROP CONSTRAINT IF EXISTS "
        "fk_claim_verifications_topic_id_project_topics;"
    )
    op.execute("ALTER TABLE claim_verifications DROP COLUMN IF EXISTS topic_id;")
