"""Assessments, attempts and answers (migration 0021): the SQL, nothing else.

No rule lives here - which questions are acceptable, what passes and who may attempt is
``domain.training_assessment`` and ``services.assessments``.

**Two kinds of read, kept apart on purpose.** :func:`list_employee_questions` returns
:class:`EmployeeQuestion` rows, a type with no field for the correct option, and its query never
touches ``assessment_answer_keys``. Only :func:`load_grading_set` (used inside the grading call) and
:func:`list_admin_questions` (administrators) read the key. An employee-facing path therefore cannot
return the answer by accident: there is nothing to return it with.

**Attempts are insert-only.** This module has :func:`insert_attempt` and
:func:`insert_attempt_answers` and *no function that updates or deletes* a row of
``assessment_attempts`` or ``assessment_attempt_answers`` - ``tests/test_architecture_rules.py``
fails if any code under ``src`` issues an UPDATE or DELETE against them - and the database refuses
one too (``reject_mutation`` triggers). An attempt is written once, already graded.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.db.tables import (
    assessment_answer_keys,
    assessment_attempt_answers,
    assessment_attempts,
    assessment_options,
    assessment_questions,
    assessments,
    training_modules,
)
from edutoon.domain.training_assessment import (
    AuthoredQuestion,
    Grade,
    SnapshotOption,
    SnapshotQuestion,
)

# --- records -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssessmentRecord:
    id: UUID
    module_id: UUID
    threshold_percent: int
    created_by: UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class EmployeeOption:
    id: UUID
    position: int
    label: str


@dataclass(frozen=True, slots=True)
class EmployeeQuestion:
    """A question as an employee may see it: no key, no correctness, no explanation."""

    id: UUID
    position: int
    prompt: str
    options: tuple[EmployeeOption, ...]


@dataclass(frozen=True, slots=True)
class AdminOption:
    id: UUID
    position: int
    label: str
    is_correct: bool


@dataclass(frozen=True, slots=True)
class AdminQuestion:
    id: UUID
    position: int
    prompt: str
    options: tuple[AdminOption, ...]


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    id: UUID
    assessment_id: UUID
    module_id: UUID
    user_id: UUID
    attempt_number: int
    threshold_percent: int
    correct_count: int
    question_count: int
    passed: bool
    idempotency_key: str
    submitted_at: datetime


def _assessment(row: Any) -> AssessmentRecord:
    return AssessmentRecord(
        id=row["id"],
        module_id=row["module_id"],
        threshold_percent=row["threshold_percent"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _attempt(row: Any) -> AttemptRecord:
    return AttemptRecord(
        id=row["id"],
        assessment_id=row["assessment_id"],
        module_id=row["module_id"],
        user_id=row["user_id"],
        attempt_number=row["attempt_number"],
        threshold_percent=row["threshold_percent"],
        correct_count=row["correct_count"],
        question_count=row["question_count"],
        passed=row["passed"],
        idempotency_key=row["idempotency_key"],
        submitted_at=row["submitted_at"],
    )


# --- authoring ---------------------------------------------------------------------------------


async def get_by_module(
    session: AsyncSession, module_id: UUID, *, share: bool = False
) -> AssessmentRecord | None:
    """The module's assessment. ``share=True`` takes a ``FOR SHARE`` lock, which waits for an
    administrator's replacement of its questions (that updates this row) to commit and then holds
    it off until the caller's transaction ends - so a grading call sees one consistent version."""
    stmt = select(assessments).where(assessments.c.module_id == module_id)
    if share:
        stmt = stmt.with_for_update(read=True)
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _assessment(row) if row is not None else None


async def upsert(
    session: AsyncSession, *, module_id: UUID, threshold_percent: int, created_by: UUID
) -> AssessmentRecord:
    """Create the module's assessment or change its threshold (``created_by`` is kept)."""
    stmt = (
        pg_insert(assessments)
        .values(module_id=module_id, threshold_percent=threshold_percent, created_by=created_by)
        .on_conflict_do_update(
            index_elements=[assessments.c.module_id],
            set_={"threshold_percent": threshold_percent},
        )
        .returning(assessments)
    )
    return _assessment((await session.execute(stmt)).mappings().one())


async def delete_assessment(session: AsyncSession, assessment_id: UUID) -> None:
    await session.execute(delete(assessments).where(assessments.c.id == assessment_id))


async def replace_questions(
    session: AsyncSession, *, assessment_id: UUID, questions: Sequence[AuthoredQuestion]
) -> None:
    """Replace the whole authored question set (questions, options, keys) of an assessment.

    Authoring rows only: attempts hold their own copy of what they were graded against, so this
    cannot change any recorded result.
    """
    await session.execute(
        delete(assessment_questions).where(assessment_questions.c.assessment_id == assessment_id)
    )  # options and keys go with their questions (ON DELETE CASCADE)
    for q_position, question in enumerate(questions, start=1):
        question_id = (
            await session.execute(
                pg_insert(assessment_questions)
                .values(assessment_id=assessment_id, position=q_position, prompt=question.prompt)
                .returning(assessment_questions.c.id)
            )
        ).scalar_one()
        correct_option_id: UUID | None = None
        for o_position, option in enumerate(question.options, start=1):
            option_id = (
                await session.execute(
                    pg_insert(assessment_options)
                    .values(question_id=question_id, position=o_position, label=option.label)
                    .returning(assessment_options.c.id)
                )
            ).scalar_one()
            if option.is_correct:
                correct_option_id = option_id
        if correct_option_id is None:  # the domain refuses this first; never write a keyless one
            raise ValueError("a question was saved without a correct option")
        await session.execute(
            pg_insert(assessment_answer_keys).values(
                question_id=question_id, correct_option_id=correct_option_id
            )
        )


async def _options_by_question(
    session: AsyncSession, question_ids: Sequence[UUID]
) -> dict[UUID, list[Any]]:
    grouped: dict[UUID, list[Any]] = {q: [] for q in question_ids}
    if not question_ids:
        return grouped
    stmt = (
        select(
            assessment_options.c.id,
            assessment_options.c.question_id,
            assessment_options.c.position,
            assessment_options.c.label,
        )
        .where(assessment_options.c.question_id.in_(list(question_ids)))
        .order_by(assessment_options.c.question_id, assessment_options.c.position)
    )
    for row in (await session.execute(stmt)).mappings():
        grouped[row["question_id"]].append(row)
    return grouped


async def _question_rows(session: AsyncSession, assessment_id: UUID) -> list[Any]:
    stmt = (
        select(
            assessment_questions.c.id,
            assessment_questions.c.position,
            assessment_questions.c.prompt,
        )
        .where(assessment_questions.c.assessment_id == assessment_id)
        .order_by(assessment_questions.c.position)
    )
    return list((await session.execute(stmt)).mappings())


async def list_employee_questions(
    session: AsyncSession, assessment_id: UUID
) -> list[EmployeeQuestion]:
    """The questions as an employee may see them. Never reads ``assessment_answer_keys``."""
    rows = await _question_rows(session, assessment_id)
    options = await _options_by_question(session, [r["id"] for r in rows])
    return [
        EmployeeQuestion(
            id=r["id"],
            position=r["position"],
            prompt=r["prompt"],
            options=tuple(
                EmployeeOption(o["id"], o["position"], o["label"]) for o in options[r["id"]]
            ),
        )
        for r in rows
    ]


async def _keys(session: AsyncSession, question_ids: Sequence[UUID]) -> dict[UUID, UUID]:
    if not question_ids:
        return {}
    stmt = select(
        assessment_answer_keys.c.question_id, assessment_answer_keys.c.correct_option_id
    ).where(assessment_answer_keys.c.question_id.in_(list(question_ids)))
    return {
        r["question_id"]: r["correct_option_id"] for r in (await session.execute(stmt)).mappings()
    }


async def list_admin_questions(session: AsyncSession, assessment_id: UUID) -> list[AdminQuestion]:
    """The questions with their keys - administrators only."""
    rows = await _question_rows(session, assessment_id)
    ids = [r["id"] for r in rows]
    options = await _options_by_question(session, ids)
    keys = await _keys(session, ids)
    return [
        AdminQuestion(
            id=r["id"],
            position=r["position"],
            prompt=r["prompt"],
            options=tuple(
                AdminOption(o["id"], o["position"], o["label"], o["id"] == keys.get(r["id"]))
                for o in options[r["id"]]
            ),
        )
        for r in rows
    ]


async def load_grading_set(session: AsyncSession, assessment_id: UUID) -> list[SnapshotQuestion]:
    """The question set with its keys, for the one grading call. A question with no key row cannot
    be graded, so it is a hard error rather than a question nobody can get right."""
    rows = await _question_rows(session, assessment_id)
    ids = [r["id"] for r in rows]
    options = await _options_by_question(session, ids)
    keys = await _keys(session, ids)
    result: list[SnapshotQuestion] = []
    for r in rows:
        if r["id"] not in keys:
            raise ValueError("an assessment question has no answer key")
        result.append(
            SnapshotQuestion(
                id=r["id"],
                position=r["position"],
                prompt=r["prompt"],
                options=tuple(
                    SnapshotOption(o["id"], o["position"], o["label"]) for o in options[r["id"]]
                ),
                correct_option_id=keys[r["id"]],
            )
        )
    return result


async def module_ids_with_assessment(
    session: AsyncSession, module_ids: Collection[UUID]
) -> frozenset[UUID]:
    """Which of ``module_ids`` have an assessment (with at least one question)."""
    if not module_ids:
        return frozenset()
    stmt = (
        select(assessments.c.module_id)
        .join(assessment_questions, assessment_questions.c.assessment_id == assessments.c.id)
        .where(assessments.c.module_id.in_(list(module_ids)))
        .distinct()
    )
    return frozenset((await session.execute(stmt)).scalars().all())


# --- attempts (insert-only) ---------------------------------------------------------------------


async def lock_user_module(session: AsyncSession, *, user_id: UUID, module_id: UUID) -> None:
    """Serialise submissions by one employee for one module until the transaction ends, so two
    concurrent ones are numbered one after the other."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"assessment-attempt:{user_id}:{module_id}"},
    )


async def get_attempt_by_key(
    session: AsyncSession, *, user_id: UUID, idempotency_key: str
) -> AttemptRecord | None:
    stmt = select(assessment_attempts).where(
        assessment_attempts.c.user_id == user_id,
        assessment_attempts.c.idempotency_key == idempotency_key,
    )
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _attempt(row) if row is not None else None


async def next_attempt_number(session: AsyncSession, *, user_id: UUID, module_id: UUID) -> int:
    stmt = select(func.coalesce(func.max(assessment_attempts.c.attempt_number), 0)).where(
        assessment_attempts.c.user_id == user_id, assessment_attempts.c.module_id == module_id
    )
    return int((await session.execute(stmt)).scalar_one()) + 1


async def insert_attempt(
    session: AsyncSession,
    *,
    assessment_id: UUID,
    module_id: UUID,
    user_id: UUID,
    attempt_number: int,
    grade: Grade,
    idempotency_key: str,
) -> AttemptRecord:
    """Write one graded attempt and the exact question set it was graded against."""
    attempt = _attempt(
        (
            await session.execute(
                pg_insert(assessment_attempts)
                .values(
                    assessment_id=assessment_id,
                    module_id=module_id,
                    user_id=user_id,
                    attempt_number=attempt_number,
                    threshold_percent=grade.threshold_percent,
                    correct_count=grade.correct,
                    question_count=grade.total,
                    passed=grade.passed,
                    idempotency_key=idempotency_key,
                )
                .returning(assessment_attempts)
            )
        )
        .mappings()
        .one()
    )
    await insert_attempt_answers(session, attempt_id=attempt.id, grade=grade)
    return attempt


async def insert_attempt_answers(session: AsyncSession, *, attempt_id: UUID, grade: Grade) -> None:
    await session.execute(
        pg_insert(assessment_attempt_answers),
        [
            {
                "attempt_id": attempt_id,
                "position": graded.question.position,
                "question_id": graded.question.id,
                "prompt": graded.question.prompt,
                "options": [
                    {"id": str(o.id), "position": o.position, "label": o.label}
                    for o in graded.question.options
                ],
                "correct_option_id": graded.question.correct_option_id,
                "selected_option_id": graded.selected_option_id,
                "is_correct": graded.is_correct,
            }
            for graded in grade.answers
        ],
    )


async def list_attempts(
    session: AsyncSession, *, user_id: UUID, module_id: UUID
) -> list[AttemptRecord]:
    stmt = (
        select(assessment_attempts)
        .where(
            assessment_attempts.c.user_id == user_id, assessment_attempts.c.module_id == module_id
        )
        .order_by(assessment_attempts.c.attempt_number)
    )
    return [_attempt(r) for r in (await session.execute(stmt)).mappings()]


async def list_answer_rows(session: AsyncSession, attempt_id: UUID) -> list[dict[str, Any]]:
    """The recorded question set of an attempt (with the key as it was) - for tests and audits."""
    stmt = (
        select(assessment_attempt_answers)
        .where(assessment_attempt_answers.c.attempt_id == attempt_id)
        .order_by(assessment_attempt_answers.c.position)
    )
    return [dict(r) for r in (await session.execute(stmt)).mappings()]


async def passed_module_ids(
    session: AsyncSession, *, user_id: UUID, module_ids: Collection[UUID]
) -> frozenset[UUID]:
    """The subset of ``module_ids`` the user has a **passing** submitted attempt for. A later
    failing attempt cannot remove a module from this set: it only ever looks for a pass."""
    if not module_ids:
        return frozenset()
    stmt = (
        select(assessment_attempts.c.module_id)
        .where(
            assessment_attempts.c.user_id == user_id,
            assessment_attempts.c.passed.is_(True),
            assessment_attempts.c.module_id.in_(list(module_ids)),
        )
        .distinct()
    )
    return frozenset((await session.execute(stmt)).scalars().all())


async def has_attempts_for_module(session: AsyncSession, module_id: UUID) -> bool:
    stmt = (
        select(assessment_attempts.c.id)
        .where(assessment_attempts.c.module_id == module_id)
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


async def has_attempts_in_course(session: AsyncSession, course_id: UUID) -> bool:
    stmt = (
        select(assessment_attempts.c.id)
        .join(training_modules, training_modules.c.id == assessment_attempts.c.module_id)
        .where(training_modules.c.course_id == course_id)
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None
