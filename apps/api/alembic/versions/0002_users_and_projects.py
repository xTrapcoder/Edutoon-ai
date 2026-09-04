"""users and projects

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE users (
            id              uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            clerk_user_id   text,
            email           text        NOT NULL,
            display_name    text,
            status          text        NOT NULL DEFAULT 'active',
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_users_status
                CHECK (status IN ('active', 'suspended', 'deleted')),
            CONSTRAINT ck_users_email_lowercase
                CHECK (email = lower(email)),
            CONSTRAINT ck_users_email_shape
                CHECK (position('@' IN email) > 1 AND char_length(email) >= 3)
        );
        """
    )
    op.execute("CREATE UNIQUE INDEX uq_users_email ON users (email);")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_users_clerk_user_id
            ON users (clerk_user_id)
            WHERE clerk_user_id IS NOT NULL;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_users_set_updated_at
            BEFORE UPDATE ON users
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )

    op.execute(
        """
        CREATE TABLE projects (
            id           uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            owner_id     uuid        NOT NULL,
            title        text        NOT NULL,
            description  text,
            source_type  text        NOT NULL,
            status       text        NOT NULL DEFAULT 'draft',
            language     text        NOT NULL DEFAULT 'en-GB',
            created_at   timestamptz NOT NULL DEFAULT now(),
            updated_at   timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_projects_owner_id_users
                FOREIGN KEY (owner_id) REFERENCES users (id) ON DELETE CASCADE,
            CONSTRAINT ck_projects_source_type
                CHECK (source_type IN ('pdf', 'topic')),
            CONSTRAINT ck_projects_status
                CHECK (status IN ('draft', 'processing', 'ready', 'failed', 'archived')),
            CONSTRAINT ck_projects_title_not_blank
                CHECK (length(btrim(title)) > 0)
        );
        """
    )
    op.execute("CREATE INDEX ix_projects_owner_id ON projects (owner_id);")
    op.execute("CREATE INDEX ix_projects_status ON projects (status);")
    op.execute("CREATE INDEX ix_projects_created_at ON projects (created_at);")
    op.execute(
        """
        CREATE TRIGGER trg_projects_set_updated_at
            BEFORE UPDATE ON projects
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS projects;")
    op.execute("DROP TABLE IF EXISTS users;")
