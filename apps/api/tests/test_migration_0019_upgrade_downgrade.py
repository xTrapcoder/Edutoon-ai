"""Migration 0019 (user roles and invitations) upgraded and downgraded for real, with Alembic, on a
scratch database (mirrors ``test_migration_0018_upgrade_downgrade.py``).

The point of the test is the backfill policy: **least privilege**. Existing users become
employees; only an address explicitly named on ``ADMIN_BOOTSTRAP_EMAILS`` becomes an administrator;
nobody is promoted because of what they own; no user row is lost; and the schema refuses a user
without a role.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.exc import DBAPIError

from tests.migration_helpers import run_alembic, scratch_database, sql

ALLOW_LIST = "Boss@Example.com; second.admin@example.com"


@pytest.fixture
def scratch_url() -> Iterator[str]:
    with scratch_database() as url:
        yield url


def _seed_users_at_0018(url: str) -> None:
    """Three pre-roles users; one owns a project. None has a role column yet."""
    for email, clerk_id in (
        ("boss@example.com", "clerk_boss"),
        ("owner.of.projects@example.com", "clerk_owner"),
        ("second.admin@example.com", None),
    ):
        sql(url, "INSERT INTO users (email, clerk_user_id) VALUES (:e, :c)", e=email, c=clerk_id)
    [owner] = sql(url, "SELECT id FROM users WHERE email = 'owner.of.projects@example.com'")
    sql(
        url,
        "INSERT INTO projects (owner_id, title, source_type) VALUES (:o, 'Mine', 'pdf')",
        o=owner["id"],
    )


def _roles(url: str) -> dict[str, str]:
    return {row["email"]: row["role"] for row in sql(url, "SELECT email, role FROM users")}


def test_only_the_explicitly_named_addresses_become_administrators(scratch_url):
    run_alembic(scratch_url, "upgrade", "0018")
    _seed_users_at_0018(scratch_url)

    output = run_alembic(scratch_url, "upgrade", "0019", env={"ADMIN_BOOTSTRAP_EMAILS": ALLOW_LIST})

    assert _roles(scratch_url) == {
        "boss@example.com": "admin",
        "second.admin@example.com": "admin",
        # Owns a project, but ownership is not a reason to be promoted.
        "owner.of.projects@example.com": "employee",
    }
    assert "3 existing user(s); 2 promoted to admin" in output


def test_with_no_allow_list_nobody_is_promoted_and_the_log_says_so(scratch_url):
    run_alembic(scratch_url, "upgrade", "0018")
    _seed_users_at_0018(scratch_url)

    output = run_alembic(scratch_url, "upgrade", "0019", env={"ADMIN_BOOTSTRAP_EMAILS": ""})

    assert set(_roles(scratch_url).values()) == {"employee"}
    assert "no existing user is an administrator" in output
    assert "grant_admin.py" in output


def test_an_allow_list_address_with_no_account_promotes_nobody_and_creates_nobody(scratch_url):
    run_alembic(scratch_url, "upgrade", "0018")
    _seed_users_at_0018(scratch_url)

    run_alembic(scratch_url, "upgrade", "0019", env={"ADMIN_BOOTSTRAP_EMAILS": "ghost@example.com"})

    assert set(_roles(scratch_url).values()) == {"employee"}
    assert len(_roles(scratch_url)) == 3


def test_upgrading_an_empty_database_works(scratch_url):
    run_alembic(scratch_url, "upgrade", "0019", env={"ADMIN_BOOTSTRAP_EMAILS": ""})

    assert sql(scratch_url, "SELECT count(*) AS n FROM users") == [{"n": 0}]


def test_upgrade_keeps_every_user_and_project_untouched(scratch_url):
    run_alembic(scratch_url, "upgrade", "0018")
    _seed_users_at_0018(scratch_url)
    before = sql(scratch_url, "SELECT id, email, clerk_user_id, status FROM users ORDER BY email")

    run_alembic(scratch_url, "upgrade", "0019", env={"ADMIN_BOOTSTRAP_EMAILS": ALLOW_LIST})

    after = sql(scratch_url, "SELECT id, email, clerk_user_id, status FROM users ORDER BY email")
    assert after == before
    assert sql(scratch_url, "SELECT count(*) AS n FROM projects") == [{"n": 1}]


def test_the_schema_refuses_a_user_without_a_valid_role(scratch_url):
    run_alembic(scratch_url, "upgrade", "0019", env={"ADMIN_BOOTSTRAP_EMAILS": ""})

    [column] = sql(
        scratch_url,
        "SELECT is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'users' AND column_name = 'role'",
    )
    assert column == {"is_nullable": "NO", "column_default": None}  # NOT NULL, and no default

    with pytest.raises(DBAPIError):
        sql(scratch_url, "INSERT INTO users (email) VALUES ('norole@example.com')")
    with pytest.raises(DBAPIError):
        sql(scratch_url, "INSERT INTO users (email, role) VALUES ('bad@example.com', 'owner')")
    sql(scratch_url, "INSERT INTO users (email, role) VALUES ('ok@example.com', 'employee')")


def test_invitation_constraints(scratch_url):
    run_alembic(scratch_url, "upgrade", "0019", env={"ADMIN_BOOTSTRAP_EMAILS": ""})
    [admin] = sql(
        scratch_url,
        "INSERT INTO users (email, role) VALUES ('a@example.com', 'admin') RETURNING id",
    )

    def invite(email="new@example.com", role="employee", status="pending", extra=""):
        return sql(
            scratch_url,
            "INSERT INTO invitations (email, role, status, invited_by, expires_at) "
            f"VALUES (:e, :r, :s, :i, now() + interval '1 day'){extra}",
            e=email,
            r=role,
            s=status,
            i=admin["id"],
        )

    invite()
    with pytest.raises(DBAPIError):  # one pending invitation per address
        invite()
    invite(status="revoked")  # ...but a revoked one does not hold the slot
    with pytest.raises(DBAPIError):  # lower-case addresses only
        invite(email="Upper@Example.com")
    with pytest.raises(DBAPIError):  # known roles only
        invite(email="r@example.com", role="owner")
    with pytest.raises(DBAPIError):  # 'accepted' needs the user and the time
        invite(email="acc@example.com", status="accepted")
    with pytest.raises(DBAPIError):  # known statuses only
        invite(email="s@example.com", status="expired")


def test_downgrade_removes_only_what_0019_added_and_it_upgrades_again(scratch_url):
    run_alembic(scratch_url, "upgrade", "0018")
    _seed_users_at_0018(scratch_url)
    run_alembic(scratch_url, "upgrade", "0019", env={"ADMIN_BOOTSTRAP_EMAILS": ALLOW_LIST})

    run_alembic(scratch_url, "downgrade", "0018")

    columns = {
        row["column_name"]
        for row in sql(
            scratch_url,
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'users'",
        )
    }
    assert "role" not in columns
    assert sql(scratch_url, "SELECT to_regclass('public.invitations') AS t") == [{"t": None}]
    assert sql(scratch_url, "SELECT count(*) AS n FROM users") == [{"n": 3}]  # no user lost
    assert sql(scratch_url, "SELECT count(*) AS n FROM projects") == [{"n": 1}]

    run_alembic(scratch_url, "upgrade", "head", env={"ADMIN_BOOTSTRAP_EMAILS": ALLOW_LIST})
    assert _roles(scratch_url)["boss@example.com"] == "admin"
