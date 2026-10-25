"""project_topics origin + keywords, and topic_citations

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-23

Schema for the Phase 5 content-understanding foundation. Purely additive:
no existing column, constraint or index is altered, so everything written
before this migration keeps its meaning.

``project_topics.origin`` records where a topic came from:

  * ``source``    - extracted from the project's uploaded material; it must
                    carry at least one ``topic_citations`` row (enforced by
                    the service layer, not here - a DB CHECK cannot see
                    another table).
  * ``user``      - typed in by the user (a keyword or topic). May or may not
                    gain citations if the material happens to cover it.
  * ``suggested`` - proposed by the model with no source material behind it.
                    Never carries citations and is never verified.

It defaults to ``user`` so a topic created by hand, exactly as before this
migration, is a user topic. As with every enumeration in this schema it is
``text`` plus a ``CHECK``, never a Postgres enum type.

``project_topics.keywords`` holds a topic's key terms / concepts as a flat
``text[]``. ``NULL`` elements are rejected so readers never have to guard
against them.

``topic_citations`` links a topic to the ``source_chunks`` rows it was drawn
from (page range = the chunk's ``page_from`` / ``page_to``). Unlike
``claim_citations`` it cascades from ``source_chunks`` rather than
restricting: topic evidence is derived and regenerable, so it must never
block the removal of the material it points at - a topic simply loses its
evidence, which is visible as zero rows.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE project_topics ADD COLUMN origin text NOT NULL DEFAULT 'user';")
    op.execute(
        """
        ALTER TABLE project_topics
            ADD CONSTRAINT ck_project_topics_origin
                CHECK (origin IN ('source', 'user', 'suggested'));
        """
    )

    op.execute("ALTER TABLE project_topics ADD COLUMN keywords text[] NOT NULL DEFAULT '{}';")
    op.execute(
        """
        ALTER TABLE project_topics
            ADD CONSTRAINT ck_project_topics_keywords_no_null_elements
                CHECK (array_position(keywords, NULL) IS NULL);
        """
    )

    op.execute(
        """
        CREATE TABLE topic_citations (
            id              uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            topic_id        uuid        NOT NULL,
            source_chunk_id uuid        NOT NULL,
            created_at      timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_topic_citations_topic_id_project_topics
                FOREIGN KEY (topic_id) REFERENCES project_topics (id) ON DELETE CASCADE,
            CONSTRAINT fk_topic_citations_source_chunk_id_source_chunks
                FOREIGN KEY (source_chunk_id) REFERENCES source_chunks (id) ON DELETE CASCADE
        );
        """
    )
    op.execute("CREATE INDEX ix_topic_citations_topic_id ON topic_citations (topic_id);")
    op.execute(
        "CREATE INDEX ix_topic_citations_source_chunk_id ON topic_citations (source_chunk_id);"
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_topic_citations_topic_id_source_chunk_id
            ON topic_citations (topic_id, source_chunk_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS topic_citations;")
    op.execute(
        "ALTER TABLE project_topics DROP CONSTRAINT IF EXISTS "
        "ck_project_topics_keywords_no_null_elements;"
    )
    op.execute("ALTER TABLE project_topics DROP COLUMN IF EXISTS keywords;")
    op.execute("ALTER TABLE project_topics DROP CONSTRAINT IF EXISTS ck_project_topics_origin;")
    op.execute("ALTER TABLE project_topics DROP COLUMN IF EXISTS origin;")
