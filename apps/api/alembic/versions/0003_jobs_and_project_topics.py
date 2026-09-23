"""jobs and project_topics

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE jobs (
            id              uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            project_id      uuid        NOT NULL,
            kind            text        NOT NULL,
            status          text        NOT NULL DEFAULT 'queued',
            priority        integer     NOT NULL DEFAULT 100,
            attempts        integer     NOT NULL DEFAULT 0,
            max_attempts    integer     NOT NULL DEFAULT 3,
            idempotency_key text,
            payload         jsonb       NOT NULL DEFAULT '{}'::jsonb,
            result          jsonb,
            error           text,
            scheduled_at    timestamptz NOT NULL DEFAULT now(),
            started_at      timestamptz,
            finished_at     timestamptz,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_jobs_project_id_projects
                FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE,
            CONSTRAINT ck_jobs_kind CHECK (kind IN (
                'parse_source', 'extract_topics', 'build_evidence',
                'generate_script', 'synthesise_voice', 'render_video'
            )),
            CONSTRAINT ck_jobs_status CHECK (status IN (
                'queued', 'running', 'succeeded', 'failed', 'cancelled'
            )),
            CONSTRAINT ck_jobs_attempts_non_negative CHECK (attempts >= 0),
            CONSTRAINT ck_jobs_max_attempts_positive CHECK (max_attempts >= 1),
            CONSTRAINT ck_jobs_attempts_within_max CHECK (attempts <= max_attempts),
            CONSTRAINT ck_jobs_payload_is_object
                CHECK (jsonb_typeof(payload) = 'object'),
            CONSTRAINT ck_jobs_finished_requires_started
                CHECK (finished_at IS NULL OR started_at IS NOT NULL)
        );
        """
    )
    op.execute("CREATE INDEX ix_jobs_project_id ON jobs (project_id);")
    op.execute("CREATE INDEX ix_jobs_status_scheduled_at ON jobs (status, scheduled_at);")
    op.execute("CREATE INDEX ix_jobs_kind ON jobs (kind);")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_jobs_project_id_kind_idempotency_key
            ON jobs (project_id, kind, idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_jobs_set_updated_at
            BEFORE UPDATE ON jobs
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )

    op.execute(
        """
        CREATE TABLE project_topics (
            id          uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            project_id  uuid        NOT NULL,
            parent_id   uuid,
            title       text        NOT NULL,
            summary     text,
            position    integer     NOT NULL DEFAULT 0,
            depth       integer     NOT NULL DEFAULT 0,
            status      text        NOT NULL DEFAULT 'proposed',
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_project_topics_project_id_projects
                FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE,
            CONSTRAINT fk_project_topics_parent_id_project_topics
                FOREIGN KEY (parent_id) REFERENCES project_topics (id) ON DELETE CASCADE,
            CONSTRAINT ck_project_topics_status
                CHECK (status IN ('proposed', 'accepted', 'rejected')),
            CONSTRAINT ck_project_topics_position_non_negative CHECK (position >= 0),
            CONSTRAINT ck_project_topics_depth_non_negative CHECK (depth >= 0),
            CONSTRAINT ck_project_topics_not_own_parent
                CHECK (parent_id IS NULL OR parent_id <> id),
            CONSTRAINT ck_project_topics_title_not_blank
                CHECK (length(btrim(title)) > 0)
        );
        """
    )
    op.execute("CREATE INDEX ix_project_topics_project_id ON project_topics (project_id);")
    op.execute("CREATE INDEX ix_project_topics_parent_id ON project_topics (parent_id);")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_project_topics_project_id_parent_id_position
            ON project_topics (project_id, parent_id, position)
            NULLS NOT DISTINCT;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_project_topics_set_updated_at
            BEFORE UPDATE ON project_topics
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS project_topics;")
    op.execute("DROP TABLE IF EXISTS jobs;")
