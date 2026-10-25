"""Pasted text as a source: the ``text`` source kind and project type

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-16

Lets a project's source be text the owner pasted in, which is chunked and indexed exactly like a
PDF's pages (``domain.pasted_text``). Purely additive: no table or column is added (a pasted
chunk has no page, and ``source_chunks.page_from`` / ``page_to`` are already nullable), and
nothing written before this migration changes meaning.

``ck_projects_source_type`` and ``ck_uploaded_sources_kind`` are widened to allow ``text``.
Every existing row is ``pdf``, ``pptx`` or ``topic``, so widening cannot fail against current
data; ``downgrade`` narrows them again and so refuses, loudly, while any text project or source
exists (delete them first) rather than silently rewriting them.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE projects DROP CONSTRAINT ck_projects_source_type;")
    op.execute(
        """
        ALTER TABLE projects
            ADD CONSTRAINT ck_projects_source_type
                CHECK (source_type IN ('pdf', 'topic', 'pptx', 'text'));
        """
    )

    op.execute("ALTER TABLE uploaded_sources DROP CONSTRAINT ck_uploaded_sources_kind;")
    op.execute(
        """
        ALTER TABLE uploaded_sources
            ADD CONSTRAINT ck_uploaded_sources_kind CHECK (kind IN ('pdf', 'pptx', 'text'));
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE uploaded_sources DROP CONSTRAINT IF EXISTS ck_uploaded_sources_kind;")
    op.execute(
        """
        ALTER TABLE uploaded_sources
            ADD CONSTRAINT ck_uploaded_sources_kind CHECK (kind IN ('pdf', 'pptx'));
        """
    )
    op.execute("ALTER TABLE projects DROP CONSTRAINT IF EXISTS ck_projects_source_type;")
    op.execute(
        """
        ALTER TABLE projects
            ADD CONSTRAINT ck_projects_source_type
                CHECK (source_type IN ('pdf', 'topic', 'pptx'));
        """
    )
