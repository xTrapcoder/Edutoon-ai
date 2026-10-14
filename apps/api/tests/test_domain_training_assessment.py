"""The assessment rules, pure: the integer pass gate, grading, authoring and the progression rule.

No database, no clock, no float: ``correct * 100 >= threshold * total``.
"""

from __future__ import annotations

import ast
import inspect
from uuid import uuid4

import pytest

from edutoon.domain import training_assessment
from edutoon.domain.training_access import (
    ACCESS_ALLOWED,
    ACCESS_LOCKED,
    ModuleRef,
    decide_module_access,
)
from edutoon.domain.training_assessment import (
    Answer,
    AuthoredOption,
    AuthoredQuestion,
    SnapshotOption,
    SnapshotQuestion,
    answers_problem,
    authoring_problem,
    grade,
    passes,
    requires_pass,
)

# --- the pass gate, in integers ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("correct", "total", "threshold", "expected"),
    [
        (7, 10, 70, True),  # exactly the threshold passes
        (69, 100, 70, False),  # one short fails
        (70, 100, 70, True),
        (2, 3, 70, False),  # 66.67%
        (5, 7, 70, True),  # 71.43%
        (3, 3, 70, True),
        (0, 3, 70, False),
        (1, 1, 100, True),
        (99, 100, 100, False),
        (1, 100, 1, True),  # the lowest threshold: at least 1%
        (0, 100, 1, False),
        (0, 0, 70, False),  # an empty assessment never passes
        (29, 100, 29, True),  # float: 29 / 100 * 100 == 28.999999999999996, integers are exact
        (57, 100, 57, True),  # float: 56.99999999999999
        (2, 3, 67, False),  # 200 >= 201 is False: no rounding rescues 66.67% against 67
        (2, 3, 66, True),
    ],
)
def test_the_pass_gate_is_decided_with_integers(correct, total, threshold, expected):
    assert passes(correct=correct, total=total, threshold_percent=threshold) is expected


def test_the_grading_path_contains_no_float_division():
    """Structural: no ``/`` operator and no ``float`` / ``round`` / ``Decimal`` in the module, so no
    float can reach the gate."""
    source = inspect.getsource(training_assessment)
    tree = ast.parse(source)
    divisions = [
        n for n in ast.walk(tree) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div)
    ]
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert divisions == []
    assert not names & {"float", "round", "Decimal"}


# --- grading -------------------------------------------------------------------------------------


def question(position: int, options: int = 3, correct: int = 0) -> SnapshotQuestion:
    ops = tuple(SnapshotOption(uuid4(), i + 1, f"option {i + 1}") for i in range(options))
    return SnapshotQuestion(uuid4(), position, f"question {position}", ops, ops[correct].id)


def answers(questions, right: int) -> list[Answer]:
    result = []
    for index, q in enumerate(questions):
        right_one = next(o for o in q.options if o.id == q.correct_option_id)
        wrong_one = next(o for o in q.options if o.id != q.correct_option_id)
        result.append(Answer(q.id, (right_one if index < right else wrong_one).id))
    return result


def test_grading_counts_correct_answers_and_applies_the_stored_threshold():
    questions = [question(i + 1) for i in range(10)]

    seven = grade(questions, answers(questions, 7), threshold_percent=70)
    six = grade(questions, answers(questions, 6), threshold_percent=70)
    six_lenient = grade(questions, answers(questions, 6), threshold_percent=60)

    assert (seven.correct, seven.total, seven.passed) == (7, 10, True)
    assert (six.correct, six.total, six.passed) == (6, 10, False)
    assert six_lenient.passed is True  # the threshold is an argument, never read from elsewhere
    assert seven.threshold_percent == 70


def test_graded_answers_come_back_in_question_order_whatever_order_they_were_sent():
    questions = [question(i + 1) for i in range(4)]
    shuffled = list(reversed(answers(questions, 4)))

    graded = grade(questions, shuffled, threshold_percent=70)

    assert [g.question.position for g in graded.answers] == [1, 2, 3, 4]


def test_all_wrong_and_all_right():
    questions = [question(i + 1) for i in range(5)]

    assert grade(questions, answers(questions, 0), threshold_percent=70).correct == 0
    best = grade(questions, answers(questions, 5), threshold_percent=70)
    assert (best.correct, best.passed) == (5, True)


# --- what cannot be graded -----------------------------------------------------------------------


def test_every_question_must_be_answered_exactly_once_with_its_own_option():
    questions = [question(1), question(2)]
    good = answers(questions, 2)
    foreign = Answer(questions[0].id, questions[1].options[0].id)

    assert answers_problem(questions, good) is None
    assert answers_problem(questions, good[:1]) is not None  # a question left unanswered
    assert answers_problem(questions, [good[0], good[0]]) is not None  # one answered twice
    assert answers_problem(questions, [*good, good[0]]) is not None
    assert answers_problem(questions, [foreign, good[1]]) is not None  # another question's option
    assert answers_problem(questions, [Answer(uuid4(), uuid4()), *good]) is not None  # unknown
    assert answers_problem(questions, []) is not None


def test_grade_refuses_what_answers_problem_refuses():
    questions = [question(1), question(2)]
    with pytest.raises(ValueError):
        grade(questions, answers(questions, 2)[:1], threshold_percent=70)


# --- authoring -----------------------------------------------------------------------------------


def authored(prompt="Which?", labels=("A", "B", "C"), correct=0) -> AuthoredQuestion:
    return AuthoredQuestion(
        prompt, tuple(AuthoredOption(label, i == correct) for i, label in enumerate(labels))
    )


def test_a_valid_assessment_is_accepted():
    assert authoring_problem([authored()], 70) is None
    assert authoring_problem([authored(labels=("Yes", "No"))], 1) is None
    assert authoring_problem([authored()] * training_assessment.MAX_QUESTIONS, 100) is None


@pytest.mark.parametrize("threshold", [0, -1, 101, 1000])
def test_a_threshold_outside_1_to_100_is_refused(threshold):
    assert authoring_problem([authored()], threshold) is not None


def test_the_shape_of_the_questions_is_enforced():
    too_many = [authored()] * (training_assessment.MAX_QUESTIONS + 1)
    assert authoring_problem([], 70) is not None
    assert authoring_problem(too_many, 70) is not None
    assert authoring_problem([authored(prompt="  ")], 70) is not None
    assert authoring_problem([authored(labels=("Only",))], 70) is not None
    assert authoring_problem([authored(labels=tuple("ABCDEFG"))], 70) is not None
    assert authoring_problem([authored(labels=("A", "a"))], 70) is not None  # same label
    assert authoring_problem([authored(labels=("A", " "))], 70) is not None  # blank label


def test_exactly_one_option_must_be_correct():
    none_correct = AuthoredQuestion("Q?", (AuthoredOption("A", False), AuthoredOption("B", False)))
    two_correct = AuthoredQuestion("Q?", (AuthoredOption("A", True), AuthoredOption("B", True)))

    assert authoring_problem([none_correct], 70) is not None
    assert authoring_problem([two_correct], 70) is not None


# --- progression: a module without an assessment blocks nothing ---------------------------


def modules(n: int) -> list[ModuleRef]:
    return [ModuleRef(uuid4(), i + 1, "published") for i in range(n)]


def decide(course_modules, target, *, passed=(), assessed=None):
    return decide_module_access(
        module_id=target.id,
        user_role="employee",
        user_status="active",
        course_status="published",
        enrolment_status="active",
        course_modules=course_modules,
        passed_module_ids=frozenset(passed),
        assessed_module_ids=assessed,
    )


def test_not_being_told_which_modules_have_an_assessment_fails_closed():
    ms = modules(2)
    assert requires_pass(ms[0].id, None) is True
    assert decide(ms, ms[1]).access == ACCESS_LOCKED  # no assessed set given: every module blocks


def test_a_module_without_an_assessment_does_not_block_the_next():
    ms = modules(3)
    assert decide(ms, ms[1], assessed=frozenset()).access == ACCESS_ALLOWED
    assert decide(ms, ms[2], assessed=frozenset()).access == ACCESS_ALLOWED


def test_module_n_unlocks_when_every_lower_module_that_has_an_assessment_is_passed():
    ms = modules(4)
    assessed = frozenset({ms[0].id, ms[2].id})  # modules 1 and 3 have one; 2 does not

    assert decide(ms, ms[1], assessed=assessed).access == ACCESS_LOCKED  # 1 not passed
    assert decide(ms, ms[1], passed={ms[0].id}, assessed=assessed).access == ACCESS_ALLOWED
    blocked = decide(ms, ms[3], passed={ms[0].id}, assessed=assessed)  # 3 not passed
    assert (blocked.access, blocked.blocking_module_id) == (ACCESS_LOCKED, ms[2].id)
    assert (
        decide(ms, ms[3], passed={ms[0].id, ms[2].id}, assessed=assessed).access == ACCESS_ALLOWED
    )
    # passing the module without an assessment (or one that is not assessed) changes nothing
    assert decide(ms, ms[3], passed={ms[1].id}, assessed=assessed).access == ACCESS_LOCKED
