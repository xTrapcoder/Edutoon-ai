"""The pure rules in ``domain/roles.py``."""

from __future__ import annotations

import pytest

from edutoon.domain import roles


def test_the_role_vocabulary_is_admin_and_employee_only():
    assert {"admin", "employee"} == roles.ROLES


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, set()),
        ("", set()),
        ("  ", set()),
        ("a@x.co", {"a@x.co"}),
        (
            "A@X.co, b@x.co ;c@x.co\nd@x.co  e@x.co,,",
            {"a@x.co", "b@x.co", "c@x.co", "d@x.co", "e@x.co"},
        ),
    ],
)
def test_parse_email_list_normalises_and_drops_blanks(raw, expected):
    assert roles.parse_email_list(raw) == expected


def test_normalise_email_lowercases_and_strips():
    assert roles.normalise_email("  Ada@Example.COM ") == "ada@example.com"


@pytest.mark.parametrize(
    ("verified", "admin_exists", "listed", "allowed"),
    [
        (True, False, True, True),
        (False, False, True, False),  # address not proven to be theirs
        (True, True, True, False),  # an administrator already exists: the list is spent
        (True, False, False, False),  # not on the list
    ],
)
def test_first_admin_bootstrap_needs_all_three_conditions(verified, admin_exists, listed, allowed):
    emails = frozenset({"boss@x.co"}) if listed else frozenset({"other@x.co"})

    assert (
        roles.may_bootstrap_first_admin(
            email="Boss@X.co",
            email_verified=verified,
            bootstrap_emails=emails,
            active_admin_exists=admin_exists,
        )
        is allowed
    )


@pytest.mark.parametrize(
    ("target_is_admin", "stays_admin", "count", "blocked"),
    [
        (True, False, 1, True),  # the only one leaves
        (True, False, 2, False),  # another remains
        (True, True, 1, False),  # stays an active administrator
        (False, False, 1, False),  # not an administrator to begin with
        (True, False, 0, True),
    ],
)
def test_would_leave_no_active_admin(target_is_admin, stays_admin, count, blocked):
    assert (
        roles.would_leave_no_active_admin(
            target_is_active_admin=target_is_admin,
            stays_active_admin=stays_admin,
            active_admin_count=count,
        )
        is blocked
    )
