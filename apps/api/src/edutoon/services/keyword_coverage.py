"""Which of a project's keywords its document covers (``domain.keyword_coverage``).

Read-only: it reads the keyword topics the owner typed, the claim counts the claim pipeline
produced for them and the topics the project's current script teaches, and says, for each keyword,
whether the document backs it. It never starts a job and never changes a topic.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.domain import keyword_coverage as domain
from edutoon.domain.topic import ORIGIN_USER
from edutoon.repositories import claim_verifications as claim_verifications_repo
from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories.project_topics import ProjectTopicRecord
from edutoon.services import projects as projects_service


@dataclass(frozen=True, slots=True)
class KeywordCoverageReport:
    keywords: list[domain.KeywordCoverage]
    complete: bool
    summary: str | None


async def uncovered_keyword_terms(
    session: AsyncSession, *, project_id: UUID, topics: Sequence[ProjectTopicRecord]
) -> dict[UUID, tuple[str, ...]]:
    """For each keyword topic the owner typed, the words of it that the project's document never
    contains (``domain.keyword_coverage.missing_terms``). A topic whose words are all in the
    document is absent from the result, and so is every topic when the project has no document
    (a topic-only lesson has nothing to look in: its statements are labelled general knowledge)."""
    typed = [topic for topic in topics if topic.origin == ORIGIN_USER]
    if not typed:
        return {}
    chunks = await source_chunks_repo.list_by_project_in_order(session, project_id)
    if not chunks:
        return {}
    texts = [chunk.content for chunk in chunks]
    missing = {topic.id: domain.missing_terms(topic.title, texts) for topic in typed}
    return {topic_id: terms for topic_id, terms in missing.items() if terms}


async def get_keyword_coverage(
    session: AsyncSession, *, project_id: UUID, owner_id: UUID
) -> KeywordCoverageReport:
    """The coverage of every keyword the owner typed. A project the caller does not own is a 404
    (rule 9). A project with no typed keywords has an empty, complete report."""
    await projects_service.get_project(session, project_id=project_id, owner_id=owner_id)

    topics = await project_topics_repo.list_all_by_project(session, project_id)
    if not any(topic.origin == ORIGIN_USER for topic in topics):
        return KeywordCoverageReport(keywords=[], complete=True, summary=None)
    missing = await uncovered_keyword_terms(session, project_id=project_id, topics=topics)
    typed = [
        domain.TopicFacts(
            topic_id=topic.id,
            title=topic.title,
            status=topic.status,
            missing_terms=missing.get(topic.id, ()),
        )
        for topic in topics
        if topic.origin == ORIGIN_USER
    ]

    counts = await claim_verifications_repo.count_by_topic(session, project_id=project_id)
    script = await scripts_repo.get_active(session, project_id)
    scripted: set[UUID] | None = None
    if script is not None and script.status in ("draft", "approved"):
        segments = await scripts_repo.list_segments(
            session, project_id=project_id, script_id=script.id
        )
        scripted = {segment.topic_id for segment in segments if segment.topic_id is not None}

    items = domain.assess(
        typed,
        {
            topic_id: domain.ClaimCounts(supported=supported, total=total)
            for topic_id, (supported, total) in counts.items()
        },
        scripted_topic_ids=scripted,
    )
    return KeywordCoverageReport(
        keywords=items, complete=domain.is_complete(items), summary=domain.summary(items)
    )
