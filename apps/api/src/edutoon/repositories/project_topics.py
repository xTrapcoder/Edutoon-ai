from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import CursorResult, RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import raise_conflict_from_integrity_error
from edutoon.core.pagination import Page, paginate_rows
from edutoon.db.tables import project_topics

# The ``status`` a topic starts in. Mirrors the column's DB default; stated
# here so bulk inserts (which must give every row the same keys) stay
# explicit rather than relying on it.
DEFAULT_STATUS = "proposed"

# A topic typed in by the user. Also the origin of a topic created without
# saying otherwise - a topic made by hand is a user topic (migration 0009's
# column default).
USER_ORIGIN = "user"
DEFAULT_ORIGIN = USER_ORIGIN


@dataclass(frozen=True, slots=True)
class ProjectTopicRecord:
    id: UUID
    project_id: UUID
    parent_id: UUID | None
    title: str
    summary: str | None
    position: int
    depth: int
    status: str
    origin: str
    keywords: list[str]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class NewProjectTopic:
    """One row for :func:`create_many`. ``parent_id`` must already exist, so
    a tree is inserted one depth level at a time (shallowest first).
    """

    project_id: UUID
    title: str
    parent_id: UUID | None = None
    summary: str | None = None
    position: int = 0
    depth: int = 0
    origin: str = DEFAULT_ORIGIN
    status: str = DEFAULT_STATUS
    keywords: tuple[str, ...] = ()


def _from_row(row: RowMapping) -> ProjectTopicRecord:
    return ProjectTopicRecord(
        id=row["id"],
        project_id=row["project_id"],
        parent_id=row["parent_id"],
        title=row["title"],
        summary=row["summary"],
        position=row["position"],
        depth=row["depth"],
        status=row["status"],
        origin=row["origin"],
        keywords=list(row["keywords"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def create(
    session: AsyncSession,
    *,
    project_id: UUID,
    title: str,
    parent_id: UUID | None = None,
    summary: str | None = None,
    position: int = 0,
    depth: int = 0,
    origin: str = DEFAULT_ORIGIN,
    status: str = DEFAULT_STATUS,
    keywords: Sequence[str] = (),
) -> ProjectTopicRecord:
    stmt = (
        project_topics.insert()
        .values(
            project_id=project_id,
            title=title,
            parent_id=parent_id,
            summary=summary,
            position=position,
            depth=depth,
            origin=origin,
            status=status,
            keywords=list(keywords),
        )
        .returning(*project_topics.c)
    )
    try:
        result = await session.execute(stmt)
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)
    return _from_row(result.mappings().one())


async def create_many(
    session: AsyncSession, topics: Sequence[NewProjectTopic]
) -> list[ProjectTopicRecord]:
    """Bulk-insert topics in a single round trip.

    ``RETURNING`` order is not guaranteed for a multi-row insert, so callers
    must map results back to their input by ``(parent_id, position)`` - the
    pair the ``uq_project_topics_project_id_parent_id_position`` index makes
    unique - not by list index. The result is sorted ``(depth, position, id)``
    only so it is deterministic.
    """
    if not topics:
        return []
    stmt = (
        project_topics.insert()
        .values(
            [
                {
                    "project_id": topic.project_id,
                    "title": topic.title,
                    "parent_id": topic.parent_id,
                    "summary": topic.summary,
                    "position": topic.position,
                    "depth": topic.depth,
                    "origin": topic.origin,
                    "status": topic.status,
                    "keywords": list(topic.keywords),
                }
                for topic in topics
            ]
        )
        .returning(*project_topics.c)
    )
    try:
        result = await session.execute(stmt)
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)
    records = [_from_row(row) for row in result.mappings().all()]
    return sorted(records, key=lambda record: (record.depth, record.position, str(record.id)))


async def get_by_id(session: AsyncSession, topic_id: UUID) -> ProjectTopicRecord | None:
    stmt = select(project_topics).where(project_topics.c.id == topic_id)
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def get_by_id_for_project(
    session: AsyncSession, topic_id: UUID, project_id: UUID
) -> ProjectTopicRecord | None:
    """Project-scoped fetch (rule 9): a topic that exists but belongs to a
    different project looks identical to one that doesn't exist.
    """
    stmt = select(project_topics).where(
        project_topics.c.id == topic_id, project_topics.c.project_id == project_id
    )
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def list_by_project(
    session: AsyncSession, project_id: UUID, *, limit: int = 50, cursor: str | None = None
) -> Page[ProjectTopicRecord]:
    stmt = select(project_topics).where(project_topics.c.project_id == project_id)
    rows, next_cursor = await paginate_rows(
        session, stmt, table=project_topics, limit=limit, cursor=cursor
    )
    return Page(items=[_from_row(row) for row in rows], next_cursor=next_cursor)


async def list_all_by_project(
    session: AsyncSession, project_id: UUID
) -> list[ProjectTopicRecord]:
    """Every topic of a project, shallowest first, then by ``position``.

    Unlike :func:`list_by_project` this is deliberately not keyset-paginated
    by ``(created_at, id)``: that order suits "newest first" lists, not a
    topic tree, and a bulk insert gives a whole tree the same ``created_at``.
    A project's topic count is bounded by ``MAX_TOPICS`` (a few dozen), so
    returning them all is safe - the same reasoning as
    ``source_chunks.list_by_source_in_order``. Arranging them into reading
    order is domain logic, not a query concern.
    """
    stmt = (
        select(project_topics)
        .where(project_topics.c.project_id == project_id)
        .order_by(
            project_topics.c.depth.asc(),
            project_topics.c.position.asc(),
            project_topics.c.id.asc(),
        )
    )
    rows = (await session.execute(stmt)).mappings().all()
    return [_from_row(row) for row in rows]


async def next_position(
    session: AsyncSession, *, project_id: UUID, parent_id: UUID | None
) -> int:
    """The first free ``position`` among ``parent_id``'s children (``0`` when
    it has none). ``parent_id=None`` means the project's root topics.
    """
    siblings = (
        project_topics.c.parent_id.is_(None)
        if parent_id is None
        else project_topics.c.parent_id == parent_id
    )
    stmt = select(func.coalesce(func.max(project_topics.c.position) + 1, 0)).where(
        project_topics.c.project_id == project_id, siblings
    )
    return int((await session.execute(stmt)).scalar_one())


async def update_for_project(
    session: AsyncSession, *, topic_id: UUID, project_id: UUID, **fields: Any
) -> ProjectTopicRecord | None:
    """Project-scoped partial update. Returns ``None`` if the topic does not
    exist or belongs to another project - the same rule-9 shape as
    :func:`get_by_id_for_project`. An empty ``fields`` is a no-op read.
    """
    if not fields:
        return await get_by_id_for_project(session, topic_id, project_id)

    stmt = (
        project_topics.update()
        .where(project_topics.c.id == topic_id, project_topics.c.project_id == project_id)
        .values(**fields)
        .returning(*project_topics.c)
    )
    try:
        result = await session.execute(stmt)
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)
    row = result.mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def delete_derived_by_project(session: AsyncSession, project_id: UUID) -> int:
    """Delete every topic of ``project_id`` that was *not* typed in by the
    user (``origin != 'user'``), and - via the ``parent_id`` and
    ``topic_citations`` cascades - their descendants and evidence. Returns the
    number of rows deleted directly.

    Used when an extraction is re-run with ``replace``. User topics are
    always created as roots, so the parent cascade can never reach one.
    ``project_topics`` is not an append-only table (only ``audit_logs`` and
    the ledger-style tables are), so a real DELETE is correct here.
    """
    stmt = project_topics.delete().where(
        project_topics.c.project_id == project_id, project_topics.c.origin != USER_ORIGIN
    )
    result = cast(CursorResult[Any], await session.execute(stmt))
    return result.rowcount
