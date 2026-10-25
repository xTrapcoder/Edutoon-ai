"""The seam between assessment results and the access rule (``passed_module_ids``).

Step 3 replaced its body with a query for a submitted, passing attempt (see
``test_assessments_api.py`` for the attempts themselves). These tests keep the seam honest for
someone who has attempted nothing: it answers "nothing passed", and the access rule really does go
through it - whatever it answers is what the rule uses.
"""

from __future__ import annotations

from uuid import uuid4

from edutoon.domain.training_access import (
    ACCESS_ALLOWED,
    ACCESS_LOCKED,
    ModuleRef,
    decide_module_access,
)
from edutoon.services import training_access


async def test_the_seam_says_a_user_with_no_attempts_has_passed_nothing(db_session):
    ids = [uuid4() for _ in range(5)]

    passed = await training_access.passed_module_ids(db_session, user_id=uuid4(), module_ids=ids)

    assert passed == frozenset()


async def test_the_seam_answers_nothing_for_any_user_with_no_attempts(db_session):
    for user_id in (uuid4(), uuid4()):
        assert (
            await training_access.passed_module_ids(db_session, user_id=user_id, module_ids=[])
            == frozenset()
        )


async def test_with_no_attempts_module_one_is_open_and_every_assessed_module_blocks_the_rest(
    db_session,
):
    modules = [ModuleRef(id=uuid4(), position=i, status="published") for i in range(1, 5)]
    user_id = uuid4()
    passed = await training_access.passed_module_ids(
        db_session, user_id=user_id, module_ids=[m.id for m in modules]
    )

    outcomes = [
        decide_module_access(
            module_id=m.id,
            user_role="employee",
            user_status="active",
            course_status="published",
            enrolment_status="active",
            course_modules=modules,
            passed_module_ids=passed,
            assessed_module_ids={m.id for m in modules},  # every module has an assessment
        ).access
        for m in modules
    ]

    assert outcomes == [ACCESS_ALLOWED, ACCESS_LOCKED, ACCESS_LOCKED, ACCESS_LOCKED]


async def test_the_rule_really_consumes_the_seams_answer(db_session, monkeypatch):
    """Mutation guard: if a seam ever answered 'everything passed', the next module would open.
    This proves the seam is wired to the decision (so Step 3's real query will be effective) and
    that the Step 2 lock comes from the seam's empty answer, not from a hard-coded refusal."""
    modules = [ModuleRef(id=uuid4(), position=i, status="published") for i in (1, 2)]

    async def everything_passed(session, *, user_id, module_ids):
        return frozenset(module_ids)

    monkeypatch.setattr(training_access, "passed_module_ids", everything_passed)
    passed = await training_access.passed_module_ids(
        db_session, user_id=uuid4(), module_ids=[m.id for m in modules]
    )

    decision = decide_module_access(
        module_id=modules[1].id,
        user_role="employee",
        user_status="active",
        course_status="published",
        enrolment_status="active",
        course_modules=modules,
        passed_module_ids=passed,
    )

    assert decision.access == ACCESS_ALLOWED
