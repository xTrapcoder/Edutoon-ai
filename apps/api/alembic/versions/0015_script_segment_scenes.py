"""script_segment_scenes, and the generate_storyboard job kind

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-28

Schema for Phase 9's Scene / Storyboard layer. Purely additive: no existing
column, constraint or index is altered, so everything written before this
migration keeps its meaning.

``script_segment_scenes`` turns one script segment's measured narration audio
(``script_segment_audio.measured_duration_ms``, Phase 8) into an ordered set of
fixed-template scenes, each with its own absolute ``start_ms`` and
``duration_ms`` (integer milliseconds throughout - rule 5) within the whole
timeline. A scene belongs to exactly one segment (``ON DELETE CASCADE`` - a
regenerated script's segments are new rows, so their old scenes simply cease
to exist with them, never orphaned) and never duplicates ``project_id`` /
``script_id``: those are reached by joining through
``script_segments -> scripts``, exactly like ``script_segment_audio``.

Unlike ``script_segments`` / ``script_segment_claims``, this table is **not**
append-only: a retry replaces one segment's whole scene set (DELETE + INSERT
inside one SAVEPOINT - see ``repositories.script_segment_scenes``), never
edits individual rows in place, so ``SUM(scene.duration_ms)`` for a segment
always equals its measured audio duration the moment a replacement commits.
``uq_script_segment_scenes_script_segment_id_scene_index`` keeps
``scene_index`` a well-defined 0-based order within a segment.

``template`` is a text column with a ``CHECK`` constraint (rule 7), never a
Postgres enum - the same discipline as every other status/kind column in this
schema - and the six values are the whole of Phase 9's fixed template
vocabulary (``domain.storyboard_template``); layout for a given
``(template, orientation)`` pair is deterministic code, never stored here.
``content`` is the template's validated structured slot data (rule: the
database stores content, never renderer coordinates or frame numbers -
those belong to a future rendering phase, per
``frame = round(t_ms * fps / 1000)``, rule 6). ``source_audio_content_hash``
is the staleness fingerprint: it is compared against the segment's *current*
``script_segment_audio.content_hash`` to decide whether a scene set survived
a re-synthesis or must be regenerated (see ``services.storyboard``).

``ck_jobs_kind`` (migration 0003, already widened by 0012) is widened again to
allow the new ``generate_storyboard`` job kind. Every existing row's ``kind``
is still one of the current seven values, so widening the constraint cannot
fail against current data.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_JOB_KINDS = (
    "parse_source",
    "extract_topics",
    "extract_claims",
    "build_evidence",
    "generate_script",
    "synthesise_voice",
    "render_video",
)
_NEW_JOB_KINDS = (*_OLD_JOB_KINDS, "generate_storyboard")

# Phase 9's fixed template vocabulary (domain.storyboard_template.TEMPLATES).
_TEMPLATES = (
    "title_card",
    "bullet_points",
    "labelled_diagram",
    "split_text_illustration",
    "full_bleed_illustration",
    "formula_or_code",
)


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE script_segment_scenes (
            id                        uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            script_segment_id         uuid        NOT NULL,
            scene_index               integer     NOT NULL,
            start_ms                  bigint      NOT NULL,
            duration_ms               bigint      NOT NULL,
            template                  text        NOT NULL,
            content                   jsonb       NOT NULL,
            source_audio_content_hash text        NOT NULL,
            created_at                timestamptz NOT NULL DEFAULT now(),
            updated_at                timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_script_segment_scenes_script_segment_id_script_segments
                FOREIGN KEY (script_segment_id) REFERENCES script_segments (id)
                ON DELETE CASCADE,
            CONSTRAINT uq_script_segment_scenes_script_segment_id_scene_index
                UNIQUE (script_segment_id, scene_index),
            CONSTRAINT ck_script_segment_scenes_duration_ms_positive
                CHECK (duration_ms > 0),
            CONSTRAINT ck_script_segment_scenes_start_ms_non_negative
                CHECK (start_ms >= 0),
            CONSTRAINT ck_script_segment_scenes_template CHECK (template IN (
                {", ".join(f"'{template}'" for template in _TEMPLATES)}
            )),
            CONSTRAINT ck_script_segment_scenes_content_is_object
                CHECK (jsonb_typeof(content) = 'object')
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_script_segment_scenes_script_segment_id "
        "ON script_segment_scenes (script_segment_id);"
    )
    op.execute(
        """
        CREATE TRIGGER trg_script_segment_scenes_set_updated_at
            BEFORE UPDATE ON script_segment_scenes
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )

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
    op.execute("DROP TABLE IF EXISTS script_segment_scenes;")
