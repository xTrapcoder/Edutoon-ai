"""``/v1/admin`` - invitations and user access. Administrators only; the role is the database's.

Clerk is never contacted: ``providers.clerk.send_invitation`` is replaced where a test needs the
"sent" or "failed" outcome, and otherwise invitations are "not configured". Whether Clerk really
sends the email is NOT verified here (the Clerk keys are placeholders).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from edutoon.providers import clerk
from tests.role_fixtures import idem, suspend_every_existing_admin

pytestmark = pytest.mark.real_auth


@pytest.fixture(autouse=True)
def _clerk_not_configured(monkeypatch):
    """Whatever the developer's .env holds, no test talks to Clerk unless it opts in."""
    monkeypatch.setattr(clerk, "invitations_configured", lambda: False)


async def _invite(client, admin, **body):
    payload = {"email": f"{uuid4()}@example.com", **body}
    return await client.post(
        "/v1/admin/invitations", json=payload, headers={**admin.headers, **idem()}
    )


# --- creating invitations --------------------------------------------------------------------


async def test_an_admin_creates_an_invitation_that_defaults_to_employee(role_client, make_person):
    admin = await make_person(role="admin")
    email = f"  {uuid4()}@Example.COM "

    response = await _invite(role_client, admin, email=email, display_name="  Asha Rao ")

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == email.strip().lower()
    assert (body["role"], body["status"], body["delivery"]) == (
        "employee",
        "pending",
        "not_configured",
    )
    assert body["display_name"] == "Asha Rao"
    assert "clerk_invitation_id" not in body


async def test_an_admin_can_invite_another_administrator(role_client, make_person):
    admin = await make_person(role="admin")

    response = await _invite(role_client, admin, role="admin")

    assert response.status_code == 201 and response.json()["role"] == "admin"


async def test_the_invited_person_can_then_sign_in_with_the_invited_role(
    role_client, make_person, mint_token
):
    admin = await make_person(role="admin")
    email = f"{uuid4()}@example.com"
    await _invite(role_client, admin, email=email)

    token = mint_token(email=email)
    me = await role_client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert me.status_code == 200 and me.json()["role"] == "employee"
    listed = await role_client.get("/v1/admin/invitations?status=accepted", headers=admin.headers)
    assert email in [item["email"] for item in listed.json()["items"]]


async def test_an_employee_cannot_create_an_invitation(role_client, make_person):
    employee = await make_person(role="employee")

    response = await _invite(role_client, employee)

    assert response.status_code == 403 and response.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize(
    "extra",
    [{"is_admin": True}, {"status": "accepted"}, {"expires_at": "2099-01-01T00:00:00Z"}],
)
async def test_unknown_fields_are_rejected_with_422(role_client, make_person, extra):
    admin = await make_person(role="admin")

    response = await _invite(role_client, admin, **extra)

    assert response.status_code == 422


@pytest.mark.parametrize(
    "email", ["", "no-at-sign", "@nolocal.com", "a b@c.com", "x" * 400 + "@a.co"]
)
async def test_a_malformed_email_is_a_422(role_client, make_person, email):
    admin = await make_person(role="admin")

    response = await _invite(role_client, admin, email=email)

    assert response.status_code == 422


async def test_an_unknown_role_is_a_422(role_client, make_person):
    admin = await make_person(role="admin")

    response = await _invite(role_client, admin, role="superuser")

    assert response.status_code == 422


async def test_creating_requires_an_idempotency_key(role_client, make_person):
    admin = await make_person(role="admin")

    response = await role_client.post(
        "/v1/admin/invitations", json={"email": f"{uuid4()}@example.com"}, headers=admin.headers
    )

    assert response.status_code == 422


async def test_the_same_idempotency_key_replays_instead_of_inviting_twice(
    role_client, make_person, db_session
):
    admin = await make_person(role="admin")
    email = f"{uuid4()}@example.com"
    headers = {**admin.headers, **idem()}

    first = await role_client.post("/v1/admin/invitations", json={"email": email}, headers=headers)
    second = await role_client.post("/v1/admin/invitations", json={"email": email}, headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    count = (
        await db_session.execute(
            text("SELECT count(*) FROM invitations WHERE email = :e"), {"e": email}
        )
    ).scalar_one()
    assert count == 1


async def test_a_second_pending_invitation_for_the_same_address_is_a_409(role_client, make_person):
    admin = await make_person(role="admin")
    email = f"{uuid4()}@example.com"
    await _invite(role_client, admin, email=email)

    response = await _invite(role_client, admin, email=email)

    assert response.status_code == 409


async def test_inviting_an_address_that_already_has_an_account_is_a_409(role_client, make_person):
    admin = await make_person(role="admin")
    employee = await make_person(role="employee")

    response = await _invite(role_client, admin, email=employee.email)

    assert response.status_code == 409


async def test_an_expired_invitation_can_be_replaced(role_client, make_person, db_session):
    admin = await make_person(role="admin")
    email = f"{uuid4()}@example.com"
    first = (await _invite(role_client, admin, email=email)).json()
    await db_session.execute(
        text(
            "UPDATE invitations SET created_at = now() - interval '2 days', "
            "expires_at = now() - interval '1 day' WHERE id = :i"
        ),
        {"i": first["id"]},
    )

    again = await _invite(role_client, admin, email=email)

    assert again.status_code == 201 and again.json()["id"] != first["id"]


# --- delivery through Clerk (replaced) -------------------------------------------------------


async def test_when_clerk_sends_it_the_invitation_is_marked_sent_and_carries_no_role(
    role_client, make_person, monkeypatch, db_session
):
    sent: list[dict[str, object]] = []

    async def fake_send(**kwargs):
        sent.append(kwargs)
        return "inv_clerk_123"

    monkeypatch.setattr(clerk, "invitations_configured", lambda: True)
    monkeypatch.setattr(clerk, "send_invitation", fake_send)
    admin = await make_person(role="admin")

    response = await _invite(role_client, admin, role="admin")

    assert response.json()["delivery"] == "sent"
    assert len(sent) == 1 and set(sent[0]) == {"email"}  # nothing else - above all, no role
    stored = (
        await db_session.execute(
            text("SELECT clerk_invitation_id FROM invitations WHERE id = :i"),
            {"i": response.json()["id"]},
        )
    ).scalar_one()
    assert stored == "inv_clerk_123"


async def test_when_clerk_fails_the_invitation_still_exists_and_says_so(
    role_client, make_person, monkeypatch, db_session
):
    async def failing_send(**kwargs):
        raise clerk.ClerkInvitationError("Clerk could not be reached to send the invitation.")

    monkeypatch.setattr(clerk, "invitations_configured", lambda: True)
    monkeypatch.setattr(clerk, "send_invitation", failing_send)
    admin = await make_person(role="admin")

    response = await _invite(role_client, admin)

    assert response.status_code == 201 and response.json()["delivery"] == "failed"
    assert response.json()["status"] == "pending"


# --- listing and revoking --------------------------------------------------------------------


async def test_invitations_can_be_listed_and_filtered_by_status(role_client, make_person):
    admin = await make_person(role="admin")
    pending = (await _invite(role_client, admin)).json()
    revoked = (await _invite(role_client, admin)).json()
    await role_client.post(
        f"/v1/admin/invitations/{revoked['id']}/revoke", headers={**admin.headers, **idem()}
    )

    all_items = (await role_client.get("/v1/admin/invitations", headers=admin.headers)).json()[
        "items"
    ]
    only_pending = (
        await role_client.get("/v1/admin/invitations?status=pending", headers=admin.headers)
    ).json()["items"]

    assert {pending["id"], revoked["id"]} <= {item["id"] for item in all_items}
    assert pending["id"] in {item["id"] for item in only_pending}
    assert revoked["id"] not in {item["id"] for item in only_pending}


async def test_revoking_blocks_sign_in_and_cannot_be_done_twice(
    role_client, make_person, mint_token
):
    admin = await make_person(role="admin")
    email = f"{uuid4()}@example.com"
    invitation = (await _invite(role_client, admin, email=email)).json()

    revoke = await role_client.post(
        f"/v1/admin/invitations/{invitation['id']}/revoke", headers={**admin.headers, **idem()}
    )
    again = await role_client.post(
        f"/v1/admin/invitations/{invitation['id']}/revoke", headers={**admin.headers, **idem()}
    )
    sign_in = await role_client.get(
        "/v1/auth/me", headers={"Authorization": f"Bearer {mint_token(email=email)}"}
    )

    assert revoke.status_code == 200 and revoke.json()["status"] == "revoked"
    assert again.status_code == 409
    assert sign_in.status_code == 403 and sign_in.json()["error"]["code"] == "not_invited"


async def test_revoking_an_unknown_invitation_is_a_404(role_client, make_person):
    admin = await make_person(role="admin")

    response = await role_client.post(
        f"/v1/admin/invitations/{uuid4()}/revoke", headers={**admin.headers, **idem()}
    )

    assert response.status_code == 404


async def test_an_employee_cannot_list_or_revoke(role_client, make_person):
    employee = await make_person(role="employee")

    listing = await role_client.get("/v1/admin/invitations", headers=employee.headers)
    users = await role_client.get("/v1/admin/users", headers=employee.headers)
    revoke = await role_client.post(
        f"/v1/admin/invitations/{uuid4()}/revoke", headers={**employee.headers, **idem()}
    )

    assert listing.status_code == users.status_code == revoke.status_code == 403


# --- users: listing, role and status ---------------------------------------------------------


async def test_an_admin_lists_users_with_their_roles_and_no_secrets(role_client, make_person):
    admin = await make_person(role="admin")
    employee = await make_person(role="employee")

    response = await role_client.get("/v1/admin/users?limit=100", headers=admin.headers)

    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()["items"]}
    assert by_id[str(employee.user.id)]["role"] == "employee"
    assert by_id[str(employee.user.id)]["has_signed_in"] is True
    assert "clerk_user_id" not in by_id[str(employee.user.id)]


async def test_an_admin_changes_a_role_and_it_applies_on_the_next_request(
    role_client, make_person, db_session
):
    admin = await make_person(role="admin")
    employee = await make_person(role="employee")
    assert (await role_client.get("/v1/projects", headers=employee.headers)).status_code == 403

    response = await role_client.patch(
        f"/v1/admin/users/{employee.user.id}",
        json={"role": "admin"},
        headers={**admin.headers, **idem()},
    )

    assert response.status_code == 200 and response.json()["role"] == "admin"
    assert (await role_client.get("/v1/projects", headers=employee.headers)).status_code == 200
    audit = (
        await db_session.execute(
            text(
                "SELECT context FROM audit_logs WHERE action = 'user.role_changed' "
                "AND entity_id = :i"
            ),
            {"i": employee.user.id},
        )
    ).scalar_one()
    assert audit == {"from": "employee", "to": "admin"}


async def test_suspending_through_the_api_locks_the_user_out_at_once(role_client, make_person):
    admin = await make_person(role="admin")
    employee = await make_person(role="employee")

    response = await role_client.patch(
        f"/v1/admin/users/{employee.user.id}",
        json={"status": "suspended"},
        headers={**admin.headers, **idem()},
    )
    after = await role_client.get("/v1/auth/me", headers=employee.headers)

    assert response.status_code == 200 and response.json()["status"] == "suspended"
    assert after.status_code == 403 and after.json()["error"]["code"] == "account_inactive"

    restore = await role_client.patch(
        f"/v1/admin/users/{employee.user.id}",
        json={"status": "active"},
        headers={**admin.headers, **idem()},
    )
    assert restore.status_code == 200
    assert (await role_client.get("/v1/auth/me", headers=employee.headers)).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"role": "owner"},
        {"status": "deleted"},
        {"role": "admin", "email": "x@y.co"},
        {"id": "x"},
    ],
)
async def test_a_bad_user_update_is_a_422(role_client, make_person, body):
    admin = await make_person(role="admin")
    employee = await make_person(role="employee")

    response = await role_client.patch(
        f"/v1/admin/users/{employee.user.id}", json=body, headers={**admin.headers, **idem()}
    )

    assert response.status_code == 422


async def test_updating_an_unknown_user_is_a_404(role_client, make_person):
    admin = await make_person(role="admin")

    response = await role_client.patch(
        f"/v1/admin/users/{uuid4()}", json={"role": "admin"}, headers={**admin.headers, **idem()}
    )

    assert response.status_code == 404


async def test_an_employee_cannot_change_anyones_role_including_their_own(role_client, make_person):
    employee = await make_person(role="employee")

    response = await role_client.patch(
        f"/v1/admin/users/{employee.user.id}",
        json={"role": "admin"},
        headers={**employee.headers, **idem()},
    )

    assert response.status_code == 403
    assert (await role_client.get("/v1/auth/me", headers=employee.headers)).json()[
        "role"
    ] == "employee"


async def test_the_last_active_administrator_cannot_be_demoted_or_suspended(
    role_client, make_person, db_session
):
    await suspend_every_existing_admin(db_session)
    only_admin = await make_person(role="admin")

    demote = await role_client.patch(
        f"/v1/admin/users/{only_admin.user.id}",
        json={"role": "employee"},
        headers={**only_admin.headers, **idem()},
    )
    suspend = await role_client.patch(
        f"/v1/admin/users/{only_admin.user.id}",
        json={"status": "suspended"},
        headers={**only_admin.headers, **idem()},
    )

    assert demote.status_code == suspend.status_code == 409
    assert (await role_client.get("/v1/auth/me", headers=only_admin.headers)).json()[
        "role"
    ] == "admin"


async def test_an_administrator_can_be_demoted_when_another_active_one_remains(
    role_client, make_person, db_session
):
    await suspend_every_existing_admin(db_session)
    first = await make_person(role="admin")
    second = await make_person(role="admin")

    response = await role_client.patch(
        f"/v1/admin/users/{first.user.id}",
        json={"role": "employee"},
        headers={**second.headers, **idem()},
    )

    assert response.status_code == 200 and response.json()["role"] == "employee"


async def test_setting_what_is_already_set_changes_nothing(role_client, make_person, db_session):
    admin = await make_person(role="admin")
    employee = await make_person(role="employee")

    response = await role_client.patch(
        f"/v1/admin/users/{employee.user.id}",
        json={"role": "employee", "status": "active"},
        headers={**admin.headers, **idem()},
    )

    assert response.status_code == 200
    audit_rows = (
        await db_session.execute(
            text("SELECT count(*) FROM audit_logs WHERE entity_id = :i AND action LIKE 'user.%'"),
            {"i": employee.user.id},
        )
    ).scalar_one()
    assert audit_rows == 0


async def test_a_deleted_account_cannot_be_revived(role_client, make_person):
    admin = await make_person(role="admin")
    gone = await make_person(role="employee", status="deleted")

    response = await role_client.patch(
        f"/v1/admin/users/{gone.user.id}",
        json={"status": "active"},
        headers={**admin.headers, **idem()},
    )

    assert response.status_code == 409
