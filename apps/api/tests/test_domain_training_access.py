"""``domain/training_access.py``: the one rule for who may open a module.

Pure tests - no database. The properties that matter: it fails closed, it has exactly one outcome
for "you may not know this exists", and the order of modules is the course's published order.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from edutoon.domain.training_access import (
    ACCESS_ALLOWED,
    ACCESS_LOCKED,
    ACCESS_NOT_FOUND,
    NOT_FOUND,
    AccessDecision,
    ModuleRef,
    decide_module_access,
)

M1, M2, M3 = uuid4(), uuid4(), uuid4()


def course(*statuses: str) -> list[ModuleRef]:
    """Modules at positions 1..n with the given statuses (ids M1, M2, M3, then fresh)."""
    ids = [M1, M2, M3]
    return [
        ModuleRef(id=ids[i] if i < 3 else uuid4(), position=i + 1, status=status)
        for i, status in enumerate(statuses)
    ]


def decide(module_id: UUID = M1, **overrides) -> AccessDecision:
    facts = {
        "module_id": module_id,
        "user_role": "employee",
        "user_status": "active",
        "course_status": "published",
        "enrolment_status": "active",
        "course_modules": course("published", "published", "published"),
        "passed_module_ids": frozenset(),
    }
    return decide_module_access(**(facts | overrides))


# --- the happy path and the lock -------------------------------------------------------------


def test_the_first_module_is_open_to_an_enrolled_employee_with_nothing_passed():
    assert decide(M1) == AccessDecision(ACCESS_ALLOWED)


def test_a_later_module_is_locked_until_the_one_before_it_is_passed():
    decision = decide(M2)

    assert decision == AccessDecision(ACCESS_LOCKED, blocking_module_id=M1)


def test_passing_the_previous_module_unlocks_the_next_but_not_the_one_after():
    assert decide(M2, passed_module_ids={M1}).access == ACCESS_ALLOWED
    assert decide(M3, passed_module_ids={M1}) == AccessDecision(
        ACCESS_LOCKED, blocking_module_id=M2
    )
    assert decide(M3, passed_module_ids={M1, M2}).access == ACCESS_ALLOWED


def test_passing_a_later_module_does_not_unlock_an_earlier_gap():
    """Only an unbroken run of passes opens a module: M3 passed but M2 not does not open M3."""
    assert decide(M3, passed_module_ids={M1, M3}) == AccessDecision(
        ACCESS_LOCKED, blocking_module_id=M2
    )


def test_the_blocking_module_is_the_first_unpassed_one_in_order():
    assert decide(M3, passed_module_ids=frozenset()).blocking_module_id == M1


def test_the_course_list_may_arrive_in_any_order():
    shuffled = list(reversed(course("published", "published", "published")))

    assert decide(M3, course_modules=shuffled).blocking_module_id == M1


def test_unpublished_modules_are_skipped_in_the_ordering():
    """With M1 unpublished, M2 is the first published module and is open; M3 waits for M2."""
    modules = course("unpublished", "published", "published")

    assert decide(M2, course_modules=modules).access == ACCESS_ALLOWED
    assert decide(M3, course_modules=modules) == AccessDecision(
        ACCESS_LOCKED, blocking_module_id=M2
    )


def test_nobody_passed_means_every_module_after_the_first_is_locked():
    """The Step 2 situation exactly: an empty pass set. Fail closed for every later module."""
    modules = course("published", "published", "published", "published")
    ids = [m.id for m in modules]

    outcomes = [
        decide(module_id, course_modules=modules, passed_module_ids=frozenset()).access
        for module_id in ids
    ]

    assert outcomes == [ACCESS_ALLOWED, ACCESS_LOCKED, ACCESS_LOCKED, ACCESS_LOCKED]


# --- every "you may not know" reason is the same single outcome ------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"user_role": "admin"},
        {"user_role": "someone-else"},
        {"user_status": "suspended"},
        {"user_status": "deleted"},
        {"course_status": None},
        {"course_status": "draft"},
        {"course_status": "archived"},
        {"course_status": "something-new"},
        {"enrolment_status": None},
        {"enrolment_status": "withdrawn"},
        {"enrolment_status": "something-new"},
        {"course_modules": []},
        {"course_modules": course("unpublished", "published", "published")},  # M1 itself
    ],
)
def test_every_reason_the_caller_must_not_learn_about_is_the_one_not_found_outcome(overrides):
    decision = decide(M1, **overrides)

    assert decision == NOT_FOUND
    assert decision.access == ACCESS_NOT_FOUND
    assert decision.blocking_module_id is None


def test_a_module_that_is_not_in_the_course_is_not_found():
    assert decide(uuid4()) == NOT_FOUND


def test_an_unpublished_module_is_not_found_even_when_everything_else_is_fine():
    modules = course("published", "unpublished", "published")

    assert decide(M2, course_modules=modules, passed_module_ids={M1}) == NOT_FOUND


def test_a_missing_module_and_a_protected_one_are_indistinguishable():
    """Not enrolled in the real module's course vs. a module that does not exist at all: equal."""
    protected = decide(M1, enrolment_status=None)
    missing = decide(uuid4())

    assert protected == missing == NOT_FOUND


# --- fail closed ------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "ACTIVE", "published ", "true"])
def test_an_unrecognised_status_is_refused_not_accepted(bad):
    assert decide(M1, enrolment_status=bad) == NOT_FOUND
    assert decide(M1, course_status=bad) == NOT_FOUND
    assert decide(M1, course_modules=[ModuleRef(id=M1, position=1, status=bad)]) == NOT_FOUND


def test_a_pass_for_an_unknown_module_id_opens_nothing():
    assert decide(M2, passed_module_ids={uuid4(), uuid4()}).access == ACCESS_LOCKED


def test_the_pass_collection_can_be_any_collection_and_is_never_mutated():
    passed = [M1]  # a list, not a set

    assert decide(M2, passed_module_ids=passed).access == ACCESS_ALLOWED
    assert passed == [M1]


def test_equal_positions_never_unlock_each_other():
    """Two modules cannot share a position (a UNIQUE constraint) - but if corrupt data ever
    presented that, neither may be treated as the other's predecessor, so neither is opened by it
    and neither is locked by it."""
    a, b = (
        ModuleRef(id=M1, position=1, status="published"),
        ModuleRef(id=M2, position=1, status="published"),
    )

    assert decide(M1, course_modules=[a, b]).access == ACCESS_ALLOWED
    assert decide(M2, course_modules=[a, b]).access == ACCESS_ALLOWED
