"""scripts: pending no longer counts as "active"

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-28

Phase 7 (duration-aware, topic-scoped script generation) needs a currently
``approved`` (or ``draft``) script to survive untouched for the whole time a
new generation is running, so a failed regeneration can never leave a project
with no usable script (see ``services.scripts`` module docstring, "Atomicity").

Under the original ``uq_scripts_project_id_active`` (migration 0007), a
project may have at most one script in ``pending`` / ``draft`` / ``approved``
at once - so queuing a new generation had to supersede the old ``approved``
script *before* the new one existed, immediately at enqueue time, long before
anything was validated. A failed generation then left the project with
nothing ``approved`` at all: the old script ``superseded``, the new one
``failed``.

Narrowing the index to ``draft`` / ``approved`` only lets a fresh ``pending``
placeholder coexist with the still-``approved`` script the whole time
generation runs. ``services.scripts.enqueue_script_generation`` no longer
supersedes anything; ``repositories.scripts.attach_draft`` now supersedes the
old script itself, inside the same ``SAVEPOINT`` that writes the new one's
segments and flips it to ``draft`` - so the old script is only ever touched
in the same atomic step that proves the new one is ready to replace it.
``draft`` / ``approved`` still exclude each other exactly as before: two
pieces of *actual, reviewable content* can still never coexist.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_scripts_project_id_active;")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_scripts_project_id_active
            ON scripts (project_id)
            WHERE status IN ('draft', 'approved');
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_scripts_project_id_active;")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_scripts_project_id_active
            ON scripts (project_id)
            WHERE status IN ('pending', 'draft', 'approved');
        """
    )
