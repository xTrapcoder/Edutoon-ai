"""Assessments: the vocabulary, the authoring rules and the grading, pure (rule 1).

One optional assessment per module: single-correct multiple choice only. Grading is **integer
only** - there is no float division anywhere in this module or in the path that calls it:

    passed  <=>  correct * 100 >= threshold_percent * total

so 7/10 passes a 70% threshold, 69/100 fails it, 70/100 passes it and 2/3 fails it, with no
rounding question to argue about. The threshold is a property of the *assessment* (stored by the
administrator, 70 by default) and is copied onto every attempt, together with the exact question
set the attempt was graded against; nothing here reads configuration.

Nothing in this module performs I/O.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

DEFAULT_THRESHOLD_PERCENT: Final = 70
MIN_THRESHOLD_PERCENT: Final = 1
MAX_THRESHOLD_PERCENT: Final = 100
"""``ck_assessments_threshold_percent``: 0 would pass everyone, so it is not allowed."""

MAX_QUESTIONS: Final = 50
MIN_OPTIONS: Final = 2
MAX_OPTIONS: Final = 6
PROMPT_MAX_CHARS: Final = 1000
LABEL_MAX_CHARS: Final = 500

ASSESSMENT_NOT_FOUND: Final = "Assessment not found."
"""The one fixed message for every employee-side 404 on an assessment route: no such module, not
enrolled, enrolment withdrawn, course not published, module not published, the caller is not an
employee, and the module simply has no assessment. Free of any id, so an assessment that exists
behind a module the caller cannot reach is **byte-identical** to one that does not exist."""

ASSESSMENT_DELETE_REFUSED: Final = (
    "This assessment has attempts, so it cannot be deleted. Replace its questions instead: "
    "earlier attempts keep the question set and threshold they were graded with."
)
MODULE_DELETE_REFUSED: Final = (
    "This module has learner attempts, so it cannot be deleted. Unpublish it instead."
)
REORDER_REFUSED: Final = (
    "This course has learner attempts, so its modules can no longer be reordered: that would "
    "change which modules earlier results unlock. Create a new course with the order you want "
    "and enrol employees in it."
)


def passes(*, correct: int, total: int, threshold_percent: int) -> bool:
    """The pass gate, in integers. An empty assessment (``total`` of 0) never passes."""
    return total > 0 and correct * 100 >= threshold_percent * total


# --- authoring ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuthoredOption:
    label: str
    is_correct: bool


@dataclass(frozen=True, slots=True)
class AuthoredQuestion:
    prompt: str
    options: tuple[AuthoredOption, ...]


def authoring_problem(questions: Sequence[AuthoredQuestion], threshold_percent: int) -> str | None:
    """Why this set of questions cannot be saved as an assessment, or ``None`` if it can.

    A valid assessment has a threshold in range, between 1 and ``MAX_QUESTIONS`` questions, and
    for every question a non-blank prompt, between ``MIN_OPTIONS`` and ``MAX_OPTIONS`` distinct
    non-blank labels and **exactly one** correct option.
    """
    if not MIN_THRESHOLD_PERCENT <= threshold_percent <= MAX_THRESHOLD_PERCENT:
        return f"The pass threshold must be between {MIN_THRESHOLD_PERCENT} and 100 percent."
    if not 1 <= len(questions) <= MAX_QUESTIONS:
        return f"An assessment needs between 1 and {MAX_QUESTIONS} questions."
    for number, question in enumerate(questions, start=1):
        if not question.prompt.strip() or len(question.prompt) > PROMPT_MAX_CHARS:
            return f"Question {number} needs a prompt of 1 to {PROMPT_MAX_CHARS} characters."
        if not MIN_OPTIONS <= len(question.options) <= MAX_OPTIONS:
            return f"Question {number} needs between {MIN_OPTIONS} and {MAX_OPTIONS} options."
        labels = [option.label.strip().casefold() for option in question.options]
        if any(not label for label in labels) or any(
            len(option.label) > LABEL_MAX_CHARS for option in question.options
        ):
            return f"Question {number} has an option label that is blank or too long."
        if len(set(labels)) != len(labels):
            return f"Question {number} has two options with the same label."
        if sum(1 for option in question.options if option.is_correct) != 1:
            return f"Question {number} must have exactly one correct option."
    return None


# --- grading -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SnapshotOption:
    id: UUID
    position: int
    label: str


@dataclass(frozen=True, slots=True)
class SnapshotQuestion:
    """One question as it stands when an attempt is graded, with its key. Only the grading call
    sees this; no employee-facing type has a field for ``correct_option_id``."""

    id: UUID
    position: int
    prompt: str
    options: tuple[SnapshotOption, ...]
    correct_option_id: UUID


@dataclass(frozen=True, slots=True)
class Answer:
    question_id: UUID
    option_id: UUID


@dataclass(frozen=True, slots=True)
class GradedAnswer:
    question: SnapshotQuestion
    selected_option_id: UUID
    is_correct: bool


@dataclass(frozen=True, slots=True)
class Grade:
    answers: tuple[GradedAnswer, ...]
    correct: int
    total: int
    threshold_percent: int
    passed: bool


def answers_problem(questions: Sequence[SnapshotQuestion], answers: Sequence[Answer]) -> str | None:
    """Why ``answers`` cannot be graded against ``questions``, or ``None``.

    Every question must be answered **exactly once** with one of *its own* options. A missing
    answer, a repeat, an unknown question and an option of another question are all refused whole
    (nothing is written), so a partial or tampered submission can never produce a grade.
    """
    by_id = {q.id: q for q in questions}
    seen: set[UUID] = set()
    for answer in answers:
        question = by_id.get(answer.question_id)
        if question is None:
            return "An answer names a question that is not in this assessment."
        if answer.question_id in seen:
            return "A question was answered more than once."
        seen.add(answer.question_id)
        if answer.option_id not in {o.id for o in question.options}:
            return "An answer names an option that does not belong to its question."
    if seen != set(by_id):
        return "Every question must be answered."
    return None


def grade(
    questions: Sequence[SnapshotQuestion],
    answers: Sequence[Answer],
    *,
    threshold_percent: int,
) -> Grade:
    """Grade ``answers`` against ``questions``; refuses (``ValueError``) what
    :func:`answers_problem` refuses, so an ungradeable submission can never be graded."""
    problem = answers_problem(questions, answers)
    if problem is not None:
        raise ValueError(problem)
    chosen = {a.question_id: a.option_id for a in answers}
    graded = tuple(
        GradedAnswer(q, chosen[q.id], chosen[q.id] == q.correct_option_id)
        for q in sorted(questions, key=lambda q: q.position)
    )
    correct = sum(1 for g in graded if g.is_correct)
    total = len(graded)
    return Grade(
        answers=graded,
        correct=correct,
        total=total,
        threshold_percent=threshold_percent,
        passed=passes(correct=correct, total=total, threshold_percent=threshold_percent),
    )


def requires_pass(module_id: UUID, assessed_module_ids: Collection[UUID] | None) -> bool:
    """Whether a module blocks the ones after it until it is passed.

    Only a module that *has an assessment* does. ``None`` means "not told which modules have one"
    and is the fail-closed reading: every module requires a pass.
    """
    return assessed_module_ids is None or module_id in assessed_module_ids
