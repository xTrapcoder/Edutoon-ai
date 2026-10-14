"""Every route declares its role policy, and the policy is actually enforced.

Two layers, both against the real application:

1. **Introspection.** Walk every route the app serves and require that it carries exactly one
   policy dependency from ``edutoon.api.policies`` (``public`` / ``authenticated`` / ``admin`` /
   ``employee``). A route added without one - or with two - fails this test, so "I forgot to say
   who may call it" cannot reach a release. The checker is itself tested on a synthetic app.
2. **Behaviour.** Call every ``admin`` route as an employee, a suspended administrator, an
   uninvited person and an anonymous caller, and require 403 / 403 / 403 / 401, with no user row
   created for the uninvited person.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute
from sqlalchemy import text

from edutoon.api.policies import POLICY_NAMES, require_admin
from edutoon.main import create_app

pytestmark = pytest.mark.real_auth

# Starlette's own documentation routes (not APIRoute); nothing else may be served outside APIRoute.
FRAMEWORK_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

# Routes whose policy is not in the admin family. Everything else must be in it - and every route
# of the employee area is listed here by name, deliberately, in the change that adds it.
EXPECTED_NON_ADMIN = {
    ("GET", "/health"): "public",
    ("GET", "/v1/auth/me"): "authenticated",
    ("GET", "/v1/training/courses"): "employee",
    ("GET", "/v1/training/modules/{module_id}/video"): "employee_module",
}

# Which policy a route that names a resource must carry, whatever the resource's *position* in the
# path. A nested collection such as ``/courses/{course_id}/modules`` does not end in an identifier,
# yet its answer depends on whether that course exists, so it must answer an employee with the same
# 404 as the course route itself - never a 403 that would tell them the route is real but the
# course is not theirs. Order matters: the first parameter present wins.
RESOURCE_POLICY_BY_PATH_PARAMETER = (
    ("{project_id}", "admin_project"),
    ("{course_id}", "admin_course"),
    ("{module_id}", "admin_module"),
)


@dataclass(frozen=True)
class Endpoint:
    path: str
    methods: frozenset[str]
    dependant: object


def flatten(app: FastAPI) -> tuple[list[Endpoint], list[str]]:
    """Every endpoint the app serves, and a description of anything that is not an APIRoute.

    FastAPI (0.141) keeps routers added with ``include_router`` as lazy ``_IncludedRouter`` objects
    whose routes are listed by ``effective_route_contexts()``. Anything this helper does not
    recognise is *reported* rather than skipped, so a framework change fails the test loudly
    instead of letting routes go unchecked.
    """
    endpoints: list[Endpoint] = []
    unknown: list[str] = []

    def add(item) -> None:
        methods = frozenset(item.methods - {"HEAD", "OPTIONS"})
        endpoints.append(Endpoint(item.path, methods, item.dependant))

    for route in app.routes:
        if isinstance(route, APIRoute):
            add(route)
        elif type(route).__name__ == "_IncludedRouter":
            for context in route.effective_route_contexts():
                if isinstance(context.original_route, APIRoute):
                    add(context)
                else:
                    unknown.append(f"{context.path or context.frontend_prefix!r} (in a router)")
        elif getattr(route, "path", None) in FRAMEWORK_PATHS:
            continue
        else:
            unknown.append(f"{getattr(route, 'path', repr(route))} ({type(route).__name__})")
    return endpoints, unknown


def policies_of(endpoint: Endpoint) -> set[str]:
    found: set[str] = set()

    def walk(dependant) -> None:
        if dependant.call in POLICY_NAMES:
            found.add(POLICY_NAMES[dependant.call])
        for sub in dependant.dependencies:
            walk(sub)

    walk(endpoint.dependant)
    return found


def policy_violations(app: FastAPI) -> list[str]:
    """Human-readable problems with the app's routes (empty when every route is accounted for)."""
    endpoints, unknown = flatten(app)
    problems = [
        f"{item}: served outside APIRoute and not a known framework route" for item in unknown
    ]
    for endpoint in endpoints:
        found = policies_of(endpoint)
        label = f"{sorted(endpoint.methods)} {endpoint.path}"
        if not found:
            problems.append(f"{label}: declares no role policy")
        elif len(found) > 1:
            problems.append(f"{label}: declares several role policies {sorted(found)}")
    return problems


def api_routes(app: FastAPI) -> list[tuple[str, Endpoint]]:
    endpoints, _ = flatten(app)
    return [(method, endpoint) for endpoint in endpoints for method in sorted(endpoint.methods)]


# --- layer 1: introspection ------------------------------------------------------------------


def test_every_route_of_the_real_app_declares_exactly_one_role_policy():
    assert policy_violations(create_app()) == []


def test_the_checker_catches_a_route_added_without_a_policy():
    app = FastAPI()
    router = APIRouter(prefix="/v1")

    @router.get("/forgotten")
    async def forgotten() -> dict[str, str]:
        return {}

    app.include_router(router)

    problems = policy_violations(app)

    assert len(problems) == 1
    assert "/v1/forgotten" in problems[0] and "no role policy" in problems[0]


def test_the_checker_catches_a_route_with_two_policies_and_accepts_one():
    from edutoon.api.policies import require_employee

    app = FastAPI()
    both = APIRouter(prefix="/v1", dependencies=[Depends(require_admin), Depends(require_employee)])
    one = APIRouter(prefix="/v1", dependencies=[Depends(require_admin)])

    @both.get("/both")
    async def both_route() -> dict[str, str]:
        return {}

    @one.get("/one")
    async def one_route() -> dict[str, str]:
        return {}

    app.include_router(both)
    app.include_router(one)

    problems = policy_violations(app)

    assert len(problems) == 1 and "/v1/both" in problems[0] and "several" in problems[0]


def test_a_route_mounted_outside_apiroute_is_flagged():
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    app = FastAPI()
    app.router.routes.append(Route("/sneaky", lambda request: PlainTextResponse("x")))

    assert any("/sneaky" in problem for problem in policy_violations(app))


def _expected_policy(method: str, path: str) -> str:
    if (method, path) in EXPECTED_NON_ADMIN:
        return EXPECTED_NON_ADMIN[(method, path)]
    # A route that names a project, course or module *anywhere in its path* answers a
    # non-administrator 404; every other admin route 403.
    for parameter, policy in RESOURCE_POLICY_BY_PATH_PARAMETER:
        if parameter in path:
            return policy
    return "admin"


def test_the_policy_of_every_route_is_the_expected_one():
    """Pins the whole map: /health is public, /v1/auth/me is any active user, every route naming
    a project is admin-with-404, and every other route is admin-with-403."""
    actual = {
        (method, endpoint.path): next(iter(policies_of(endpoint)))
        for method, endpoint in api_routes(create_app())
    }
    expected = {key: _expected_policy(*key) for key in actual}

    assert actual == expected
    assert sum(1 for policy in actual.values() if policy == "admin_project") >= 30
    assert sum(1 for policy in actual.values() if policy == "admin") >= 5


def test_the_project_collection_routes_are_403_and_the_project_id_routes_are_404():
    actual = {
        (method, endpoint.path): next(iter(policies_of(endpoint)))
        for method, endpoint in api_routes(create_app())
    }

    assert actual[("GET", "/v1/projects")] == "admin"
    assert actual[("POST", "/v1/projects")] == "admin"
    for method in ("GET", "PATCH", "DELETE"):
        assert actual[(method, "/v1/projects/{project_id}")] == "admin_project"
    assert actual[("GET", "/v1/admin/users")] == "admin"  # no project id: 403 for an employee


# --- layer 2: behaviour ----------------------------------------------------------------------

NIL_ID = "00000000-0000-4000-8000-000000000000"


def _concrete(path: str, project_id: str = NIL_ID) -> str:
    return re.sub(r"\{[^}]+\}", NIL_ID, path.replace("{project_id}", project_id))


async def _users_with_email(db_session, email: str) -> int:
    """Accounts with this one address - scoped to the test's own identity, so users that other
    tests or the running application hold in the shared database cannot disturb the assertion."""
    return (
        await db_session.execute(
            text("SELECT count(*) FROM users WHERE lower(email) = lower(:email)"),
            {"email": email},
        )
    ).scalar_one()


ADMIN_FAMILY = {"admin", "admin_project", "admin_course", "admin_module"}
NOT_FOUND_POLICIES = {"admin_project", "admin_course", "admin_module"}


def _admin_family_routes(role_app):
    return [
        (method, endpoint, next(iter(policies_of(endpoint))))
        for method, endpoint in api_routes(role_app)
        if policies_of(endpoint) & ADMIN_FAMILY
    ]


async def test_every_admin_route_refuses_everyone_who_is_not_an_active_administrator(
    role_app, role_client, make_person, mint_token, db_session
):
    """Employee: 403 ``forbidden`` on a route with no project id, 404 ``not_found`` on a route that
    names one. Suspended administrator, uninvited person, anonymous caller: 403 / 403 / 401 on both
    (they are refused before any role or project is looked at)."""
    employee = await make_person(role="employee")
    suspended_admin = await make_person(role="admin", status="suspended")
    uninvited_email = f"uninvited-{uuid4().hex}@example.com"
    uninvited = {"Authorization": f"Bearer {mint_token(email=uninvited_email)}"}
    assert await _users_with_email(db_session, uninvited_email) == 0

    routes = _admin_family_routes(role_app)
    assert len(routes) >= 50

    failures: list[str] = []
    for method, endpoint, policy in routes:
        url = _concrete(endpoint.path)
        employee_wants = (403, "forbidden") if policy == "admin" else (404, "not_found")
        for who, headers, status, code in (
            ("employee", employee.headers, *employee_wants),
            ("suspended admin", suspended_admin.headers, 403, "account_inactive"),
            ("uninvited", uninvited, 403, "not_invited"),
            ("anonymous", {}, 401, "unauthorized"),
        ):
            response = await role_client.request(method, url, headers=headers)
            body = response.json() if response.content else {}
            got = (response.status_code, body.get("error", {}).get("code"))
            if got != (status, code):
                failures.append(f"{who}: {method} {url} -> {got}, wanted {(status, code)}")

    assert failures == []
    # The uninvited person's many attempts created no account.
    assert await _users_with_email(db_session, uninvited_email) == 0


async def test_an_employee_gets_403_on_the_project_list_and_create_routes(role_client, make_person):
    employee = await make_person(role="employee")

    listing = await role_client.get("/v1/projects", headers=employee.headers)
    create = await role_client.post(
        "/v1/projects",
        json={"title": "Sneaky", "source_type": "pdf"},
        headers={**employee.headers, "Idempotency-Key": "employee-create-1"},
    )

    for response in (listing, create):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"


async def test_an_employee_gets_404_on_a_project_id_route_and_it_reveals_nothing(
    role_app, role_client, make_person
):
    """The same status and body (apart from the id echoed back) whether the project exists, belongs
    to an administrator, or does not exist at all - on every route that names a project."""
    admin = await make_person(role="admin")
    employee = await make_person(role="employee")
    created = await role_client.post(
        "/v1/projects",
        json={"title": "Secret plan", "source_type": "pdf"},
        headers={**admin.headers, "Idempotency-Key": "admin-create-1"},
    )
    assert created.status_code == 201
    real_id = created.json()["id"]

    def normalised(response, project_id: str):
        return response.status_code, response.text.replace(project_id, "<project>")

    differences: list[str] = []
    checked = 0
    for method, endpoint, policy in _admin_family_routes(role_app):
        if policy != "admin_project":
            continue
        existing = await role_client.request(
            method, _concrete(endpoint.path, real_id), headers=employee.headers
        )
        absent = await role_client.request(
            method, _concrete(endpoint.path, NIL_ID), headers=employee.headers
        )
        checked += 1
        got_existing, got_absent = normalised(existing, real_id), normalised(absent, NIL_ID)
        if got_existing != got_absent or got_existing[0] != 404:
            differences.append(f"{method} {endpoint.path}: {got_existing} vs {got_absent}")
        assert "Secret plan" not in existing.text

    assert checked >= 30
    assert differences == []

    # What the employee sent changed nothing: the project is still there, untouched.
    still = await role_client.get(f"/v1/projects/{real_id}", headers=admin.headers)
    assert still.status_code == 200 and still.json()["title"] == "Secret plan"


async def test_the_employees_404_is_the_one_an_administrator_gets_for_a_missing_project(
    role_client, make_person
):
    employee = await make_person(role="employee")
    admin = await make_person(role="admin")

    from_employee = await role_client.get(f"/v1/projects/{NIL_ID}", headers=employee.headers)
    from_admin = await role_client.get(f"/v1/projects/{NIL_ID}", headers=admin.headers)

    assert from_employee.status_code == from_admin.status_code == 404
    assert from_employee.json() == from_admin.json()


async def test_an_employee_cannot_patch_or_delete_a_project_and_it_survives(
    role_client, make_person
):
    admin = await make_person(role="admin")
    employee = await make_person(role="employee")
    created = await role_client.post(
        "/v1/projects",
        json={"title": "Keep me", "source_type": "pdf"},
        headers={**admin.headers, "Idempotency-Key": "admin-create-2"},
    )
    project_id = created.json()["id"]

    patch = await role_client.patch(
        f"/v1/projects/{project_id}",
        json={"title": "Hijacked"},
        headers={**employee.headers, "Idempotency-Key": "employee-patch-1"},
    )
    delete = await role_client.delete(
        f"/v1/projects/{project_id}",
        headers={**employee.headers, "Idempotency-Key": "employee-delete-1"},
    )
    after = await role_client.get(f"/v1/projects/{project_id}", headers=admin.headers)

    assert patch.status_code == delete.status_code == 404
    assert after.status_code == 200 and after.json()["title"] == "Keep me"


async def test_a_malformed_project_id_is_the_same_422_for_everyone(role_client, make_person):
    """A malformed id can never name a project, so a validation error leaks nothing."""
    employee = await make_person(role="employee")
    admin = await make_person(role="admin")

    from_employee = await role_client.get("/v1/projects/not-a-uuid", headers=employee.headers)
    from_admin = await role_client.get("/v1/projects/not-a-uuid", headers=admin.headers)

    assert from_employee.status_code == from_admin.status_code == 422


async def test_an_admin_passes_the_role_gate_on_the_admin_routes_we_can_call_blind(
    role_client, make_person
):
    admin = await make_person(role="admin")

    assert (await role_client.get("/v1/projects", headers=admin.headers)).status_code == 200
    assert (await role_client.get("/v1/admin/users", headers=admin.headers)).status_code == 200
    assert (
        await role_client.get("/v1/admin/invitations", headers=admin.headers)
    ).status_code == 200


async def test_an_employee_can_reach_me_and_is_told_their_role(role_client, make_person):
    employee = await make_person(role="employee")

    response = await role_client.get("/v1/auth/me", headers=employee.headers)

    assert response.status_code == 200
    assert response.json()["role"] == "employee"


# --- training courses and modules (Step 2, Piece 2) -----------------------------------------------

TRAINING_ROUTES = {
    ("POST", "/v1/admin/training/courses"): "admin",
    ("GET", "/v1/admin/training/courses"): "admin",
    ("GET", "/v1/admin/training/courses/{course_id}"): "admin_course",
    ("PATCH", "/v1/admin/training/courses/{course_id}"): "admin_course",
    ("DELETE", "/v1/admin/training/courses/{course_id}"): "admin_course",
    ("POST", "/v1/admin/training/courses/{course_id}/publish"): "admin_course",
    ("POST", "/v1/admin/training/courses/{course_id}/archive"): "admin_course",
    ("GET", "/v1/admin/training/courses/{course_id}/modules"): "admin_course",
    ("POST", "/v1/admin/training/courses/{course_id}/modules"): "admin_course",
    ("PUT", "/v1/admin/training/courses/{course_id}/modules/order"): "admin_course",
    ("GET", "/v1/admin/training/modules/{module_id}"): "admin_module",
    ("DELETE", "/v1/admin/training/modules/{module_id}"): "admin_module",
    ("POST", "/v1/admin/training/modules/{module_id}/republish"): "admin_module",
    ("POST", "/v1/admin/training/modules/{module_id}/unpublish"): "admin_module",
    ("GET", "/v1/admin/training/modules/{module_id}/video"): "admin_module",
    # Piece 3: enrolments. Three name a course (404 for an employee); the per-user listing takes
    # ``user_id`` as a query parameter, names no resource in its path, and so is a collection (403).
    ("POST", "/v1/admin/training/courses/{course_id}/enrolments"): "admin_course",
    ("GET", "/v1/admin/training/courses/{course_id}/enrolments"): "admin_course",
    (
        "POST",
        "/v1/admin/training/courses/{course_id}/enrolments/{user_id}/withdraw",
    ): "admin_course",
    ("GET", "/v1/admin/training/enrolments"): "admin",
}

# The employee area: exactly these two routes, and no assessment, progress or pass-gate route.
EMPLOYEE_TRAINING_ROUTES = {
    ("GET", "/v1/training/courses"): "employee",
    ("GET", "/v1/training/modules/{module_id}/video"): "employee_module",
}

RENDER_BODY = {
    "project_id": NIL_ID,
    "script_id": NIL_ID,
    "orientation": "horizontal",
    "quality": "low",
}
TRAINING_BODIES = {
    ("POST", "/v1/admin/training/courses"): {"title": "Safety"},
    ("PATCH", "/v1/admin/training/courses/{course_id}"): {"title": "Renamed"},
    ("POST", "/v1/admin/training/courses/{course_id}/modules"): RENDER_BODY,
    ("PUT", "/v1/admin/training/courses/{course_id}/modules/order"): {"module_ids": [NIL_ID]},
    ("POST", "/v1/admin/training/modules/{module_id}/republish"): RENDER_BODY,
    ("POST", "/v1/admin/training/courses/{course_id}/enrolments"): {"user_id": NIL_ID},
}


def _training_endpoints(app, prefix: str = "/v1/admin/training"):
    return {
        (method, endpoint.path): next(iter(policies_of(endpoint)))
        for method, endpoint in api_routes(app)
        if endpoint.path == prefix or endpoint.path.startswith(prefix + "/")
    }


def test_every_training_route_has_exactly_the_pinned_policy():
    """No route is added, removed or re-gated without this list changing in the same commit."""
    assert _training_endpoints(create_app()) == TRAINING_ROUTES


def test_the_employee_training_area_is_exactly_the_two_pinned_routes():
    """Adds nothing for assessments, attempts, progress or a pass gate (Step 3): any route under
    ``/v1/training`` outside this list fails here."""
    assert _training_endpoints(create_app(), "/v1/training") == EMPLOYEE_TRAINING_ROUTES


def test_a_route_naming_a_resource_anywhere_in_its_path_is_not_a_plain_admin_route():
    """Nested collections too: whatever carries ``{course_id}`` / ``{module_id}`` /
    ``{project_id}`` - at the end of the path or in the middle of it - is resource-dependent and so
    must carry the matching 404 policy, never the plain 403 ``admin`` one."""
    wrong = []
    for (method, path), policy in _training_endpoints(create_app()).items():
        for parameter, wanted in RESOURCE_POLICY_BY_PATH_PARAMETER:
            if parameter in path and policy != wanted:
                wrong.append((method, path, policy))
    assert wrong == []


def test_the_pinned_expectations_name_only_routes_that_exist():
    """A stale entry (a route that was removed or renamed) would otherwise sit there unchecked."""
    actual = {key for key in _training_endpoints(create_app())}
    actual |= {key for key in _training_endpoints(create_app(), "/v1/training")}
    pinned = set(TRAINING_ROUTES) | set(EMPLOYEE_TRAINING_ROUTES)
    assert pinned == actual
    served = {
        (method, endpoint.path) for method, endpoint in api_routes(create_app())
    }
    assert set(EXPECTED_NON_ADMIN) <= served


def test_the_nested_course_modules_listing_has_the_course_404_policy():
    """``GET /courses/{course_id}/modules`` does not end in an identifier but its answer depends on
    whether that course exists - the policy is the course one, not the plain admin 403."""
    policies = _training_endpoints(create_app())
    assert policies[("GET", "/v1/admin/training/courses/{course_id}/modules")] == "admin_course"
    assert policies[("GET", "/v1/admin/training/courses/{course_id}/enrolments")] == "admin_course"


def test_a_training_route_with_a_missing_or_doubled_policy_is_flagged():
    app = FastAPI()
    router = APIRouter(prefix="/v1/admin/training")

    @router.get("/courses/{course_id}")
    async def unguarded(course_id: str) -> dict[str, str]:
        return {}

    app.include_router(router)
    assert any("no role policy" in problem for problem in policy_violations(app))


async def test_an_employee_gets_the_same_404_bytes_as_an_admin_for_a_missing_training_resource(
    role_client, make_person
):
    """On every route that names a course or a module, the employee's 404 is byte-identical to
    the administrator's for an id that genuinely does not exist - same status, same body, no id."""
    admin = await make_person(role="admin")
    employee = await make_person(role="employee")
    checked = 0
    for (method, template), policy in TRAINING_ROUTES.items():
        if policy not in {"admin_course", "admin_module"}:
            continue
        url = _concrete(template)
        body = TRAINING_BODIES.get((method, template))
        results = []
        for who in (admin, employee):
            headers = {**who.headers, "Idempotency-Key": f"bytes-{who.email}-{checked}"}
            response = await role_client.request(method, url, json=body, headers=headers)
            results.append(response)
        admin_response, employee_response = results
        assert admin_response.status_code == 404, (method, template, admin_response.text)
        assert employee_response.status_code == 404, (method, template)
        assert employee_response.content == admin_response.content, (method, template)
        assert NIL_ID not in employee_response.text
        checked += 1
    assert checked == 16  # 11 course routes + 5 module routes


async def test_employee_is_forbidden_on_the_training_collection_routes(role_client, make_person):
    employee = await make_person(role="employee")
    headers = {**employee.headers, "Idempotency-Key": "employee-course-1"}

    created = await role_client.post(
        "/v1/admin/training/courses", json={"title": "Sneaky"}, headers=headers
    )
    listed = await role_client.get("/v1/admin/training/courses", headers=employee.headers)

    for response in (created, listed):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"


async def test_an_employee_cannot_reach_an_existing_course_or_module_and_learns_nothing(
    role_client, make_person
):
    """A real course exists; the employee's answer to every course route is the fixed 404."""
    admin = await make_person(role="admin")
    employee = await make_person(role="employee")
    created = await role_client.post(
        "/v1/admin/training/courses",
        json={"title": "Secret course"},
        headers={**admin.headers, "Idempotency-Key": "admin-course-1"},
    )
    assert created.status_code == 201
    course_id = created.json()["id"]

    for method, template in [k for k, v in TRAINING_ROUTES.items() if v == "admin_course"]:
        url = template.replace("{course_id}", course_id)
        body = TRAINING_BODIES.get((method, template))
        response = await role_client.request(
            method,
            url,
            json=body,
            headers={**employee.headers, "Idempotency-Key": f"emp-{method}-{len(template)}"},
        )
        assert response.status_code == 404, (method, template)
        assert response.json() == {"error": {"code": "not_found", "message": "Course not found."}}

    still = await role_client.get(f"/v1/admin/training/courses/{course_id}", headers=admin.headers)
    assert still.status_code == 200 and still.json()["title"] == "Secret course"
