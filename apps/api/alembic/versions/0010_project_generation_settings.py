"""project generation settings: orientation, target duration, supported languages

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-26

Gives a project the settings that later phases (scene planning, narration,
rendering) read to decide what to produce. They are columns on ``projects``:
they describe the project, are read with it, and have no life of their own.

``orientation`` is ``horizontal`` (landscape) or ``vertical`` (portrait),
defaulting to ``horizontal``. As with every enumeration in this schema it is
``text`` plus a ``CHECK``, never a Postgres enum type.

``target_duration_ms`` is how long the user wants the finished video to run, in
integer milliseconds (rule 5). It is bounded to one minute (60000) through sixty
minutes (3600000) inclusive, and defaults to five minutes (300000).

``language`` already exists (migration 0002, default ``'en-GB'``) as unconstrained
text, and is what topic extraction hands the model. Rather than add a second
language column, this migration constrains that one to the supported set. Any
existing row holding something outside the set is reset to the ``'en-GB'`` default
first: the value was never validated, no code path acted on anything but the
default, and leaving it would make the constraint impossible to add. ``downgrade``
drops the constraint but cannot restore a value that was reset.

Adding a language later is one more entry in ``domain.generation_settings`` and a
new migration that widens ``ck_projects_language``.

Existing rows take the column defaults, so every project written before this
migration keeps working unchanged. The ``projects`` ``set_updated_at`` trigger
fires only for rows the language reset touches.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE projects ADD COLUMN orientation text NOT NULL DEFAULT 'horizontal';")
    op.execute(
        """
        ALTER TABLE projects
            ADD CONSTRAINT ck_projects_orientation
                CHECK (orientation IN ('horizontal', 'vertical'));
        """
    )

    op.execute("ALTER TABLE projects ADD COLUMN target_duration_ms bigint NOT NULL DEFAULT 300000;")
    op.execute(
        """
        ALTER TABLE projects
            ADD CONSTRAINT ck_projects_target_duration_ms_range
                CHECK (target_duration_ms BETWEEN 60000 AND 3600000);
        """
    )

    op.execute("UPDATE projects SET language = 'en-GB' WHERE language NOT IN ('en-GB', 'en-IN');")
    op.execute(
        """
        ALTER TABLE projects
            ADD CONSTRAINT ck_projects_language
                CHECK (language IN ('en-GB', 'en-IN'));
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE projects DROP CONSTRAINT IF EXISTS ck_projects_language;")
    op.execute(
        "ALTER TABLE projects DROP CONSTRAINT IF EXISTS ck_projects_target_duration_ms_range;"
    )
    op.execute("ALTER TABLE projects DROP COLUMN IF EXISTS target_duration_ms;")
    op.execute("ALTER TABLE projects DROP CONSTRAINT IF EXISTS ck_projects_orientation;")
    op.execute("ALTER TABLE projects DROP COLUMN IF EXISTS orientation;")
