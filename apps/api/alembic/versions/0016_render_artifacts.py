"""render_chunks, render_outputs, render_output_scenes

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-29

Schema for Phase 10's video rendering. Purely additive: no existing column, constraint or
index is altered (``ck_jobs_kind`` already allows ``render_video`` - migration 0003), so
everything written before this migration keeps its meaning. No video bytes live in the
database: every row below only *names* an object in object storage.

``render_chunks`` is a **content-addressed cache** of rendered scene videos. A chunk is one
Phase 9 scene drawn to a video-only MP4 and stored at a key derived from its fingerprint
(``chunk_hash`` - a hash of every input that decides its bytes: template, content, duration,
orientation, quality, encoder, frame rate and renderer version; see ``domain.render``). It
is not tied to a scene row, so a storyboard regeneration that produces the same scene finds
its chunk again, and two identical scenes share one. ``(project_id, chunk_hash)`` is unique;
a row exists only once its object has been uploaded (the row is the commit), so a crash
mid-upload leaves no row and the chunk is simply rendered again. Deleting a project removes
its chunks (``ON DELETE CASCADE``); the service removes the objects after the delete commits,
exactly as it does for uploads and narration audio.

``render_outputs`` is the finished MP4 for one ``(script, orientation, quality)``: at most
one current row (``uq_render_outputs_script_id_orientation_quality``), replaced in place by a
newer render. ``output_hash`` fingerprints the ordered chunks plus the narration, so the
video is rebuilt exactly when one of them, the renderer or the encoder changes. ``probe``
holds the small ffprobe summary the render was verified against (a few dozen bytes of
JSON - never render state that grows with the video).

``render_output_scenes`` records, per output, which chunk fingerprint each scene used
(``script_segment_id`` + ``scene_index`` -> ``chunk_hash``). That is what lets the workspace
say *this* scene is stale - it changed since the last video - without marking the whole
video stale, and what protects a chunk from garbage collection while a video still uses it.
It holds one small row per scene, never a blob. ``script_segment_id`` is deliberately not a
foreign key: the row describes the video that was made, and cascades away with the output.

Orientation, quality and the render statuses are text columns with CHECK constraints
(rule 7), never Postgres enum types.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE render_chunks (
            id               uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            project_id       uuid        NOT NULL,
            chunk_hash       text        NOT NULL,
            orientation      text        NOT NULL,
            quality          text        NOT NULL,
            renderer_version text        NOT NULL,
            storage_bucket   text        NOT NULL,
            storage_key      text        NOT NULL,
            byte_size        bigint      NOT NULL,
            frame_count      integer     NOT NULL,
            duration_ms      bigint      NOT NULL,
            created_at       timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_render_chunks_project_id_projects
                FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE,
            CONSTRAINT uq_render_chunks_project_id_chunk_hash UNIQUE (project_id, chunk_hash),
            CONSTRAINT ck_render_chunks_orientation
                CHECK (orientation IN ('horizontal', 'vertical')),
            CONSTRAINT ck_render_chunks_quality CHECK (quality IN ('low', 'high')),
            CONSTRAINT ck_render_chunks_byte_size_positive CHECK (byte_size > 0),
            CONSTRAINT ck_render_chunks_frame_count_positive CHECK (frame_count > 0),
            CONSTRAINT ck_render_chunks_duration_ms_positive CHECK (duration_ms > 0)
        );
        """
    )
    op.execute("CREATE INDEX ix_render_chunks_project_id ON render_chunks (project_id);")

    op.execute(
        """
        CREATE TABLE render_outputs (
            id               uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            project_id       uuid        NOT NULL,
            script_id        uuid        NOT NULL,
            job_id           uuid,
            orientation      text        NOT NULL,
            quality          text        NOT NULL,
            output_hash      text        NOT NULL,
            renderer_version text        NOT NULL,
            encoder          text        NOT NULL,
            fps              integer     NOT NULL,
            width            integer     NOT NULL,
            height           integer     NOT NULL,
            duration_ms      bigint      NOT NULL,
            byte_size        bigint      NOT NULL,
            storage_bucket   text        NOT NULL,
            storage_key      text        NOT NULL,
            probe            jsonb       NOT NULL,
            created_at       timestamptz NOT NULL DEFAULT now(),
            updated_at       timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_render_outputs_project_id_projects
                FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE,
            CONSTRAINT fk_render_outputs_script_id_scripts
                FOREIGN KEY (script_id) REFERENCES scripts (id) ON DELETE CASCADE,
            CONSTRAINT fk_render_outputs_job_id_jobs
                FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE SET NULL,
            CONSTRAINT uq_render_outputs_script_id_orientation_quality
                UNIQUE (script_id, orientation, quality),
            CONSTRAINT ck_render_outputs_orientation
                CHECK (orientation IN ('horizontal', 'vertical')),
            CONSTRAINT ck_render_outputs_quality CHECK (quality IN ('low', 'high')),
            CONSTRAINT ck_render_outputs_encoder CHECK (encoder IN ('libx264', 'h264_nvenc')),
            CONSTRAINT ck_render_outputs_fps_positive CHECK (fps > 0),
            CONSTRAINT ck_render_outputs_size_positive CHECK (width > 0 AND height > 0),
            CONSTRAINT ck_render_outputs_duration_ms_positive CHECK (duration_ms > 0),
            CONSTRAINT ck_render_outputs_byte_size_positive CHECK (byte_size > 0),
            CONSTRAINT ck_render_outputs_probe_is_object CHECK (jsonb_typeof(probe) = 'object')
        );
        """
    )
    op.execute("CREATE INDEX ix_render_outputs_project_id ON render_outputs (project_id);")
    op.execute(
        """
        CREATE TRIGGER trg_render_outputs_set_updated_at
            BEFORE UPDATE ON render_outputs
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )

    op.execute(
        """
        CREATE TABLE render_output_scenes (
            render_output_id  uuid    NOT NULL,
            position          integer NOT NULL,
            script_segment_id uuid    NOT NULL,
            scene_index       integer NOT NULL,
            chunk_hash        text    NOT NULL,
            CONSTRAINT pk_render_output_scenes PRIMARY KEY (render_output_id, position),
            CONSTRAINT fk_render_output_scenes_render_output_id_render_outputs
                FOREIGN KEY (render_output_id) REFERENCES render_outputs (id) ON DELETE CASCADE,
            CONSTRAINT ck_render_output_scenes_position_non_negative CHECK (position >= 0),
            CONSTRAINT ck_render_output_scenes_scene_index_non_negative CHECK (scene_index >= 0)
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_render_output_scenes_chunk_hash ON render_output_scenes (chunk_hash);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS render_output_scenes;")
    op.execute("DROP TABLE IF EXISTS render_outputs;")
    op.execute("DROP TABLE IF EXISTS render_chunks;")
