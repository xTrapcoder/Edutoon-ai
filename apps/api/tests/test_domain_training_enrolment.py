"""The pure enrolment rules (``domain/training.py``): who can be enrolled and what enrolling or
withdrawing must do for each state a user's enrolment can be in. No I/O."""

from __future__ import annotations

import pytest

from edutoon.domain.roles import ROLE_ADMIN, ROLE_EMPLOYEE
from edutoon.domain.training import (
    ENROLMENT_ACTIVE,
    ENROLMENT_WITHDRAWN,
    can_enrol,
    decide_enrolment,
    decide_withdrawal,
)


def test_only_an_employee_can_be_enrolled() -> None:
    assert can_enrol(ROLE_EMPLOYEE) is True
    assert can_enrol(ROLE_ADMIN) is False
    assert can_enrol("") is False
    assert can_enrol("superuser") is False


@pytest.mark.parametrize(
    ("existing", "action"),
    [
        (None, "create"),
        (ENROLMENT_WITHDRAWN, "reactivate"),
        (ENROLMENT_ACTIVE, "already_active"),
        (
            "something-new",
            "already_active",
        ),  # a status this code does not know: refuse, change nothing
    ],
)
def test_what_enrolling_does_for_each_state(existing: str | None, action: str) -> None:
    assert decide_enrolment(existing) == action


@pytest.mark.parametrize(
    ("existing", "action"),
    [
        (None, "not_found"),
        (ENROLMENT_ACTIVE, "withdraw"),
        (ENROLMENT_WITHDRAWN, "already_withdrawn"),
        ("something-new", "already_withdrawn"),
    ],
)
def test_what_withdrawing_does_for_each_state(existing: str | None, action: str) -> None:
    assert decide_withdrawal(existing) == action
