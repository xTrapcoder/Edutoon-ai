"""Free-text instructions on a project, and how a PDF's text was read

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-17

Two additive changes, nothing existing is rewritten:

``projects.instructions`` - what the owner asked of the lesson in their own words ("make it suitable
for a beginner", "focus on chapter 2"). It is optional, at most 2,000 characters, and is only ever
*guidance*: the topic and script writers are shown it, and it can never relax a grounding rule
(``domain.instructions``).

``uploaded_sources`` gains the facts about how a PDF was read, so the application can say so
instead of guessing:

- ``extraction_method`` - ``text`` (the PDF's own text layer, every row so far) or ``ocr`` (at least
  one page was read from its image by local text recognition).
- ``ocr_page_count`` - how many pages were read by OCR (``NULL`` when none was tried).
- ``failure_code`` - why a ``failed`` source failed, one of a fixed vocabulary. Before this the
  reason was *derived* from the page count; older failed rows keep ``NULL`` and keep that
  derivation, so nothing already stored changes meaning.

Enumerations are text columns with CHECK constraints (rule 7).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE projects
            ADD COLUMN instructions text,
            ADD CONSTRAINT ck_projects_instructions_length
                CHECK (instructions IS NULL OR (length(btrim(instructions)) > 0
                                                AND length(instructions) <= 2000));
        """
    )
    op.execute(
        """
        ALTER TABLE uploaded_sources
            ADD COLUMN extraction_method text NOT NULL DEFAULT 'text',
            ADD COLUMN ocr_page_count integer,
            ADD COLUMN failure_code text,
            ADD CONSTRAINT ck_uploaded_sources_extraction_method
                CHECK (extraction_method IN ('text', 'ocr')),
            ADD CONSTRAINT ck_uploaded_sources_ocr_page_count_non_negative
                CHECK (ocr_page_count IS NULL OR ocr_page_count >= 0),
            ADD CONSTRAINT ck_uploaded_sources_failure_code
                CHECK (failure_code IS NULL OR failure_code IN (
                    'unreadable', 'too_many_pages', 'no_text',
                    'ocr_unavailable', 'ocr_too_many_pages', 'ocr_no_text', 'ocr_failed'
                ));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE uploaded_sources
            DROP CONSTRAINT IF EXISTS ck_uploaded_sources_failure_code,
            DROP CONSTRAINT IF EXISTS ck_uploaded_sources_ocr_page_count_non_negative,
            DROP CONSTRAINT IF EXISTS ck_uploaded_sources_extraction_method,
            DROP COLUMN IF EXISTS failure_code,
            DROP COLUMN IF EXISTS ocr_page_count,
            DROP COLUMN IF EXISTS extraction_method;
        """
    )
    op.execute(
        """
        ALTER TABLE projects
            DROP CONSTRAINT IF EXISTS ck_projects_instructions_length,
            DROP COLUMN IF EXISTS instructions;
        """
    )
