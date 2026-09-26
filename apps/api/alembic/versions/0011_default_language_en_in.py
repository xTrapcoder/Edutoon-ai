"""default project language is en-IN

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-26

Makes ``en-IN`` the language a project gets when its creator does not choose one.
The API and repository already say ``en-IN`` (``domain.generation_settings``); this
brings the column default in the database into line, so a row inserted without a
language - by anything that skips the API - gets the same value.

Only the column default changes. ``SET DEFAULT`` never rewrites an existing row, so
every project keeps the language it has, ``en-GB`` included, and ``en-GB`` stays a
supported value: ``ck_projects_language`` (migration 0010) is untouched.

``downgrade`` restores the ``en-GB`` default and likewise leaves every row alone,
so a project created as ``en-IN`` in the meantime keeps it (the older constraint
allows it).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE projects ALTER COLUMN language SET DEFAULT 'en-IN';")


def downgrade() -> None:
    op.execute("ALTER TABLE projects ALTER COLUMN language SET DEFAULT 'en-GB';")
