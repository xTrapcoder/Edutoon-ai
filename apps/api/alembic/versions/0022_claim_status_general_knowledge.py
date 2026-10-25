"""a claim may be marked as general knowledge (keyword-only projects)

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-16

A project built from keywords alone has no source material, so none of its claims can be checked
against anything and none may ever be called ``supported`` (that word means "a cited passage backs
this"). Such a claim is recorded with its own status, ``general_knowledge``: the model's general
knowledge, never verified, never cited. Nothing else changes - ``supported`` still requires at least
one citation, and a project that has source material never produces this status.

``claim_verifications.status`` is a text column with a CHECK (rule 7), so this only widens the
CHECK.

``downgrade`` narrows it again and is refused, with a clear message, while any ``general_knowledge``
claim exists: those rows cannot be expressed in the old vocabulary and are not rewritten or deleted
here.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "('supported', 'partially_supported', 'unsupported', 'contradicted')"
_NEW = "('supported', 'partially_supported', 'unsupported', 'contradicted', 'general_knowledge')"


def _replace_check(values: str) -> None:
    op.execute("ALTER TABLE claim_verifications DROP CONSTRAINT ck_claim_verifications_status;")
    op.execute(
        "ALTER TABLE claim_verifications ADD CONSTRAINT ck_claim_verifications_status "
        f"CHECK (status IN {values});"
    )


def upgrade() -> None:
    _replace_check(_NEW)


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM claim_verifications WHERE status = 'general_knowledge') THEN
                RAISE EXCEPTION
                    'Cannot downgrade: general_knowledge claims exist and are not rewritten here.';
            END IF;
        END $$;
        """
    )
    _replace_check(_OLD)
