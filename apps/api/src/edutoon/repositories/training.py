"""Training courses and modules (migration 0020): the SQL, nothing else.

No rule lives here - which transitions are allowed, who may do what and what happens to the files
is ``services.training``. This module only reads and writes the rows, and keeps the one invariant
that is cheaper to hold in SQL than in Python: a course's module positions are contiguous.

**Locking.** Every operation that depends on the set of a course's modules (appending one, closing
a gap, reordering) first takes ``SELECT ... FOR UPDATE`` on the *course* row (:func:`get_course`
with ``for_update=True``), always course before module, so two concurrent writers to one course
queue instead of computing the same next position.

**Positions.** ``uq_training_modules_course_id_position`` is ``DEFERRABLE INITIALLY DEFERRED``, so a
single statement may pass through duplicate positions (a swap) as long as the final state is
unique. :func:`apply_order` relies on exactly that: one ``UPDATE`` assigns every module its new
position, with no negative placeholders and no delete-and-reinsert.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, bindparam, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import raise_conflict_from_integrity_error
from edutoon.core.pagination import Page, paginate_rows
from edutoon.db.tables import course_enrolments, training_courses, training_modules, users
from edutoon.domain.training import (
    COURSE_PUBLISHED,
    ENROLMENT_ACTIVE,
    MODULE_PUBLISHED,
    MODULE_UNPUBLISHED,
)

POSITION_CONSTRAINT = "uq_training_modules_course_id_position"
ENROLMENT_CONSTRAINT = "uq_course_enrolments_course_id_user_id"
FOREIGN_KEY_VIOLATION = "23503"


@dataclass(frozen=True, slots=True)
class CourseRecord:
    id: UUID
    title: str
    description: str | None
    status: str
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    module_count: int
    published_module_count: int


@dataclass(frozen=True, slots=True)
class ModuleRecord:
    id: UUID
    course_id: UUID
    position: int
    title: str
    description: str | None
    status: str
    source_render_output_id: UUID | None
    video_bucket: str | None
    video_key: str | None
    video_byte_size: int
    video_duration_ms: int
    video_width: int
    video_height: int
    video_fps: int
    video_output_hash: str
    published_by: UUID
    published_at: datetime
    unpublished_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class VideoSnapshot:
    """What a publication pins: where the module's own copy lives and what it is."""

    bucket: str
    key: str
    byte_size: int
    duration_ms: int
    width: int
    height: int
    fps: int
    output_hash: str
    source_render_output_id: UUID | None


def _module_from_row(row: RowMapping) -> ModuleRecord:
    return ModuleRecord(**{name: row[name] for name in ModuleRecord.__slots__})


def _course_from_row(row: RowMapping) -> CourseRecord:
    return CourseRecord(**{name: row[name] for name in CourseRecord.__slots__})


def is_foreign_key_violation(exc: IntegrityError) -> bool:
    """Whether ``exc`` is a foreign-key violation (SQLSTATE 23503), however the driver wraps it."""
    return any(
        getattr(candidate, "sqlstate", None) == FOREIGN_KEY_VIOLATION
        for candidate in (getattr(exc.orig, "__cause__", None), exc.orig)
    )


# --- courses -----------------------------------------------------------------------------


def _course_select() -> Select[Any]:
    modules = training_modules
    total = (
        select(func.count())
        .select_from(modules)
        .where(modules.c.course_id == training_courses.c.id)
        .scalar_subquery()
    )
    published = (
        select(func.count())
        .select_from(modules)
        .where(modules.c.course_id == training_courses.c.id, modules.c.status == MODULE_PUBLISHED)
        .scalar_subquery()
    )
    return select(
        training_courses,
        total.label("module_count"),
        published.label("published_module_count"),
    )


async def create_course(
    session: AsyncSession, *, title: str, description: str | None, created_by: UUID
) -> CourseRecord:
    inserted = (
        await session.execute(
            training_courses.insert()
            .values(title=title, description=description, status="draft", created_by=created_by)
            .returning(training_courses.c.id)
        )
    ).scalar_one()
    course = await get_course(session, inserted)
    assert course is not None
    return course


async def get_course(
    session: AsyncSession,
    course_id: UUID,
    *,
    for_update: bool = False,
    for_share: bool = False,
) -> CourseRecord | None:
    """One course with its module counts. ``for_update`` locks the course row for the rest of
    the transaction (the lock that serialises changes to the course's module list).
    ``for_share`` takes the weaker shared lock instead: many holders at once, but it waits for -
    and holds off - anyone changing or deleting the course (what enrolling a user needs)."""
    assert not (for_update and for_share), "ask for one lock, not two"
    if for_update or for_share:
        lock = select(training_courses.c.id).where(training_courses.c.id == course_id)
        locked = (
            await session.execute(
                lock.with_for_update() if for_update else lock.with_for_update(read=True)
            )
        ).scalar_one_or_none()
        if locked is None:
            return None
    stmt = _course_select().where(training_courses.c.id == course_id)
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _course_from_row(row) if row else None


async def list_courses(
    session: AsyncSession, *, status: str | None, limit: int, cursor: str | None
) -> Page[CourseRecord]:
    stmt = _course_select()
    if status is not None:
        stmt = stmt.where(training_courses.c.status == status)
    rows, next_cursor = await paginate_rows(
        session, stmt, table=training_courses, limit=limit, cursor=cursor
    )
    return Page(items=[_course_from_row(row) for row in rows], next_cursor=next_cursor)


async def update_course(session: AsyncSession, course_id: UUID, **fields: Any) -> None:
    """Set the given columns of a course (``title``, ``description`` or ``status``)."""
    allowed = {"title", "description", "status"}
    assert fields.keys() <= allowed, f"unexpected course columns {fields.keys() - allowed}"
    await session.execute(
        update(training_courses).where(training_courses.c.id == course_id).values(**fields)
    )


async def delete_course(session: AsyncSession, course_id: UUID) -> bool:
    """Delete the course (its modules and enrolments cascade). The caller has locked it."""
    result = await session.execute(
        delete(training_courses).where(training_courses.c.id == course_id)
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def count_enrolments(session: AsyncSession, course_id: UUID) -> int:
    """Enrolments of the course in any status - a withdrawn one is still history."""
    return (
        await session.execute(
            select(func.count())
            .select_from(course_enrolments)
            .where(course_enrolments.c.course_id == course_id)
        )
    ).scalar_one()


# --- modules -----------------------------------------------------------------------------


async def new_module_id(session: AsyncSession) -> UUID:
    """A fresh time-ordered id from the database's own ``uuid_generate_v7()``.

    The id has to be known *before* the row exists: the module's video key is built from it and
    ``ck_training_modules_video_key_is_module_owned`` requires the key to contain it.
    """
    value = (await session.execute(select(func.uuid_generate_v7()))).scalar_one()
    return value if isinstance(value, UUID) else UUID(str(value))


async def get_module(session: AsyncSession, module_id: UUID) -> ModuleRecord | None:
    row = (
        (await session.execute(select(training_modules).where(training_modules.c.id == module_id)))
        .mappings()
        .one_or_none()
    )
    return _module_from_row(row) if row else None


async def get_module_locked(session: AsyncSession, module_id: UUID) -> ModuleRecord | None:
    """The module row, locked. Callers lock its *course* first (lock order: course, module)."""
    row = (
        (
            await session.execute(
                select(training_modules).where(training_modules.c.id == module_id).with_for_update()
            )
        )
        .mappings()
        .one_or_none()
    )
    return _module_from_row(row) if row else None


async def list_modules(session: AsyncSession, course_id: UUID) -> list[ModuleRecord]:
    """Every module of the course in position order - published and unpublished alike."""
    stmt = (
        select(training_modules)
        .where(training_modules.c.course_id == course_id)
        .order_by(training_modules.c.position)
    )
    return [_module_from_row(row) for row in (await session.execute(stmt)).mappings().all()]


async def find_published_by_source(
    session: AsyncSession, *, course_id: UUID, source_render_output_id: UUID
) -> ModuleRecord | None:
    stmt = select(training_modules).where(
        training_modules.c.course_id == course_id,
        training_modules.c.source_render_output_id == source_render_output_id,
        training_modules.c.status == MODULE_PUBLISHED,
    )
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _module_from_row(row) if row else None


async def next_position(session: AsyncSession, course_id: UUID) -> int:
    value = (
        await session.execute(
            select(func.coalesce(func.max(training_modules.c.position), 0) + 1).where(
                training_modules.c.course_id == course_id
            )
        )
    ).scalar_one()
    return int(value)


async def insert_module(
    session: AsyncSession,
    *,
    module_id: UUID,
    course_id: UUID,
    position: int,
    title: str,
    description: str | None,
    snapshot: VideoSnapshot,
    published_by: UUID,
) -> ModuleRecord:
    """Insert a published module. A second published module for the same source render in the
    course is refused by ``uq_training_modules_course_source_render`` (-> ``ConflictError``)."""
    try:
        async with session.begin_nested():
            row = (
                (
                    await session.execute(
                        training_modules.insert()
                        .values(
                            id=module_id,
                            course_id=course_id,
                            position=position,
                            title=title,
                            description=description,
                            status=MODULE_PUBLISHED,
                            source_render_output_id=snapshot.source_render_output_id,
                            video_bucket=snapshot.bucket,
                            video_key=snapshot.key,
                            video_byte_size=snapshot.byte_size,
                            video_duration_ms=snapshot.duration_ms,
                            video_width=snapshot.width,
                            video_height=snapshot.height,
                            video_fps=snapshot.fps,
                            video_output_hash=snapshot.output_hash,
                            published_by=published_by,
                        )
                        .returning(*training_modules.c)
                    )
                )
                .mappings()
                .one()
            )
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)
    return _module_from_row(row)


async def replace_snapshot(
    session: AsyncSession, *, module_id: UUID, snapshot: VideoSnapshot, published_by: UUID
) -> ModuleRecord:
    """Point the module at a new snapshot and publish it (also how an unpublished module comes
    back). The previous object is the caller's to remove, after the commit."""
    try:
        async with session.begin_nested():
            row = (
                (
                    await session.execute(
                        update(training_modules)
                        .where(training_modules.c.id == module_id)
                        .values(
                            status=MODULE_PUBLISHED,
                            source_render_output_id=snapshot.source_render_output_id,
                            video_bucket=snapshot.bucket,
                            video_key=snapshot.key,
                            video_byte_size=snapshot.byte_size,
                            video_duration_ms=snapshot.duration_ms,
                            video_width=snapshot.width,
                            video_height=snapshot.height,
                            video_fps=snapshot.fps,
                            video_output_hash=snapshot.output_hash,
                            published_by=published_by,
                            published_at=func.now(),
                            unpublished_at=None,
                        )
                        .returning(*training_modules.c)
                    )
                )
                .mappings()
                .one()
            )
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)
    return _module_from_row(row)


async def mark_unpublished(session: AsyncSession, module_id: UUID) -> ModuleRecord:
    """Unpublish: the video location is cleared (the file is the caller's to remove after the
    commit); the descriptive snapshot, the position and every other row stay."""
    row = (
        (
            await session.execute(
                update(training_modules)
                .where(training_modules.c.id == module_id)
                .values(
                    status=MODULE_UNPUBLISHED,
                    video_bucket=None,
                    video_key=None,
                    unpublished_at=func.now(),
                )
                .returning(*training_modules.c)
            )
        )
        .mappings()
        .one()
    )
    return _module_from_row(row)


async def delete_module(session: AsyncSession, module_id: UUID) -> None:
    await session.execute(delete(training_modules).where(training_modules.c.id == module_id))


class PositionGapError(RuntimeError):
    """:func:`close_position_gap` was called while a module still sits at the position it was told
    had been vacated - a programming error in the caller, never a user error."""


async def close_position_gap(session: AsyncSession, *, course_id: UUID, after: int) -> None:
    """After the module at position ``after`` was **removed**, move every later module up one
    place, in one statement, so positions stay 1..n. Valid mid-statement because the unique
    constraint is deferred.

    Precondition (checked, not assumed): no module of the course is at ``after`` any more. With
    that true, ``position > after`` and ``position >= after`` select the same rows, which is why the
    strict comparison is correct; with it false, closing the gap would stack two modules on one
    position, so the call is refused with :class:`PositionGapError` before anything is changed.
    """
    occupied = (
        await session.execute(
            select(func.count())
            .select_from(training_modules)
            .where(training_modules.c.course_id == course_id, training_modules.c.position == after)
        )
    ).scalar_one()
    if occupied:
        raise PositionGapError(
            f"position {after} of course {course_id} is still occupied; "
            "remove the module before closing the gap it leaves"
        )
    await session.execute(
        update(training_modules)
        .where(and_(training_modules.c.course_id == course_id, training_modules.c.position > after))
        .values(position=training_modules.c.position - 1)
    )


async def apply_order(
    session: AsyncSession, *, course_id: UUID, module_ids: Sequence[UUID]
) -> None:
    """Give the listed modules positions 1..n in the order listed, in **one** ``UPDATE``.

    ``unnest(...) WITH ORDINALITY`` numbers the ids from 1, and the join assigns each module its
    ordinal. Several rows briefly share a position inside the statement (a swap does), which the
    deferrable unique constraint allows; ``SET CONSTRAINTS ... IMMEDIATE`` then makes the
    database check uniqueness *now*, inside this transaction, so a violation surfaces here as a
    clear error rather than at commit. The caller guarantees ``module_ids`` is every module of
    the course exactly once, which is what makes the result contiguous.
    """
    statement = text(
        """
        UPDATE training_modules AS m
           SET position = ordered.new_position::integer
          FROM unnest(:module_ids) WITH ORDINALITY AS ordered(id, new_position)
         WHERE m.id = ordered.id AND m.course_id = :course_id
        """
    ).bindparams(
        bindparam("module_ids", type_=ARRAY(PG_UUID(as_uuid=True))),
        bindparam("course_id", type_=PG_UUID(as_uuid=True)),
    )
    try:
        await session.execute(statement, {"module_ids": list(module_ids), "course_id": course_id})
        await session.execute(text(f"SET CONSTRAINTS {POSITION_CONSTRAINT} IMMEDIATE"))
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)


# --- enrolments --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EnrolmentRecord:
    """One enrolment with the few facts about the user and the course that a list needs."""

    id: UUID
    course_id: UUID
    user_id: UUID
    status: str
    assigned_by: UUID
    created_at: datetime
    updated_at: datetime
    user_email: str
    user_display_name: str | None
    course_title: str
    course_status: str


def _enrolment_select() -> Select[Any]:
    e = course_enrolments
    return select(
        e,
        users.c.email.label("user_email"),
        users.c.display_name.label("user_display_name"),
        training_courses.c.title.label("course_title"),
        training_courses.c.status.label("course_status"),
    ).select_from(
        e.join(users, users.c.id == e.c.user_id).join(
            training_courses, training_courses.c.id == e.c.course_id
        )
    )


def _enrolment_from_row(row: RowMapping) -> EnrolmentRecord:
    return EnrolmentRecord(**{name: row[name] for name in EnrolmentRecord.__slots__})


async def insert_enrolment_if_absent(
    session: AsyncSession, *, course_id: UUID, user_id: UUID, assigned_by: UUID
) -> UUID | None:
    """Enrol ``user_id`` unless a row for the pair already exists; returns the new id, or ``None``.

    One statement, decided by the database's unique constraint (``ON CONFLICT DO NOTHING``), not by
    a prior look: two requests racing to enrol the same user cannot both insert. The loser waits
    for the winner's transaction to end and then gets ``None``.
    """
    inserted = (
        await session.execute(
            pg_insert(course_enrolments)
            .values(
                course_id=course_id,
                user_id=user_id,
                status=ENROLMENT_ACTIVE,
                assigned_by=assigned_by,
            )
            .on_conflict_do_nothing(constraint=ENROLMENT_CONSTRAINT)
            .returning(course_enrolments.c.id)
        )
    ).scalar_one_or_none()
    return inserted


async def get_enrolment(
    session: AsyncSession, *, course_id: UUID, user_id: UUID, for_update: bool = False
) -> EnrolmentRecord | None:
    """The user's enrolment in the course (any status). ``for_update`` locks the row."""
    e = course_enrolments
    if for_update:
        locked = (
            await session.execute(
                select(e.c.id)
                .where(e.c.course_id == course_id, e.c.user_id == user_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if locked is None:
            return None
    row = (
        (
            await session.execute(
                _enrolment_select().where(e.c.course_id == course_id, e.c.user_id == user_id)
            )
        )
        .mappings()
        .one_or_none()
    )
    return _enrolment_from_row(row) if row else None


async def set_enrolment_status(
    session: AsyncSession, enrolment_id: UUID, *, status: str, assigned_by: UUID | None = None
) -> None:
    """Change the status of an enrolment **in place** (the row, and so its ``created_at`` and id,
    are kept). ``assigned_by`` is replaced only when given (a re-enrolment)."""
    values: dict[str, Any] = {"status": status}
    if assigned_by is not None:
        values["assigned_by"] = assigned_by
    await session.execute(
        update(course_enrolments).where(course_enrolments.c.id == enrolment_id).values(**values)
    )


async def list_enrolments_for_course(
    session: AsyncSession, *, course_id: UUID, status: str | None, limit: int, cursor: str | None
) -> Page[EnrolmentRecord]:
    stmt = _enrolment_select().where(course_enrolments.c.course_id == course_id)
    if status is not None:
        stmt = stmt.where(course_enrolments.c.status == status)
    rows, next_cursor = await paginate_rows(
        session, stmt, table=course_enrolments, limit=limit, cursor=cursor
    )
    return Page(items=[_enrolment_from_row(row) for row in rows], next_cursor=next_cursor)


async def list_enrolments_for_user(
    session: AsyncSession, *, user_id: UUID, status: str | None, limit: int, cursor: str | None
) -> Page[EnrolmentRecord]:
    stmt = _enrolment_select().where(course_enrolments.c.user_id == user_id)
    if status is not None:
        stmt = stmt.where(course_enrolments.c.status == status)
    rows, next_cursor = await paginate_rows(
        session, stmt, table=course_enrolments, limit=limit, cursor=cursor
    )
    return Page(items=[_enrolment_from_row(row) for row in rows], next_cursor=next_cursor)


async def list_open_courses_of_user(session: AsyncSession, user_id: UUID) -> list[CourseRecord]:
    """The courses an employee can currently see: an **active** enrolment in a **published**
    course, oldest enrolment first. (A withdrawn enrolment or a draft / archived course is simply
    absent.)"""
    e = course_enrolments
    stmt = (
        _course_select()
        .join(e, e.c.course_id == training_courses.c.id)
        .where(
            e.c.user_id == user_id,
            e.c.status == ENROLMENT_ACTIVE,
            training_courses.c.status == COURSE_PUBLISHED,
        )
        .order_by(e.c.created_at, e.c.id)
    )
    return [_course_from_row(row) for row in (await session.execute(stmt)).mappings().all()]


async def list_modules_of_courses(
    session: AsyncSession, course_ids: Sequence[UUID]
) -> dict[UUID, list[ModuleRecord]]:
    """Every module (published or not) of each course, in position order, in one query."""
    grouped: dict[UUID, list[ModuleRecord]] = {course_id: [] for course_id in course_ids}
    if not course_ids:
        return grouped
    stmt = (
        select(training_modules)
        .where(training_modules.c.course_id.in_(list(course_ids)))
        .order_by(training_modules.c.course_id, training_modules.c.position)
    )
    for row in (await session.execute(stmt)).mappings().all():
        grouped[row["course_id"]].append(_module_from_row(row))
    return grouped
