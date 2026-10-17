"""Whether a project's lesson rests on the model's general knowledge rather than on a document.

``domain.general_knowledge`` holds the rule (a keyword-only project with no indexed source
material); this is the one place that reads the two facts it needs - the project's source type and
how much material it has - so topic, claim and script stages all answer the question the same way.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.config import get_settings
from edutoon.domain import general_knowledge as domain
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories.projects import ProjectRecord


async def applies_to(session: AsyncSession, project: ProjectRecord) -> bool:
    """``True`` when the project is built from keywords alone and has no source material, and
    keyword-only projects are switched on (``KEYWORD_ONLY_PROJECTS_ENABLED``). A project that has
    material - a PDF, pasted text, a deck, or a keyword project that later gained a PDF - verifies
    every claim against it and never takes this path."""
    if not get_settings().KEYWORD_ONLY_PROJECTS_ENABLED:
        return False
    if project.source_type != domain.SOURCE_TYPE:
        return False
    chunk_count = await source_chunks_repo.count_by_project(session, project.id)
    return domain.is_general_knowledge_project(
        source_type=project.source_type, indexed_chunk_count=chunk_count
    )
