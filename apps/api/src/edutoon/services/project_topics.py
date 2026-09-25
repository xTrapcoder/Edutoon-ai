"""Project topics - service layer.

Two generations live here. The first three functions
(``create_project_topic`` / ``get_project_topic_or_404`` / ``list_project_topics``)
are thin repository wrappers from before the API existed. The rest (Phase 5,
Step 6) back ``/v1/projects/{project_id}/topics``:

- ``list_topic_outline`` - the project's topics in reading order, each with the
  source pages it was drawn from (evidence is batch-loaded: two queries in
  total, however many topics there are).
- ``add_keyword_topics`` - the user's own keywords, stored as ``user`` topics
  that are already ``accepted``. No model call, no cost.
- ``update_topic`` - the review step: accept or reject a topic, and correct its
  wording.

Every path is project + owner scoped (rule 9: a project or topic the caller
does not own is a 404, never a 403), and every write that an in-flight
extraction could collide with first takes the project's extraction lock and
refuses while one is queued or running - see
``topic_extraction.refuse_if_extraction_active``. Everything the user types is
validated strictly and *rejected*, never silently trimmed: these are their own
words, so a problem is theirs to correct.

Architecture (CLAUDE.md rules 1, 2, 3): no SQL here, no provider import; the
limits come from settings and the rules from ``domain.topic_tree``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.config import get_settings
from edutoon.core.errors import ConflictError, NotFoundError, ValidationError
from edutoon.core.pagination import Page
from edutoon.domain.topic import ORIGIN_USER, STATUS_ACCEPTED, TOPIC_STATUSES
from edutoon.domain.topic_tree import (
    InvalidKeywordError,
    normalise_term,
    normalise_user_keywords,
    reading_order,
    term_key,
)
from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import topic_citations as topic_citations_repo
from edutoon.repositories.project_topics import NewProjectTopic, ProjectTopicRecord
from edutoon.repositories.topic_citations import TopicEvidenceRecord
from edutoon.services import audit_logs as audit_logs_service
from edutoon.services import projects as projects_service
from edutoon.services import topic_extraction as topic_extraction_service

AUDIT_ENTITY_TYPE = "project_topic"
AUDIT_ACTION_KEYWORDS_ADDED = "topics.keywords_added"
AUDIT_ACTION_UPDATED = "topics.updated"

# The fields ``update_topic`` accepts. ``status`` is the review decision and is
# safe to change at any time; ``title`` and ``summary`` are the topic's wording,
# which an extraction in flight may be about to fill in or replace.
_WORDING_FIELDS = frozenset({"title", "summary"})
_EDITABLE_FIELDS = frozenset({"status"}) | _WORDING_FIELDS


@dataclass(frozen=True, slots=True)
class TopicWithEvidence:
    """A topic and the source pages it cites (empty for a topic the material
    does not cover, and for anything the user typed that was never matched).
    """

    topic: ProjectTopicRecord
    evidence: tuple[TopicEvidenceRecord, ...] = ()


# --- pre-API wrappers (unchanged) ---------------------------------------------------


async def create_project_topic(
    session: AsyncSession,
    *,
    project_id: UUID,
    title: str,
    parent_id: UUID | None = None,
    summary: str | None = None,
    position: int = 0,
    depth: int = 0,
) -> ProjectTopicRecord:
    return await project_topics_repo.create(
        session,
        project_id=project_id,
        title=title,
        parent_id=parent_id,
        summary=summary,
        position=position,
        depth=depth,
    )


async def get_project_topic_or_404(session: AsyncSession, topic_id: UUID) -> ProjectTopicRecord:
    topic = await project_topics_repo.get_by_id(session, topic_id)
    if topic is None:
        raise NotFoundError(f"Project topic {topic_id} not found.")
    return topic


async def list_project_topics(
    session: AsyncSession, project_id: UUID, *, limit: int = 50, cursor: str | None = None
) -> Page[ProjectTopicRecord]:
    return await project_topics_repo.list_by_project(
        session, project_id, limit=limit, cursor=cursor
    )


# --- read ------------------------------------------------------------------------------


async def list_topic_outline(
    session: AsyncSession, *, project_id: UUID, owner_id: UUID
) -> list[TopicWithEvidence]:
    """Every topic of the project in reading order (depth-first, siblings by
    position), each with its evidence. Not paginated: a project's topic count is
    bounded by the extraction limits, and a tree is meaningless in pages.
    """
    await projects_service.get_project(session, project_id=project_id, owner_id=owner_id)

    ordered = reading_order(await project_topics_repo.list_all_by_project(session, project_id))
    evidence = await topic_citations_repo.list_evidence_for_topics(
        session, project_id=project_id, topic_ids=[topic.id for topic in ordered]
    )
    return [
        TopicWithEvidence(topic=topic, evidence=tuple(evidence.get(topic.id, ())))
        for topic in ordered
    ]


# --- keywords ------------------------------------------------------------------------------


async def add_keyword_topics(
    session: AsyncSession,
    *,
    project_id: UUID,
    owner_id: UUID,
    keywords: Sequence[str],
) -> list[TopicWithEvidence]:
    """Store ``keywords`` as the project's own topics: ``origin='user'``,
    already ``accepted`` (the user chose them), appended after the existing
    roots in the order given. Returns the created topics.

    All or nothing. Raises :class:`~edutoon.core.errors.ValidationError` (400)
    for a blank or over-long keyword, or if the project would end up with more
    topics of its own than one extraction can carry
    (``MAX_TOPICS_PER_EXTRACTION`` - beyond that every extraction would fail);
    :class:`~edutoon.core.errors.ConflictError` (409) for a keyword the project
    already has (compared ignoring case and spacing) or while an extraction is
    queued or running - it would not be included in that run and could take the
    position the run is about to use.
    """
    await projects_service.get_project(session, project_id=project_id, owner_id=owner_id)
    settings = get_settings()

    try:
        cleaned = normalise_user_keywords(
            keywords,
            max_keywords=settings.MAX_TOPICS_PER_EXTRACTION,
            max_length=settings.MAX_TOPIC_TITLE_CHARS,
        )
    except InvalidKeywordError as exc:
        raise ValidationError(str(exc)) from exc
    if not cleaned:
        raise ValidationError("Give at least one keyword.")

    await topic_extraction_service.refuse_if_extraction_active(session, project_id=project_id)

    topics = await project_topics_repo.list_all_by_project(session, project_id)
    own = [topic for topic in topics if topic.origin == ORIGIN_USER]

    taken = {term_key(topic.title) for topic in own}
    repeated = [keyword for keyword in cleaned if term_key(keyword) in taken]
    if repeated:
        shown = ", ".join(repr(keyword) for keyword in repeated[:5])
        raise ConflictError(f"This project already has a topic called {shown}.")

    if len(own) + len(cleaned) > settings.MAX_TOPICS_PER_EXTRACTION:
        raise ValidationError(
            f"A project can hold at most {settings.MAX_TOPICS_PER_EXTRACTION} topics of "
            f"its own; it has {len(own)} and {len(cleaned)} more were given."
        )

    first = await project_topics_repo.next_position(session, project_id=project_id, parent_id=None)
    created = await project_topics_repo.create_many(
        session,
        [
            NewProjectTopic(
                project_id=project_id,
                title=keyword,
                position=first + offset,
                origin=ORIGIN_USER,
                status=STATUS_ACCEPTED,
            )
            for offset, keyword in enumerate(cleaned)
        ],
    )
    await audit_logs_service.record_audit_log(
        session,
        action=AUDIT_ACTION_KEYWORDS_ADDED,
        entity_type=AUDIT_ENTITY_TYPE,
        project_id=project_id,
        actor_id=owner_id,
        context={"topic_ids": [str(topic.id) for topic in created], "count": len(created)},
    )
    return [TopicWithEvidence(topic=topic) for topic in created]


# --- review ----------------------------------------------------------------------------------


async def update_topic(
    session: AsyncSession,
    *,
    project_id: UUID,
    owner_id: UUID,
    topic_id: UUID,
    fields: Mapping[str, Any],
) -> TopicWithEvidence:
    """Apply a partial update to one topic; only the keys present in ``fields``
    change (``summary`` may be ``None`` to clear it).

    - ``status`` - the review decision; any status may move to any other.
    - ``title`` / ``summary`` - the wording. Validated strictly and never
      trimmed to fit; a title may not repeat another of the user's own topics.
      Refused with a 409 while an extraction is queued or running, since it
      could otherwise overwrite the edit.

    Changing nothing (every value already what is stored) is a successful no-op:
    no write and no audit entry. A project or topic the caller does not own is a
    404.
    """
    unknown = set(fields) - _EDITABLE_FIELDS
    if unknown:
        raise ValidationError(f"Cannot update: {', '.join(sorted(unknown))}.")

    await projects_service.get_project(session, project_id=project_id, owner_id=owner_id)
    if _WORDING_FIELDS & set(fields):
        await topic_extraction_service.refuse_if_extraction_active(session, project_id=project_id)

    current = await project_topics_repo.get_by_id_for_project(session, topic_id, project_id)
    if current is None:
        raise NotFoundError(f"Project topic {topic_id} not found.")

    changes = await _validated_changes(session, current, fields)
    changes = {name: value for name, value in changes.items() if getattr(current, name) != value}
    if not changes:
        return await _with_evidence(session, current)

    updated = await project_topics_repo.update_for_project(
        session, topic_id=topic_id, project_id=project_id, **changes
    )
    if updated is None:  # deleted between the read and the write
        raise NotFoundError(f"Project topic {topic_id} not found.")

    context: dict[str, Any] = {"fields": sorted(changes)}
    if "status" in changes:
        context["status_from"] = current.status
        context["status_to"] = updated.status
    await audit_logs_service.record_audit_log(
        session,
        action=AUDIT_ACTION_UPDATED,
        entity_type=AUDIT_ENTITY_TYPE,
        entity_id=topic_id,
        project_id=project_id,
        actor_id=owner_id,
        context=context,
    )
    return await _with_evidence(session, updated)


async def _validated_changes(
    session: AsyncSession, current: ProjectTopicRecord, fields: Mapping[str, Any]
) -> dict[str, Any]:
    settings = get_settings()
    changes: dict[str, Any] = {}

    # The API schema rejects these shapes already; the service does not lean on
    # that, because the alternative is a ``NOT NULL`` / CHECK violation reaching
    # the caller as an unhelpful "conflict".
    if "status" in fields:
        if fields["status"] not in TOPIC_STATUSES:
            raise ValidationError(f"status must be one of: {', '.join(sorted(TOPIC_STATUSES))}.")
        changes["status"] = fields["status"]

    if "title" in fields:
        if not isinstance(fields["title"], str):
            raise ValidationError("A title must be text; it cannot be cleared.")
        title = normalise_term(fields["title"])
        if not title:
            raise ValidationError("A title must not be blank.")
        if len(title) > settings.MAX_TOPIC_TITLE_CHARS:
            raise ValidationError(
                f"The title is {len(title)} characters; the limit is "
                f"{settings.MAX_TOPIC_TITLE_CHARS}."
            )
        if current.origin == ORIGIN_USER and title != current.title:
            siblings = await project_topics_repo.list_all_by_project(session, current.project_id)
            if any(
                other.origin == ORIGIN_USER
                and other.id != current.id
                and term_key(other.title) == term_key(title)
                for other in siblings
            ):
                raise ConflictError(f"This project already has a topic called {title!r}.")
        changes["title"] = title

    if "summary" in fields:
        raw = fields["summary"]
        if raw is not None and not isinstance(raw, str):
            raise ValidationError("A summary must be text, or null to clear it.")
        summary = None if raw is None else raw.strip() or None
        if summary is not None and len(summary) > settings.MAX_TOPIC_SUMMARY_CHARS:
            raise ValidationError(
                f"The summary is {len(summary)} characters; the limit is "
                f"{settings.MAX_TOPIC_SUMMARY_CHARS}."
            )
        changes["summary"] = summary

    return changes


async def _with_evidence(session: AsyncSession, topic: ProjectTopicRecord) -> TopicWithEvidence:
    evidence = await topic_citations_repo.list_evidence_for_topics(
        session, project_id=topic.project_id, topic_ids=[topic.id]
    )
    return TopicWithEvidence(topic=topic, evidence=tuple(evidence.get(topic.id, ())))
