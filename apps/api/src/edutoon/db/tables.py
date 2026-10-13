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
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID

EMBEDDING_DIMENSIONS = 1536

metadata = sa.MetaData()

users = sa.Table(
    "users",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.FetchedValue()),
    sa.Column("clerk_user_id", sa.Text),
    sa.Column("email", sa.Text, nullable=False),
    sa.Column("display_name", sa.Text),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

invitations = sa.Table(
    "invitations",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.FetchedValue()),
    sa.Column("email", sa.Text, nullable=False),
    sa.Column("display_name", sa.Text),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("invited_by", UUID(as_uuid=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("accepted_user_id", UUID(as_uuid=True)),
    sa.Column("accepted_at", sa.DateTime(timezone=True)),
    sa.Column("clerk_invitation_id", sa.Text),
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
    sa.Column("orientation", sa.Text, nullable=False),
    sa.Column("target_duration_ms", sa.BigInteger, nullable=False),
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
    # Migration 0009. ``origin`` is text + CHECK ('source' | 'user' |
    # 'suggested'), never a Postgres enum; ``keywords`` is a native text[]
    # (never NULL, no NULL elements).
    sa.Column("origin", sa.Text, nullable=False),
    sa.Column("keywords", ARRAY(sa.Text), nullable=False),
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
    sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS)),
    sa.Column("embedding_model", sa.Text),
    # Migration 0017. A deck slide's structure (domain.deck); NULL for a PDF page.
    sa.Column("structure", JSONB(none_as_null=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

claim_verifications = sa.Table(
    "claim_verifications",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.FetchedValue()),
    sa.Column("project_id", UUID(as_uuid=True), nullable=False),
    sa.Column("claim_text", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("rationale", sa.Text),
    sa.Column("embedding_model", sa.Text, nullable=False),
    sa.Column("llm_model", sa.Text, nullable=False),
    # Migration 0012. NULL for a claim typed in by hand through the manual
    # verification route; set for one extracted from a topic (Phase 6).
    # ON DELETE SET NULL - a claim's own verdict and citations outlive the
    # topic it came from.
    sa.Column("topic_id", UUID(as_uuid=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

# Append-only in practice (a re-verification is a new row, never an update) -
# no DB-level guard trigger, unlike audit_logs: these rows carry real FKs to
# live, owned resources, so the application-level guarantee (repositories
# never issue UPDATE/DELETE) is what enforces it.
claim_citations = sa.Table(
    "claim_citations",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.FetchedValue()),
    sa.Column("claim_verification_id", UUID(as_uuid=True), nullable=False),
    sa.Column("source_chunk_id", UUID(as_uuid=True), nullable=False),
    sa.Column("similarity", sa.REAL, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

# Which source_chunks a topic was drawn from (migration 0009). Derived and
# regenerable, so - unlike claim_citations - it cascades from both parents
# and a whole project's evidence may be cleared and rebuilt. Rows are never
# UPDATEd, only inserted or deleted.
topic_citations = sa.Table(
    "topic_citations",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.FetchedValue()),
    sa.Column("topic_id", UUID(as_uuid=True), nullable=False),
    sa.Column("source_chunk_id", UUID(as_uuid=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
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

# Script Generation Engine (migration 0007). ``scripts`` is a status machine
# (``pending -> draft -> approved`` / ``failed`` / ``superseded``) with a
# ``set_updated_at`` trigger, like ``jobs`` / ``projects``.
scripts = sa.Table(
    "scripts",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.FetchedValue()),
    sa.Column("project_id", UUID(as_uuid=True), nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("llm_model", sa.Text, nullable=False),
    sa.Column("estimated_duration_ms", sa.BigInteger),
    # Migration 0014 (Phase 8). The sum of the script's segments' measured
    # audio durations, once every segment has valid audio - never derived
    # from estimated_duration_ms, which stays the Phase 7 WPM estimate.
    sa.Column("measured_duration_ms", sa.BigInteger),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

# Append-only (same discipline as claim_citations): repositories only ever
# INSERT or SELECT script_segments / script_segment_claims. A regenerated
# script is a new ``scripts`` row, never an edit of these child rows.
script_segments = sa.Table(
    "script_segments",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.FetchedValue()),
    sa.Column("script_id", UUID(as_uuid=True), nullable=False),
    sa.Column("segment_index", sa.Integer, nullable=False),
    sa.Column("narration_text", sa.Text, nullable=False),
    sa.Column("estimated_duration_ms", sa.BigInteger, nullable=False),
    sa.Column("topic_id", UUID(as_uuid=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

# The traceability link back to the Evidence Engine: every row points at a
# real claim_verifications row (FK ON DELETE CASCADE since migration 0018, so a
# deleted project takes its script's claim links with it).
script_segment_claims = sa.Table(
    "script_segment_claims",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.FetchedValue()),
    sa.Column("script_segment_id", UUID(as_uuid=True), nullable=False),
    sa.Column("claim_verification_id", UUID(as_uuid=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

# Migration 0014 (Phase 8). One *current* row per segment (unlike
# script_segments/script_segment_claims, this table is not append-only: a
# re-synthesis overwrites its segment's row via INSERT ... ON CONFLICT ...
# DO UPDATE). A row exists only once an attempt has finished - "ready" or
# "failed" - never a separate "pending" state; absence means not yet
# attempted.
script_segment_audio = sa.Table(
    "script_segment_audio",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.FetchedValue()),
    sa.Column("script_segment_id", UUID(as_uuid=True), nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("storage_bucket", sa.Text),
    sa.Column("storage_key", sa.Text),
    sa.Column("format", sa.Text),
    sa.Column("sample_rate", sa.Integer),
    sa.Column("channels", sa.Integer),
    sa.Column("measured_duration_ms", sa.BigInteger),
    sa.Column("voice_model", sa.Text, nullable=False),
    sa.Column("is_temporary_voice_substitute", sa.Boolean, nullable=False),
    sa.Column("content_hash", sa.Text, nullable=False),
    sa.Column("error", sa.Text),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

# Migration 0015 (Phase 9). Not append-only, like script_segment_audio: a
# retry replaces one segment's whole scene set via DELETE + INSERT inside a
# single SAVEPOINT (repositories.script_segment_scenes.replace_for_segment),
# never an UPDATE of individual rows.
script_segment_scenes = sa.Table(
    "script_segment_scenes",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.FetchedValue()),
    sa.Column("script_segment_id", UUID(as_uuid=True), nullable=False),
    sa.Column("scene_index", sa.Integer, nullable=False),
    sa.Column("start_ms", sa.BigInteger, nullable=False),
    sa.Column("duration_ms", sa.BigInteger, nullable=False),
    sa.Column("template", sa.Text, nullable=False),
    sa.Column("content", JSONB, nullable=False),
    sa.Column("source_audio_content_hash", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


# Migration 0016 (Phase 10). ``render_chunks`` is a content-addressed cache of rendered scene
# videos (one row per ``(project_id, chunk_hash)``, inserted only once its object is uploaded);
# ``render_outputs`` is the finished MP4 for one ``(script, orientation, quality)``;
# ``render_output_scenes`` records which chunk each scene used in that output. No video bytes
# live in the database - only the names of stored objects.
render_chunks = sa.Table(
    "render_chunks",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.FetchedValue()),
    sa.Column("project_id", UUID(as_uuid=True), nullable=False),
    sa.Column("chunk_hash", sa.Text, nullable=False),
    sa.Column("orientation", sa.Text, nullable=False),
    sa.Column("quality", sa.Text, nullable=False),
    sa.Column("renderer_version", sa.Text, nullable=False),
    sa.Column("storage_bucket", sa.Text, nullable=False),
    sa.Column("storage_key", sa.Text, nullable=False),
    sa.Column("byte_size", sa.BigInteger, nullable=False),
    sa.Column("frame_count", sa.Integer, nullable=False),
    sa.Column("duration_ms", sa.BigInteger, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

render_outputs = sa.Table(
    "render_outputs",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.FetchedValue()),
    sa.Column("project_id", UUID(as_uuid=True), nullable=False),
    sa.Column("script_id", UUID(as_uuid=True), nullable=False),
    sa.Column("job_id", UUID(as_uuid=True)),
    sa.Column("orientation", sa.Text, nullable=False),
    sa.Column("quality", sa.Text, nullable=False),
    sa.Column("output_hash", sa.Text, nullable=False),
    sa.Column("renderer_version", sa.Text, nullable=False),
    sa.Column("encoder", sa.Text, nullable=False),
    sa.Column("fps", sa.Integer, nullable=False),
    sa.Column("width", sa.Integer, nullable=False),
    sa.Column("height", sa.Integer, nullable=False),
    sa.Column("duration_ms", sa.BigInteger, nullable=False),
    sa.Column("byte_size", sa.BigInteger, nullable=False),
    sa.Column("storage_bucket", sa.Text, nullable=False),
    sa.Column("storage_key", sa.Text, nullable=False),
    sa.Column("probe", JSONB, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

render_output_scenes = sa.Table(
    "render_output_scenes",
    metadata,
    sa.Column("render_output_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("position", sa.Integer, primary_key=True),
    sa.Column("script_segment_id", UUID(as_uuid=True), nullable=False),
    sa.Column("scene_index", sa.Integer, nullable=False),
    sa.Column("chunk_hash", sa.Text, nullable=False),
)

training_courses = sa.Table(
    "training_courses",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.FetchedValue()),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("description", sa.Text),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("created_by", UUID(as_uuid=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

training_modules = sa.Table(
    "training_modules",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.FetchedValue()),
    sa.Column("course_id", UUID(as_uuid=True), nullable=False),
    sa.Column("position", sa.Integer, nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("description", sa.Text),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("source_render_output_id", UUID(as_uuid=True)),
    sa.Column("video_bucket", sa.Text),
    sa.Column("video_key", sa.Text),
    sa.Column("video_byte_size", sa.BigInteger, nullable=False),
    sa.Column("video_duration_ms", sa.BigInteger, nullable=False),
    sa.Column("video_width", sa.Integer, nullable=False),
    sa.Column("video_height", sa.Integer, nullable=False),
    sa.Column("video_fps", sa.Integer, nullable=False),
    sa.Column("video_output_hash", sa.Text, nullable=False),
    sa.Column("published_by", UUID(as_uuid=True), nullable=False),
    sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("unpublished_at", sa.DateTime(timezone=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

course_enrolments = sa.Table(
    "course_enrolments",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.FetchedValue()),
    sa.Column("course_id", UUID(as_uuid=True), nullable=False),
    sa.Column("user_id", UUID(as_uuid=True), nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("assigned_by", UUID(as_uuid=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)
