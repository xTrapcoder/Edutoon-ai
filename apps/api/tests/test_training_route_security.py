# ruff: noqa: F811 - pytest fixtures imported from test_admin_training_api
"""Who may call the training routes, and what a caller who may not learns (Step 2, Piece 3).

* 403 only where the answer cannot depend on whether a particular resource exists - a collection
  route, or a route that has no resource to name;
* the identical 404 wherever the route names a course or a module - **wherever in the path** the
  name sits, so the nested collections (``/courses/{id}/modules``, ``/courses/{id}/enrolments``)
  tell an employee nothing about which courses exist;
* an employee can mutate nothing, and cannot ask for anyone else's data.
"""

from __future__ import annotations

import pytest

from edutoon.main import create_app
from tests.test_admin_training_api import (  # noqa: F401 - fixtures used by name
    BASE,
    NIL_ID,
    admin,
    h,
    make_course,
    source,
    storage,
    track,
)
from tests.test_route_policies import api_routes, policies_of
from tests.training_api_helpers import (
    EMPLOYEE_BASE,
    comparable,
    enrol,
    enrolment_rows,
    published_course,
    withdraw,
)

pytestmark = pytest.mark.real_auth


@pytest.fixture
async def employee(make_person):
    return await make_person(role="employee")


# --- employees against the administrator routes ----------------------------------------------


async def test_the_nested_course_collections_answer_an_employee_like_the_course_itself(
    role_client, admin, employee
):
    """Regression for the review finding: ``GET /courses/{id}/modules`` does not end in an
    identifier, but its answer would depend on whether that course exists - so an employee must get
    the one fixed 404 for a real course and for a made-up one alike, byte for byte, and the same
    bytes an administrator gets for a course that is missing."""
    real = await make_course(role_client, admin, "Secret course")

    for template in ("/courses/{}", "/courses/{}/modules", "/courses/{}/enrolments"):
        employee_real = await role_client.get(
            BASE + template.format(real["id"]), headers=employee.headers
        )
        employee_missing = await role_client.get(
            BASE + template.format(NIL_ID), headers=employee.headers
        )
        admin_missing = await role_client.get(BASE + template.format(NIL_ID), headers=admin.headers)

        assert employee_real.status_code == 404, template
        assert comparable(employee_real) == comparable(employee_missing), template
        assert employee_real.content == admin_missing.content, template
        assert employee_real.json() == {
            "error": {"code": "not_found", "message": "Course not found."}
        }
        assert "Secret course" not in employee_real.text


async def test_an_employee_gets_403_on_the_collection_routes_whether_or_not_the_user_exists(
    role_client, admin, employee, make_person
):
    other = await make_person(role="employee")

    answers = [
        await role_client.get(
            f"{BASE}/enrolments?user_id={employee.user.id}", headers=employee.headers
        ),
        await role_client.get(
            f"{BASE}/enrolments?user_id={other.user.id}", headers=employee.headers
        ),
        await role_client.get(f"{BASE}/enrolments?user_id={NIL_ID}", headers=employee.headers),
        await role_client.get(f"{BASE}/courses", headers=employee.headers),
    ]

    assert [a.status_code for a in answers] == [403, 403, 403, 403]
    # The three enrolment-listing answers are identical, so nothing says who exists.
    assert comparable(answers[0]) == comparable(answers[1]) == comparable(answers[2])
    assert answers[0].json()["error"]["code"] == "forbidden"


async def test_an_employee_cannot_enrol_withdraw_or_list_anyone_and_nothing_changes(
    role_client, admin, employee, make_person, db_session
):
    course = await make_course(role_client, admin)
    victim = await make_person(role="employee")
    created = await enrol(role_client, admin, course["id"], victim.user.id)
    assert created.status_code == 201
    before = await enrolment_rows(db_session, course["id"], victim.user.id)

    enrol_self = await enrol(role_client, employee, course["id"], employee.user.id)
    enrol_other = await enrol(role_client, employee, course["id"], victim.user.id)
    withdraw_other = await withdraw(role_client, employee, course["id"], victim.user.id)
    list_course = await role_client.get(
        f"{BASE}/courses/{course['id']}/enrolments", headers=employee.headers
    )
    list_user = await role_client.get(
        f"{BASE}/enrolments?user_id={victim.user.id}", headers=employee.headers
    )

    assert [r.status_code for r in (enrol_self, enrol_other, withdraw_other, list_course)] == [
        404
    ] * 4
    assert list_user.status_code == 403
    assert await enrolment_rows(db_session, course["id"], employee.user.id) == []
    assert await enrolment_rows(db_session, course["id"], victim.user.id) == before


async def test_every_employee_route_refuses_the_wrong_people(
    role_app, role_client, admin, make_person, mint_token
):
    """The administrator is refused on ``/training/courses`` (403) and gets the standard module
    404 on the stream; a suspended employee, an uninvited person and an anonymous caller are
    refused before any role is looked at (403 / 403 / 401)."""
    suspended = await make_person(role="employee", status="suspended")
    uninvited = {"Authorization": f"Bearer {mint_token()}"}
    employee_routes = [
        (method, endpoint, next(iter(policies_of(endpoint))))
        for method, endpoint in api_routes(role_app)
        if policies_of(endpoint) & {"employee", "employee_module"}
    ]
    assert len(employee_routes) == 2

    for method, endpoint, policy in employee_routes:
        url = endpoint.path.replace("{module_id}", NIL_ID)
        wrong_role = (403, "forbidden") if policy == "employee" else (404, "not_found")
        for who, headers, status, code in (
            ("administrator", admin.headers, *wrong_role),
            ("suspended employee", suspended.headers, 403, "account_inactive"),
            ("uninvited", uninvited, 403, "not_invited"),
            ("anonymous", {}, 401, "unauthorized"),
        ):
            response = await role_client.request(method, url, headers=headers)
            got = (response.status_code, response.json()["error"]["code"])
            assert got == (status, code), f"{who}: {method} {url} -> {got}"


async def test_the_course_listing_is_always_the_callers_own_whatever_the_query_says(
    role_client, admin, make_person, source, track, db_session, storage
):
    course, _ = await published_course(role_client, admin, source, track, db_session, storage)
    owner = await make_person(role="employee")
    snoop = await make_person(role="employee")
    await enrol(role_client, admin, course["id"], owner.user.id)

    own = await role_client.get(f"{EMPLOYEE_BASE}/courses", headers=owner.headers)
    snooping = await role_client.get(
        f"{EMPLOYEE_BASE}/courses?user_id={owner.user.id}", headers=snoop.headers
    )

    assert [c["id"] for c in own.json()["items"]] == [course["id"]]
    assert snooping.status_code == 200 and snooping.json() == {"items": []}


async def test_an_employee_cannot_use_the_administrators_preview_of_a_real_module(
    role_client, admin, employee, make_person, source, track, db_session, storage
):
    course, modules = await published_course(
        role_client, admin, source, track, db_session, storage, modules=1
    )
    await enrol(role_client, admin, course["id"], employee.user.id)
    preview = f"{BASE}/modules/{modules[0]['id']}/video"
    missing = f"{BASE}/modules/{NIL_ID}/video"

    real = await role_client.get(preview, headers=employee.headers)
    absent = await role_client.get(missing, headers=employee.headers)

    assert real.status_code == 404 and comparable(real) == comparable(absent)


# Pinned in Piece 3 as "no assessment, progress or pass-gate route exists"; since Step 3 it is the
# complete table of the routes in the training namespaces that carry one of those words, with the
# role policy (the scope guard) each must have. A route added, removed or re-gated changes this
# table in the same commit - and a progress / quiz / score / pass route still fails here.
SCOPE_WORDS = ("assessment", "attempt", "progress", "quiz", "question", "score", "pass")
SCOPE_GUARDED_ROUTES = {
    ("PUT", "/v1/admin/training/modules/{module_id}/assessment"): "admin_module",
    ("GET", "/v1/admin/training/modules/{module_id}/assessment"): "admin_module",
    ("DELETE", "/v1/admin/training/modules/{module_id}/assessment"): "admin_module",
    ("GET", "/v1/training/modules/{module_id}/assessment"): "employee_assessment",
    ("GET", "/v1/training/modules/{module_id}/assessment/attempts"): "employee_assessment",
    ("POST", "/v1/training/modules/{module_id}/assessment/attempts"): "employee_assessment",
}


def test_the_assessment_scope_routes_are_exactly_the_pinned_table_with_their_guards():
    """Scoped to the training namespaces (the project render API has an unrelated
    ``/render/progress``). Every route in them that mentions assessments, attempts, progress,
    quizzes, questions, scores or a pass gate is in the table, with exactly its policy; every
    employee-facing one is guarded by the assessment 404 policy, every admin one by the
    module 404 policy, and no other route in the namespaces carries such a word."""
    found = {
        (method, endpoint.path): next(iter(policies_of(endpoint)))
        for method, endpoint in api_routes(create_app())
        if endpoint.path.startswith(("/v1/training", "/v1/admin/training"))
        and any(word in endpoint.path.lower() for word in SCOPE_WORDS)
    }
    assert len(found) >= 1  # the scan found them at all
    assert found == SCOPE_GUARDED_ROUTES
    for (_, path), policy in found.items():
        employee_side = path.startswith("/v1/training")
        assert policy == ("employee_assessment" if employee_side else "admin_module")
