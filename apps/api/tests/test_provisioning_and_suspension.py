"""Who may become a user, which role they get, and how a suspended account is refused.

Real logic throughout (``real_auth``): no legacy shim. The rules under test:

* an identity nobody invited is refused with 403 and **no row is created**;
* an identity becomes a user only through a pending, unexpired invitation for its **verified**
  address (the invitation's role is the user's role) or as the first administrator named in
  ``ADMIN_BOOTSTRAP_EMAILS`` while no active administrator exists;
* nothing in the token - including a ``role`` claim - can choose or raise a role;
* a suspended (or deleted) user is refused by ``get_current_user`` on every request.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from edutoon.core.errors import NotInvitedError
from edutoon.providers.clerk import ClerkClaims
from edutoon.repositories import invitations as invitations_repo
from edutoon.repositories import users as users_repo
from edutoon.services import auth as auth_service
from tests.role_fixtures import suspend_every_existing_admin

pytestmark = pytest.mark.real_auth


def _claims(email: str, *, verified: bool = True) -> ClerkClaims:
    return ClerkClaims(clerk_user_id=f"clerk_{uuid4()}", email=email, email_verified=verified)


async def _inviter(db_session):
    return await users_repo.create(db_session, email=f"{uuid4()}@example.com", role="admin")


async def _invite(db_session, *, email: str, role: str = "employee", ttl_seconds: int = 3600):
    inviter = await _inviter(db_session)
    return await invitations_repo.create(
        db_session, email=email, role=role, invited_by=inviter.id, ttl_seconds=ttl_seconds
    )


async def _user_exists(db_session, email: str) -> bool:
    return (
        await db_session.execute(text("SELECT 1 FROM users WHERE email = :e"), {"e": email})
    ).first() is not None


def _bootstrap(monkeypatch, emails: str) -> None:
    monkeypatch.setattr(
        auth_service, "get_settings", lambda: SimpleNamespace(ADMIN_BOOTSTRAP_EMAILS=emails)
    )


# --- the uninvited ---------------------------------------------------------------------------


async def test_an_uninvited_identity_is_refused_and_nothing_is_created(db_session):
    claims = _claims(f"{uuid4()}@example.com")

    with pytest.raises(NotInvitedError):
        await auth_service.resolve_user(db_session, claims)

    assert not await _user_exists(db_session, claims.email)


async def test_an_uninvited_http_request_is_403_not_invited_and_creates_no_account(
    role_client, mint_token, db_session
):
    email = f"{uuid4()}@example.com"
    token = mint_token(email=email)

    first = await role_client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    second = await role_client.get("/v1/projects", headers={"Authorization": f"Bearer {token}"})

    assert first.status_code == second.status_code == 403
    assert first.json()["error"]["code"] == "not_invited"
    assert not await _user_exists(db_session, email)


# --- invitations -----------------------------------------------------------------------------


async def test_a_pending_invitation_provisions_the_user_with_the_invited_role(db_session):
    email = f"{uuid4()}@example.com"
    invitation = await _invite(db_session, email=email, role="employee")
    claims = _claims(email)

    user = await auth_service.resolve_user(db_session, claims)

    assert (user.email, user.role, user.status) == (email, "employee", "active")
    assert user.clerk_user_id == claims.clerk_user_id
    accepted = await invitations_repo.get_by_id(db_session, invitation.id)
    assert accepted.status == "accepted" and accepted.accepted_user_id == user.id


async def test_an_admin_invitation_provisions_an_administrator(db_session):
    email = f"{uuid4()}@example.com"
    await _invite(db_session, email=email, role="admin")

    user = await auth_service.resolve_user(db_session, _claims(email))

    assert user.role == "admin"


async def test_the_invitation_matches_the_address_case_insensitively(db_session):
    email = f"{uuid4()}@example.com"
    await _invite(db_session, email=email)

    user = await auth_service.resolve_user(db_session, _claims(email.upper()))

    assert user.email == email


async def test_a_second_sign_in_reuses_the_user_and_does_not_need_the_invitation(db_session):
    email = f"{uuid4()}@example.com"
    await _invite(db_session, email=email)
    claims = _claims(email)

    first = await auth_service.resolve_user(db_session, claims)
    second = await auth_service.resolve_user(db_session, claims)

    assert first.id == second.id


async def test_an_invitation_can_be_accepted_only_once(db_session):
    email = f"{uuid4()}@example.com"
    await _invite(db_session, email=email)
    await auth_service.resolve_user(db_session, _claims(email))

    # A different Clerk identity claiming the same address finds the invitation spent - and the
    # address already belongs to a user - so it is refused.
    with pytest.raises(NotInvitedError):
        await auth_service.resolve_user(db_session, _claims(email))


async def test_an_expired_invitation_does_not_provision(db_session):
    email = f"{uuid4()}@example.com"
    invitation = await _invite(db_session, email=email)
    await db_session.execute(
        text(
            "UPDATE invitations SET created_at = now() - interval '2 days', "
            "expires_at = now() - interval '1 day' WHERE id = :i"
        ),
        {"i": invitation.id},
    )

    with pytest.raises(NotInvitedError):
        await auth_service.resolve_user(db_session, _claims(email))
    assert not await _user_exists(db_session, email)


async def test_a_revoked_invitation_does_not_provision(db_session):
    email = f"{uuid4()}@example.com"
    invitation = await _invite(db_session, email=email)
    await invitations_repo.mark_revoked(db_session, invitation.id)

    with pytest.raises(NotInvitedError):
        await auth_service.resolve_user(db_session, _claims(email))


async def test_an_invitation_is_not_accepted_for_an_unverified_address(db_session):
    email = f"{uuid4()}@example.com"
    await _invite(db_session, email=email)

    with pytest.raises(NotInvitedError):
        await auth_service.resolve_user(db_session, _claims(email, verified=False))

    assert not await _user_exists(db_session, email)


async def test_a_role_claim_in_the_token_cannot_choose_or_raise_a_role(
    role_client, mint_token, db_session
):
    email = f"{uuid4()}@example.com"
    await _invite(db_session, email=email, role="employee")
    token = mint_token(
        email=email, role="admin", roles=["admin"], public_metadata={"role": "admin"}
    )

    me = await role_client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    projects = await role_client.get("/v1/projects", headers={"Authorization": f"Bearer {token}"})

    assert me.status_code == 200 and me.json()["role"] == "employee"
    assert projects.status_code == 403


async def test_an_existing_user_cannot_be_upgraded_by_a_later_invitation_or_token(
    role_client, make_person, mint_token, db_session
):
    employee = await make_person(role="employee")
    token = mint_token(sub=employee.clerk_user_id, email=employee.email, role="admin")

    me = await role_client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert me.json()["role"] == "employee"


# --- the first administrator -----------------------------------------------------------------


async def test_the_bootstrap_address_becomes_the_first_admin_when_none_exists(
    db_session, monkeypatch
):
    await suspend_every_existing_admin(db_session)
    email = f"{uuid4()}@example.com"
    _bootstrap(monkeypatch, f"someone@else.example, {email.upper()}")

    user = await auth_service.resolve_user(db_session, _claims(email))

    assert user.role == "admin"


async def test_the_bootstrap_address_is_ignored_once_an_active_admin_exists(
    db_session, monkeypatch
):
    await suspend_every_existing_admin(db_session)
    await users_repo.create(db_session, email=f"{uuid4()}@example.com", role="admin")
    email = f"{uuid4()}@example.com"
    _bootstrap(monkeypatch, email)

    with pytest.raises(NotInvitedError):
        await auth_service.resolve_user(db_session, _claims(email))


async def test_the_bootstrap_address_needs_a_verified_email(db_session, monkeypatch):
    await suspend_every_existing_admin(db_session)
    email = f"{uuid4()}@example.com"
    _bootstrap(monkeypatch, email)

    with pytest.raises(NotInvitedError):
        await auth_service.resolve_user(db_session, _claims(email, verified=False))


async def test_an_address_not_on_the_bootstrap_list_never_becomes_admin(db_session, monkeypatch):
    await suspend_every_existing_admin(db_session)
    _bootstrap(monkeypatch, "owner@company.example")

    with pytest.raises(NotInvitedError):
        await auth_service.resolve_user(db_session, _claims(f"{uuid4()}@example.com"))


async def test_with_nothing_configured_nobody_is_bootstrapped(db_session, monkeypatch):
    await suspend_every_existing_admin(db_session)
    _bootstrap(monkeypatch, "")

    with pytest.raises(NotInvitedError):
        await auth_service.resolve_user(db_session, _claims(f"{uuid4()}@example.com"))


# --- suspended accounts ----------------------------------------------------------------------


@pytest.mark.parametrize("status", ["suspended", "deleted"])
@pytest.mark.parametrize("role", ["admin", "employee"])
async def test_a_user_who_is_not_active_is_refused_on_every_request(
    role_client, make_person, status, role
):
    person = await make_person(role=role, status=status)

    me = await role_client.get("/v1/auth/me", headers=person.headers)
    projects = await role_client.get("/v1/projects", headers=person.headers)

    for response in (me, projects):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "account_inactive"


async def test_suspending_a_user_takes_effect_on_their_very_next_request(
    role_client, make_person, db_session
):
    person = await make_person(role="employee")
    assert (await role_client.get("/v1/auth/me", headers=person.headers)).status_code == 200

    await db_session.execute(
        text("UPDATE users SET status = 'suspended' WHERE id = :i"), {"i": person.user.id}
    )

    after = await role_client.get("/v1/auth/me", headers=person.headers)
    assert after.status_code == 403 and after.json()["error"]["code"] == "account_inactive"


async def test_a_suspended_user_is_refused_even_with_a_valid_token_and_an_open_invitation(
    role_client, make_person, mint_token, db_session
):
    """An existing user is found by identity first, so an invitation can never revive an account."""
    person = await make_person(role="employee", status="suspended")
    await _invite(db_session, email=person.email + ".other")

    response = await role_client.get("/v1/auth/me", headers=person.headers)

    assert response.status_code == 403
