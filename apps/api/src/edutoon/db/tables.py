"""Core ``Table`` objects mirroring the hand-written migrations exactly.

No ORM models exist yet (see ``db/base.py``), so repositories query through
SQLAlchemy Core against these definitions. They live on their own
:class:`MetaData`, deliberately **not** ``Base.metadata`` — that one is
Alembic's ``target_metadata``, and migrations stay hand-written (no
autogenerate), so it must stay empty rather than pick up query-only column
lists that could drift from the DDL.

Only columns actually queried by ``repositories/`` are declared. Extend as
new columns are needed — this is not a substitute for the migrations, which
remain the source of truth for the schema.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID

metadata = sa.MetaData()

users = sa.Table(
    "users",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.FetchedValue()),
    sa.Column("clerk_user_id", sa.Text),
    sa.Column("email", sa.Text, nullable=False),
    sa.Column("display_name", sa.Text),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

projects = sa.Table(
    "projects",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.FetchedValue()),
    sa.Column("owner_id", UUID(as_uuid=True), nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("description", sa.Text),
    sa.Column("source_type", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("language", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

jobs = sa.Table(
    "jobs",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.FetchedValue()),
    sa.Column("project_id", UUID(as_uuid=True), nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("priority", sa.Integer, nullable=False),
    sa.Column("attempts", sa.Integer, nullable=False),
    sa.Column("max_attempts", sa.Integer, nullable=False),
    sa.Column("idempotency_key", sa.Text),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("result", JSONB),
    sa.Column("error", sa.Text),
    sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

project_topics = sa.Table(
    "project_topics",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.FetchedValue()),
    sa.Column("project_id", UUID(as_uuid=True), nullable=False),
    sa.Column("parent_id", UUID(as_uuid=True)),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("summary", sa.Text),
    sa.Column("position", sa.Integer, nullable=False),
    sa.Column("depth", sa.Integer, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

uploaded_sources = sa.Table(
    "uploaded_sources",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.FetchedValue()),
    sa.Column("project_id", UUID(as_uuid=True), nullable=False),
    sa.Column("uploader_id", UUID(as_uuid=True)),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("original_filename", sa.Text, nullable=False),
    sa.Column("content_type", sa.Text, nullable=False),
    sa.Column("storage_bucket", sa.Text, nullable=False),
    sa.Column("storage_key", sa.Text, nullable=False),
    sa.Column("byte_size", sa.BigInteger, nullable=False),
    sa.Column("checksum_sha256", sa.Text, nullable=False),
    sa.Column("page_count", sa.Integer),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

# ``embedding`` (pgvector) is deliberately omitted: no embedding provider is
# wired up yet (Phase 2 has no business logic), and the ``pgvector`` Python
# package isn't a dependency. Add both together when that work starts.
source_chunks = sa.Table(
    "source_chunks",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.FetchedValue()),
    sa.Column("source_id", UUID(as_uuid=True), nullable=False),
    sa.Column("project_id", UUID(as_uuid=True), nullable=False),
    sa.Column("chunk_index", sa.Integer, nullable=False),
    sa.Column("page_from", sa.Integer),
    sa.Column("page_to", sa.Integer),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("token_count", sa.Integer),
    sa.Column("embedding_model", sa.Text),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

# Append-only (rule 4 / trg_audit_logs_append_only): repositories must only
# ever INSERT or SELECT this table, never UPDATE/DELETE.
audit_logs = sa.Table(
    "audit_logs",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.FetchedValue()),
    sa.Column("actor_id", UUID(as_uuid=True)),
    sa.Column("actor_type", sa.Text, nullable=False),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("entity_type", sa.Text, nullable=False),
    sa.Column("entity_id", UUID(as_uuid=True)),
    sa.Column("project_id", UUID(as_uuid=True)),
    sa.Column("request_id", sa.Text),
    sa.Column("ip_address", INET),
    sa.Column("context", JSONB, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
