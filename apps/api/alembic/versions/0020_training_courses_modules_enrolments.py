"""training courses, modules and course enrolments

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-13

Step 2 of the training platform (``docs/training-platform-design.md``). Purely additive: three new
tables, no existing table or constraint is altered, so everything written before this migration
keeps its meaning.

``training_courses`` is the ordered container; ``status`` is ``draft`` | ``published`` |
``archived``. Only a ``published`` course is visible to employees.

``training_modules`` is one published video inside one course. It is **never** the project's render:
publishing copies the MP4 to a key the module owns (``training/modules/{module_id}/...``) and the
module keeps its own snapshot of what that file is (size, duration, resolution, frame rate,
fingerprint). So a re-render of the source, or deleting the source project, cannot change or remove
a published video.

* ``course_id`` is NOT NULL and cascades: a module cannot exist outside a course, and deleting a
  course deletes its modules (the service collects their video locations before the commit and
  removes the files after it). **Step 3 note:** the assessment tables reference modules with
  ``ON DELETE RESTRICT``, so once a module has learner history its course cannot be deleted -
  *archive* it. That is the intended lifecycle, not an oversight.
* ``source_render_output_id`` is provenance only and ``ON DELETE SET NULL``: deleting the project
  that produced the render clears it and nothing else.
* ``position`` orders the modules of a course; ``UNIQUE (course_id, position)`` is
  ``DEFERRABLE INITIALLY DEFERRED`` so a reorder can swap positions inside one transaction.
* ``status`` is ``published`` | ``unpublished``. A published module has every snapshot column; an
  unpublished one has no ``video_bucket`` / ``video_key`` (its file is deleted) but keeps the
  descriptive snapshot as history.
* ``video_key`` must live under ``training/modules/{this module's id}/`` (a CHECK), so a module can
  never point into a project's own storage prefix - project clean-up cannot reach it, and a module
  clean-up cannot reach a project's objects - and two modules can never share one object
  (``uq_training_modules_video_key``).
* At most one *published* module per ``(course, source render)``
  (``uq_training_modules_course_source_render``): publishing the same render twice into a course is
  a conflict, not a silent duplicate. Replacing a module's video is an explicit act.

``course_enrolments`` is course-level only (no module-level enrolment). ``status`` is ``active`` |
``withdrawn``; withdrawing keeps the row. That the enrolled user is an active ``employee`` is
checked by the service (a CHECK cannot see another table's column).

Enumerations are text columns with CHECK constraints (rule 7); durations are integer milliseconds
(rule 5). ``downgrade`` drops the three tables in dependency order.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE training_courses (
            id          uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            title       text        NOT NULL,
            description text,
            status      text        NOT NULL DEFAULT 'draft',
            created_by  uuid        NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_training_courses_created_by_users
                FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE RESTRICT,
            CONSTRAINT ck_training_courses_title_not_blank CHECK (length(btrim(title)) > 0),
            CONSTRAINT ck_training_courses_status
                CHECK (status IN ('draft', 'published', 'archived'))
        );
        """
    )
    op.execute("CREATE INDEX ix_training_courses_created_at ON training_courses (created_at, id);")
    op.execute(
        """
        CREATE TRIGGER trg_training_courses_set_updated_at
            BEFORE UPDATE ON training_courses
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )

    op.execute(
        """
        CREATE TABLE training_modules (
            id                      uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            course_id               uuid        NOT NULL,
            position                integer     NOT NULL,
            title                   text        NOT NULL,
            description             text,
            status                  text        NOT NULL,
            source_render_output_id uuid,
            video_bucket            text,
            video_key               text,
            video_byte_size         bigint      NOT NULL,
            video_duration_ms       bigint      NOT NULL,
            video_width             integer     NOT NULL,
            video_height            integer     NOT NULL,
            video_fps               integer     NOT NULL,
            video_output_hash       text        NOT NULL,
            published_by            uuid        NOT NULL,
            published_at            timestamptz NOT NULL DEFAULT now(),
            unpublished_at          timestamptz,
            created_at              timestamptz NOT NULL DEFAULT now(),
            updated_at              timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_training_modules_course_id_training_courses
                FOREIGN KEY (course_id) REFERENCES training_courses (id) ON DELETE CASCADE,
            CONSTRAINT fk_training_modules_source_render_output_id_render_outputs
                FOREIGN KEY (source_render_output_id) REFERENCES render_outputs (id)
                ON DELETE SET NULL,
            CONSTRAINT fk_training_modules_published_by_users
                FOREIGN KEY (published_by) REFERENCES users (id) ON DELETE RESTRICT,
            CONSTRAINT uq_training_modules_course_id_position
                UNIQUE (course_id, position) DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT ck_training_modules_position_positive CHECK (position >= 1),
            CONSTRAINT ck_training_modules_title_not_blank CHECK (length(btrim(title)) > 0),
            CONSTRAINT ck_training_modules_status CHECK (status IN ('published', 'unpublished')),
            CONSTRAINT ck_training_modules_video_byte_size_positive CHECK (video_byte_size > 0),
            CONSTRAINT ck_training_modules_video_duration_ms_positive
                CHECK (video_duration_ms > 0),
            CONSTRAINT ck_training_modules_video_size_positive
                CHECK (video_width > 0 AND video_height > 0),
            CONSTRAINT ck_training_modules_video_fps_positive CHECK (video_fps > 0),
            CONSTRAINT ck_training_modules_video_state CHECK (
                (status = 'published'
                    AND video_bucket IS NOT NULL
                    AND video_key IS NOT NULL
                    AND unpublished_at IS NULL)
                OR
                (status = 'unpublished'
                    AND video_bucket IS NULL
                    AND video_key IS NULL
                    AND unpublished_at IS NOT NULL)
            ),
            CONSTRAINT ck_training_modules_video_key_is_module_owned CHECK (
                video_key IS NULL
                OR video_key LIKE 'training/modules/' || id::text || '/%'
            )
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_training_modules_source_render_output_id "
        "ON training_modules (source_render_output_id) WHERE source_render_output_id IS NOT NULL;"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_training_modules_video_key "
        "ON training_modules (video_bucket, video_key) WHERE video_key IS NOT NULL;"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_training_modules_course_source_render "
        "ON training_modules (course_id, source_render_output_id) "
        "WHERE status = 'published' AND source_render_output_id IS NOT NULL;"
    )
    op.execute(
        """
        CREATE TRIGGER trg_training_modules_set_updated_at
            BEFORE UPDATE ON training_modules
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )

    op.execute(
        """
        CREATE TABLE course_enrolments (
            id          uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            course_id   uuid        NOT NULL,
            user_id     uuid        NOT NULL,
            status      text        NOT NULL DEFAULT 'active',
            assigned_by uuid        NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_course_enrolments_course_id_training_courses
                FOREIGN KEY (course_id) REFERENCES training_courses (id) ON DELETE CASCADE,
            CONSTRAINT fk_course_enrolments_user_id_users
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT,
            CONSTRAINT fk_course_enrolments_assigned_by_users
                FOREIGN KEY (assigned_by) REFERENCES users (id) ON DELETE RESTRICT,
            CONSTRAINT uq_course_enrolments_course_id_user_id UNIQUE (course_id, user_id),
            CONSTRAINT ck_course_enrolments_status CHECK (status IN ('active', 'withdrawn'))
        );
        """
    )
    op.execute("CREATE INDEX ix_course_enrolments_user_id ON course_enrolments (user_id);")
    op.execute(
        """
        CREATE TRIGGER trg_course_enrolments_set_updated_at
            BEFORE UPDATE ON course_enrolments
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS course_enrolments;")
    op.execute("DROP TABLE IF EXISTS training_modules;")
    op.execute("DROP TABLE IF EXISTS training_courses;")
