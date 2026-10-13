"""The conftest.py shim for tests written before roles existed replaces *provisioning* only.

This module deliberately does NOT carry ``real_auth``: it runs under the shim, and proves the
suspended-account check in ``get_current_user`` still applies there, so no legacy test can
accidentally authenticate a suspended user.
"""

from __future__ import annotations


async def test_a_suspended_user_is_refused_under_the_legacy_shim(role_client, make_person):
    person = await make_person(role="admin", status="suspended")

    response = await role_client.get("/v1/auth/me", headers=person.headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "account_inactive"


async def test_the_shim_defaults_a_legacy_user_to_admin_but_an_explicit_role_wins(
    role_client, make_person
):
    employee = await make_person(role="employee")

    assert (await role_client.get("/v1/projects", headers=employee.headers)).status_code == 403
