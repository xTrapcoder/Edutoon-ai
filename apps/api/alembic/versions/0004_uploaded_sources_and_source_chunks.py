"""uploaded_sources and source_chunks

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-02

``source_chunks.embedding`` is a fixed 1536-dimension ``vector`` column
(OpenAI ``text-embedding-3-small`` / ``ada-002`` size). Revisit if the
embedding provider changes — pgvector columns are fixed-width.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIMENSIONS = 1536


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE uploaded_sources (
            id                uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            project_id        uuid        NOT NULL,
            uploader_id       uuid,
            kind              text        NOT NULL DEFAULT 'pdf',
            original_filename text        NOT NULL,
            content_type      text        NOT NULL DEFAULT 'application/pdf',
            storage_bucket    text        NOT NULL,
            storage_key       text        NOT NULL,
            byte_size         bigint      NOT NULL,
            checksum_sha256   text        NOT NULL,
            page_count        integer,
            status            text        NOT NULL DEFAULT 'pending',
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_uploaded_sources_project_id_projects
                FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE,
            CONSTRAINT fk_uploaded_sources_uploader_id_users
                FOREIGN KEY (uploader_id) REFERENCES users (id) ON DELETE SET NULL,
            CONSTRAINT ck_uploaded_sources_kind CHECK (kind IN ('pdf')),
            CONSTRAINT ck_uploaded_sources_status
                CHECK (status IN ('pending', 'processing', 'parsed', 'failed')),
            CONSTRAINT ck_uploaded_sources_byte_size_positive CHECK (byte_size > 0),
            CONSTRAINT ck_uploaded_sources_page_count_non_negative
                CHECK (page_count IS NULL OR page_count >= 0),
            CONSTRAINT ck_uploaded_sources_checksum_sha256_shape
                CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_uploaded_sources_filename_not_blank
                CHECK (length(btrim(original_filename)) > 0)
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_uploaded_sources_storage_bucket_storage_key
            ON uploaded_sources (storage_bucket, storage_key);
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_uploaded_sources_project_id_checksum_sha256
            ON uploaded_sources (project_id, checksum_sha256);
        """
    )
    op.execute("CREATE INDEX ix_uploaded_sources_project_id ON uploaded_sources (project_id);")
    op.execute("CREATE INDEX ix_uploaded_sources_uploader_id ON uploaded_sources (uploader_id);")
    op.execute(
        """
        CREATE TRIGGER trg_uploaded_sources_set_updated_at
            BEFORE UPDATE ON uploaded_sources
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )

    op.execute(
        f"""
        CREATE TABLE source_chunks (
            id              uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            source_id       uuid        NOT NULL,
            project_id      uuid        NOT NULL,
            chunk_index     integer     NOT NULL,
            page_from       integer,
            page_to         integer,
            content         text        NOT NULL,
            token_count     integer,
            embedding       vector({EMBEDDING_DIMENSIONS}),
            embedding_model text,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_source_chunks_source_id_uploaded_sources
                FOREIGN KEY (source_id) REFERENCES uploaded_sources (id) ON DELETE CASCADE,
            CONSTRAINT fk_source_chunks_project_id_projects
                FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE,
            CONSTRAINT ck_source_chunks_chunk_index_non_negative CHECK (chunk_index >= 0),
            CONSTRAINT ck_source_chunks_page_from_positive
                CHECK (page_from IS NULL OR page_from >= 1),
            CONSTRAINT ck_source_chunks_page_to_positive
                CHECK (page_to IS NULL OR page_to >= 1),
            CONSTRAINT ck_source_chunks_page_range
                CHECK (page_from IS NULL OR page_to IS NULL OR page_to >= page_from),
            CONSTRAINT ck_source_chunks_token_count_non_negative
                CHECK (token_count IS NULL OR token_count >= 0),
            CONSTRAINT ck_source_chunks_content_not_blank CHECK (length(content) > 0),
            CONSTRAINT ck_source_chunks_embedding_has_model
                CHECK (embedding IS NULL OR embedding_model IS NOT NULL)
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_source_chunks_source_id_chunk_index
            ON source_chunks (source_id, chunk_index);
        """
    )
    op.execute("CREATE INDEX ix_source_chunks_project_id ON source_chunks (project_id);")
    op.execute("CREATE INDEX ix_source_chunks_source_id ON source_chunks (source_id);")
    op.execute(
        """
        CREATE INDEX ix_source_chunks_content_trgm
            ON source_chunks USING gin (content gin_trgm_ops);
        """
    )
    op.execute(
        """
        CREATE INDEX ix_source_chunks_embedding_cosine
            ON source_chunks USING hnsw (embedding vector_cosine_ops);
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_source_chunks_set_updated_at
            BEFORE UPDATE ON source_chunks
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS source_chunks;")
    op.execute("DROP TABLE IF EXISTS uploaded_sources;")
