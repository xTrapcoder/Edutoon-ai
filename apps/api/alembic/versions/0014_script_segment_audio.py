"""script_segment_audio, and scripts.measured_duration_ms

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-28

Schema for Phase 8's local narration synthesis (Piper - see
apps/api/README.md, "Text-to-speech"). Purely additive: no existing column,
constraint or index is altered, so everything written before this migration
keeps its meaning.

``script_segments`` stays append-only (unchanged by this migration): a
regenerated script is a new ``scripts`` row with new segment rows, never an
edit of the old ones. Its audio is a different kind of data - the same
segment's audio genuinely gets *replaced* when re-synthesised with a new
voice/model/settings - so it lives in its own table rather than as columns on
``script_segments``, with one *current* row per segment
(``uq_script_segment_audio_script_segment_id``) that a re-synthesis
overwrites via ``INSERT ... ON CONFLICT ... DO UPDATE``, not a second row.

A row exists only once a synthesis attempt has *finished* (``ready`` or
``failed``) - there is no separate "pending" status/row: a segment with no
row yet is simply not started, exactly as absence already means "not done"
for a Piper voice, sample rate, or measured duration nobody has ever
computed. ``ck_script_segment_audio_ready_has_storage`` makes "ready" and
"has playable audio" the same fact at the database level, so a partial or
failed attempt can never be read back as complete (Phase 8 rule: partial
synthesis is never presented as complete).

``content_hash`` is ``sha256(narration_text + voice_model + settings)``,
computed in ``domain.script_audio`` and compared against the *current*
segment text/voice before a row is ever treated as valid - the DB only
stores it; it is never interpreted here. ``ON DELETE CASCADE`` from
``script_segments``: a segment's audio has no meaning once the segment
itself is gone (the same table it was measured against is gone too).

``scripts.measured_duration_ms`` is the sum of its segments' measured audio
durations, once every segment has valid audio - kept deliberately separate
from the existing (Phase 7) ``estimated_duration_ms``, which stays exactly
what it always was: the pre-audio, words-per-minute estimate. Neither column
is ever derived from the other.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE scripts ADD COLUMN measured_duration_ms bigint;
        """
    )
    op.execute(
        """
        ALTER TABLE scripts
            ADD CONSTRAINT ck_scripts_measured_duration_ms_non_negative
                CHECK (measured_duration_ms IS NULL OR measured_duration_ms >= 0);
        """
    )

    op.execute(
        """
        CREATE TABLE script_segment_audio (
            id                    uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            script_segment_id     uuid        NOT NULL,
            status                text        NOT NULL,
            storage_bucket        text,
            storage_key           text,
            format                text,
            sample_rate           integer,
            channels              integer,
            measured_duration_ms  bigint,
            voice_model           text        NOT NULL,
            is_temporary_voice_substitute boolean NOT NULL DEFAULT false,
            content_hash          text        NOT NULL,
            error                 text,
            created_at            timestamptz NOT NULL DEFAULT now(),
            updated_at            timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_script_segment_audio_script_segment_id_script_segments
                FOREIGN KEY (script_segment_id) REFERENCES script_segments (id)
                ON DELETE CASCADE,
            CONSTRAINT uq_script_segment_audio_script_segment_id
                UNIQUE (script_segment_id),
            CONSTRAINT ck_script_segment_audio_status
                CHECK (status IN ('ready', 'failed')),
            CONSTRAINT ck_script_segment_audio_measured_duration_ms_non_negative
                CHECK (measured_duration_ms IS NULL OR measured_duration_ms >= 0),
            CONSTRAINT ck_script_segment_audio_sample_rate_positive
                CHECK (sample_rate IS NULL OR sample_rate > 0),
            CONSTRAINT ck_script_segment_audio_channels_positive
                CHECK (channels IS NULL OR channels > 0),
            CONSTRAINT ck_script_segment_audio_ready_has_storage
                CHECK (
                    status <> 'ready'
                    OR (
                        storage_bucket IS NOT NULL
                        AND storage_key IS NOT NULL
                        AND format IS NOT NULL
                        AND sample_rate IS NOT NULL
                        AND channels IS NOT NULL
                        AND measured_duration_ms IS NOT NULL
                    )
                )
        );
        """
    )
    op.execute("CREATE INDEX ix_script_segment_audio_status ON script_segment_audio (status);")
    op.execute(
        """
        CREATE TRIGGER trg_script_segment_audio_set_updated_at
            BEFORE UPDATE ON script_segment_audio
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS script_segment_audio;")
    op.execute(
        "ALTER TABLE scripts DROP CONSTRAINT IF EXISTS "
        "ck_scripts_measured_duration_ms_non_negative;"
    )
    op.execute("ALTER TABLE scripts DROP COLUMN IF EXISTS measured_duration_ms;")
