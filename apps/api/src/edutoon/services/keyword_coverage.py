"""Which of a project's keywords its document covers (``domain.keyword_coverage``).

Read-only: it reads the keyword topics the owner typed, the claim counts the claim pipeline
produced for them and the topics the project's current script teaches, and says, for each keyword,
whether the document backs it. It never starts a job and never changes a topic.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.domain import keyword_coverage as domain
from edutoon.domain.topic import ORIGIN_USER
from edutoon.repositories import claim_verifications as claim_verifications_repo
from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.services import projects as projects_service


@dataclass(frozen=True, slots=True)
class KeywordCoverageReport:
    keywords: list[domain.KeywordCoverage]
    complete: bool
    summary: str | None


async def get_keyword_coverage(
    session: AsyncSession, *, project_id: UUID, owner_id: UUID
) -> KeywordCoverageReport:
    """The coverage of every keyword the owner typed. A project the caller does not own is a 404
    (rule 9). A project with no typed keywords has an empty, complete report."""
    await projects_service.get_project(session, project_id=project_id, owner_id=owner_id)

    topics = await project_topics_repo.list_all_by_project(session, project_id)
    typed = [
        domain.TopicFacts(topic_id=topic.id, title=topic.title, status=topic.status)
        for topic in topics
        if topic.origin == ORIGIN_USER
    ]
    if not typed:
        return KeywordCoverageReport(keywords=[], complete=True, summary=None)

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
