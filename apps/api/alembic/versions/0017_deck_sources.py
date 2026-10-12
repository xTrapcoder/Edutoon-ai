"""PowerPoint decks as a source: slide structure on chunks, and the ``pptx`` source kind

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-07

Lets a project's source be a ``.pptx`` deck. Purely additive: nothing written before this
migration changes meaning, and the PDF pipeline reads exactly what it did.

``source_chunks.structure`` (nullable ``jsonb``) holds a deck slide's structure - its title,
bullets with their level, numbered steps, tables and speaker notes - beside the flat ``content``
the chunk has always had (``domain.deck``). A deck lesson is written from the structure; the flat
text is what a citation shows and what a claim is checked against. It is ``NULL`` for every PDF
chunk, and must be a JSON object when present.

``ck_projects_source_type`` and ``ck_uploaded_sources_kind`` are widened to allow ``pptx``.
Every existing row is ``pdf`` or ``topic``, so widening cannot fail against current data;
``downgrade`` narrows them again and so refuses, loudly, while any deck project or source exists
(delete them first) rather than silently rewriting them.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE source_chunks ADD COLUMN structure jsonb;")
    op.execute(
        """
        ALTER TABLE source_chunks
            ADD CONSTRAINT ck_source_chunks_structure_object
                CHECK (structure IS NULL OR jsonb_typeof(structure) = 'object');
        """
    )

    op.execute("ALTER TABLE projects DROP CONSTRAINT ck_projects_source_type;")
    op.execute(
        """
        ALTER TABLE projects
            ADD CONSTRAINT ck_projects_source_type
                CHECK (source_type IN ('pdf', 'topic', 'pptx'));
        """
    )

    op.execute("ALTER TABLE uploaded_sources DROP CONSTRAINT ck_uploaded_sources_kind;")
    op.execute(
        """
        ALTER TABLE uploaded_sources
            ADD CONSTRAINT ck_uploaded_sources_kind CHECK (kind IN ('pdf', 'pptx'));
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE uploaded_sources DROP CONSTRAINT IF EXISTS ck_uploaded_sources_kind;")
    op.execute(
        "ALTER TABLE uploaded_sources ADD CONSTRAINT ck_uploaded_sources_kind CHECK (kind = 'pdf');"
    )
    op.execute("ALTER TABLE projects DROP CONSTRAINT IF EXISTS ck_projects_source_type;")
    op.execute(
        """
        ALTER TABLE projects
            ADD CONSTRAINT ck_projects_source_type CHECK (source_type IN ('pdf', 'topic'));
        """
    )
    op.execute(
        "ALTER TABLE source_chunks DROP CONSTRAINT IF EXISTS ck_source_chunks_structure_object;"
    )
    op.execute("ALTER TABLE source_chunks DROP COLUMN IF EXISTS structure;")
