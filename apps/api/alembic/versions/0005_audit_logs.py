"""audit_logs

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-02

The audit trail must outlive the rows it describes, so ``actor_id`` /
``entity_id`` / ``project_id`` are stored as loose UUIDs with no foreign keys.
An append-only trigger rejects UPDATE and DELETE.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE audit_logs (
            id          uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            actor_id    uuid,
            actor_type  text        NOT NULL DEFAULT 'user',
            action      text        NOT NULL,
            entity_type text        NOT NULL,
            entity_id   uuid,
            project_id  uuid,
            request_id  text,
            ip_address  inet,
            context     jsonb       NOT NULL DEFAULT '{}'::jsonb,
            created_at  timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_audit_logs_actor_type
                CHECK (actor_type IN ('user', 'system', 'service')),
            CONSTRAINT ck_audit_logs_action_not_blank
                CHECK (length(btrim(action)) > 0),
            CONSTRAINT ck_audit_logs_entity_type_not_blank
                CHECK (length(btrim(entity_type)) > 0),
            CONSTRAINT ck_audit_logs_context_is_object
                CHECK (jsonb_typeof(context) = 'object')
        );
        """
    )
    op.execute(
        """
        CREATE INDEX ix_audit_logs_entity_type_entity_id
            ON audit_logs (entity_type, entity_id);
        """
    )
    op.execute(
        """
        CREATE INDEX ix_audit_logs_project_id_created_at
            ON audit_logs (project_id, created_at DESC);
        """
    )
    op.execute(
        """
        CREATE INDEX ix_audit_logs_actor_id_created_at
            ON audit_logs (actor_id, created_at DESC);
        """
    )
    op.execute("CREATE INDEX ix_audit_logs_action ON audit_logs (action);")
    op.execute("CREATE INDEX ix_audit_logs_created_at ON audit_logs (created_at DESC);")
    op.execute(
        """
        CREATE TRIGGER trg_audit_logs_append_only
            BEFORE UPDATE OR DELETE ON audit_logs
            FOR EACH ROW EXECUTE FUNCTION reject_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_logs;")
