# ruff: noqa: F811 - pytest fixtures imported from test_admin_training_api
"""``/v1/admin/training`` enrolments over HTTP, against the real application (Step 2, Piece 3).

Same pattern as ``test_admin_training_api.py`` (whose fixtures are reused): tokens signed locally,
the real routes and dependencies, the rolled-back ``db_session`` transaction, the real local Redis
and MinIO. What needs a database that really commits (two requests racing) is in
``test_training_enrolments_committed.py``.

Every assertion is about rows, objects and audit entries this test created - never about a table
or a bucket as a whole.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from edutoon.domain.training import COURSE_DELETE_REFUSED
from edutoon.repositories import training as training_repo
from edutoon.repositories.training import PositionGapError
from tests.idempotency_helpers import INVALID_KEYS
from tests.test_admin_training_api import (  # noqa: F401 - fixtures used by name
    BASE,
    NIL_ID,
    admin,
    h,
    make_course,
    positions,
    source,
    storage,
    track,
)
from tests.training_api_helpers import (
    audit_actions,
    audit_context,
    enrol,
    enrolment_rows,
    publish_modules,
    published_course,
    withdraw,
)

pytestmark = pytest.mark.real_auth


@pytest.fixture
async def employee(make_person):
    return await make_person(role="employee")


# --- enrolling ---


async def test_enrolling_an_employee_creates_one_active_enrolment_and_an_audit_entry(
    role_client, admin, employee, db_session
):
    course = await make_course(role_client, admin)

    response = await enrol(role_client, admin, course["id"], employee.user.id)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "active" and body["course_id"] == course["id"]
    assert body["user_id"] == str(employee.user.id) and body["user_email"] == employee.email
    assert body["assigned_by"] == str(admin.user.id)
    assert body["course_title"] == course["title"]
    rows = await enrolment_rows(db_session, course["id"], employee.user.id)
    assert [(str(r.id), r.status) for r in rows] == [(body["id"], "active")]
    assert await audit_actions(db_session, "course_enrolment", body["id"]) == [
        "training.enrolment.created"
    ]


async def test_an_administrator_cannot_be_enrolled_and_no_row_is_created(
    role_client, admin, make_person, db_session
):
    course = await make_course(role_client, admin)
    other_admin = await make_person(role="admin")

    for victim in (other_admin, admin):
        response = await enrol(role_client, admin, course["id"], victim.user.id)

        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "unprocessable"
        assert "employee" in response.json()["error"]["message"]
        assert await enrolment_rows(db_session, course["id"], victim.user.id) == []
    count = (
        await db_session.execute(
            text("SELECT count(*) FROM course_enrolments WHERE course_id = :c"),
            {"c": course["id"]},
        )
    ).scalar_one()
    assert count == 0


async def test_enrolling_an_unknown_user_or_into_an_unknown_course_is_a_404(
    role_client, admin, employee
):
    course = await make_course(role_client, admin)

    no_user = await enrol(role_client, admin, course["id"], NIL_ID)
    no_course = await enrol(role_client, admin, NIL_ID, employee.user.id)

    assert no_user.status_code == 404 and no_user.json()["error"]["message"] == "User not found."
    assert no_course.status_code == 404
    assert no_course.json()["error"]["message"] == "Course not found."


async def test_nobody_can_be_enrolled_in_an_archived_course(
    role_client, admin, employee, db_session
):
    course = await make_course(role_client, admin)
    await role_client.post(f"{BASE}/courses/{course['id']}/archive", headers=h(admin))

    response = await enrol(role_client, admin, course["id"], employee.user.id)

    assert response.status_code == 409 and "archived" in response.json()["error"]["message"]
    assert await enrolment_rows(db_session, course["id"], employee.user.id) == []


async def test_enrolling_someone_already_enrolled_is_a_409_and_changes_nothing(
    role_client, admin, employee, db_session
):
    course = await make_course(role_client, admin)
    first = await enrol(role_client, admin, course["id"], employee.user.id)

    second = await enrol(role_client, admin, course["id"], employee.user.id)

    assert first.status_code == 201 and second.status_code == 409
    assert "already enrolled" in second.json()["error"]["message"]
    rows = await enrolment_rows(db_session, course["id"], employee.user.id)
    assert [str(r.id) for r in rows] == [first.json()["id"]]


async def test_replaying_the_same_idempotency_key_returns_the_first_result_once(
    role_client, admin, employee, db_session
):
    course = await make_course(role_client, admin)
    key = f"enrol-{uuid4()}"

    first = await enrol(role_client, admin, course["id"], employee.user.id, key)
    replay = await enrol(role_client, admin, course["id"], employee.user.id, key)

    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert len(await enrolment_rows(db_session, course["id"], employee.user.id)) == 1
    assert await audit_actions(db_session, "course_enrolment", first.json()["id"]) == [
        "training.enrolment.created"
    ]


@pytest.mark.parametrize("key", INVALID_KEYS)
async def test_an_empty_or_oversized_idempotency_key_is_a_422(
    role_client, admin, employee, db_session, key
):
    course = await make_course(role_client, admin)
    path = f"{BASE}/courses/{course['id']}/enrolments"

    enrolled = await role_client.post(
        path,
        json={"user_id": str(employee.user.id)},
        headers={**admin.headers, "Idempotency-Key": key},
    )
    withdrawn = await role_client.post(
        f"{path}/{employee.user.id}/withdraw", headers={**admin.headers, "Idempotency-Key": key}
    )

    assert enrolled.status_code == 422 and withdrawn.status_code == 422
    assert await enrolment_rows(db_session, course["id"], employee.user.id) == []


async def test_a_mutation_without_an_idempotency_key_is_a_422(role_client, admin, employee):
    course = await make_course(role_client, admin)
    path = f"{BASE}/courses/{course['id']}/enrolments"

    enrolled = await role_client.post(
        path, json={"user_id": str(employee.user.id)}, headers=admin.headers
    )
    withdrawn = await role_client.post(f"{path}/{employee.user.id}/withdraw", headers=admin.headers)

    assert enrolled.status_code == 422 and withdrawn.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"user_id": "not-a-uuid"},
        {"user_id": None},
        {"user_id": NIL_ID, "status": "withdrawn"},
        {"user_id": NIL_ID, "assigned_by": NIL_ID},
        {"user_id": NIL_ID, "role": "admin"},
    ],
)
async def test_a_bad_enrolment_body_or_an_unknown_field_is_a_422(role_client, admin, body):
    course = await make_course(role_client, admin)

    response = await role_client.post(
        f"{BASE}/courses/{course['id']}/enrolments", json=body, headers=h(admin)
    )

    assert response.status_code == 422


# --- withdrawing and re-enrolling ---


async def test_withdrawing_keeps_the_row_and_a_second_withdrawal_is_a_409(
    role_client, admin, employee, db_session
):
    course = await make_course(role_client, admin)
    created = (await enrol(role_client, admin, course["id"], employee.user.id)).json()

    first = await withdraw(role_client, admin, course["id"], employee.user.id)
    second = await withdraw(role_client, admin, course["id"], employee.user.id)

    assert first.status_code == 200 and first.json()["status"] == "withdrawn"
    assert first.json()["id"] == created["id"]
    assert (
        second.status_code == 409 and "already been withdrawn" in second.json()["error"]["message"]
    )
    rows = await enrolment_rows(db_session, course["id"], employee.user.id)
    assert [(str(r.id), r.status) for r in rows] == [(created["id"], "withdrawn")]
    assert await audit_actions(db_session, "course_enrolment", created["id"]) == [
        "training.enrolment.created",
        "training.enrolment.withdrawn",
    ]


async def test_withdrawing_someone_not_enrolled_or_from_an_unknown_course_is_a_404(
    role_client, admin, employee
):
    course = await make_course(role_client, admin)

    not_enrolled = await withdraw(role_client, admin, course["id"], employee.user.id)
    no_course = await withdraw(role_client, admin, NIL_ID, employee.user.id)

    assert not_enrolled.status_code == 404
    assert not_enrolled.json()["error"]["message"] == "Enrolment not found."
    assert no_course.status_code == 404
    assert no_course.json()["error"]["message"] == "Course not found."


async def test_re_enrolling_a_withdrawn_employee_reactivates_the_same_row(
    role_client, admin, employee, make_person, db_session
):
    """The pair (course, user) has one row for ever: enrolling after a withdrawal makes that row
    active again - same id, same ``created_at``, no second row and no 409."""
    course = await make_course(role_client, admin)
    second_admin = await make_person(role="admin")
    created = (await enrol(role_client, admin, course["id"], employee.user.id)).json()
    await withdraw(role_client, admin, course["id"], employee.user.id)
    withdrawn_rows = await enrolment_rows(db_session, course["id"], employee.user.id)

    again = await enrol(role_client, second_admin, course["id"], employee.user.id)

    assert again.status_code == 200, again.text  # not 201 (nothing new) and not 409
    body = again.json()
    assert body["id"] == created["id"] and body["status"] == "active"
    assert body["created_at"] == created["created_at"]
    assert body["assigned_by"] == str(second_admin.user.id)
    rows = await enrolment_rows(db_session, course["id"], employee.user.id)
    assert len(rows) == 1 and str(rows[0].id) == created["id"] and rows[0].status == "active"
    assert rows[0].created_at == withdrawn_rows[0].created_at
    assert await audit_actions(db_session, "course_enrolment", created["id"]) == [
        "training.enrolment.created",
        "training.enrolment.withdrawn",
        "training.enrolment.reactivated",
    ]
    # The trail keeps who had assigned the enrolment before the row's assigner was replaced.
    context = await audit_context(db_session, "training.enrolment.reactivated", created["id"])
    assert context["previous_assigned_by"] == str(admin.user.id)
    assert context["course_id"] == course["id"] and context["user_id"] == str(employee.user.id)
    # ... and it can be withdrawn and re-enrolled again, still the one row.
    await withdraw(role_client, admin, course["id"], employee.user.id)
    third = await enrol(role_client, admin, course["id"], employee.user.id)
    assert third.status_code == 200 and third.json()["id"] == created["id"]
    assert len(await enrolment_rows(db_session, course["id"], employee.user.id)) == 1


async def test_a_withdrawn_enrolment_cannot_be_reactivated_in_an_archived_course(
    role_client, admin, employee, db_session
):
    course = await make_course(role_client, admin)
    await enrol(role_client, admin, course["id"], employee.user.id)
    await withdraw(role_client, admin, course["id"], employee.user.id)
    await role_client.post(f"{BASE}/courses/{course['id']}/archive", headers=h(admin))

    response = await enrol(role_client, admin, course["id"], employee.user.id)

    assert response.status_code == 409
    rows = await enrolment_rows(db_session, course["id"], employee.user.id)
    assert [r.status for r in rows] == ["withdrawn"]


# --- listing ---


async def test_a_courses_enrolments_are_listed_with_a_status_filter_and_pages(
    role_client, admin, make_person
):
    course = await make_course(role_client, admin)
    people = [await make_person(role="employee") for _ in range(3)]
    for person in people:
        assert (await enrol(role_client, admin, course["id"], person.user.id)).status_code == 201
    await withdraw(role_client, admin, course["id"], people[0].user.id)
    url = f"{BASE}/courses/{course['id']}/enrolments"

    everyone = await role_client.get(url, headers=admin.headers)
    active = await role_client.get(url + "?status=active", headers=admin.headers)
    withdrawn = await role_client.get(url + "?status=withdrawn", headers=admin.headers)
    first_page = await role_client.get(url + "?limit=2", headers=admin.headers)
    second_page = await role_client.get(
        url + f"?limit=2&cursor={first_page.json()['next_cursor']}", headers=admin.headers
    )

    assert {e["user_id"] for e in everyone.json()["items"]} == {str(p.user.id) for p in people}
    assert {e["user_id"] for e in active.json()["items"]} == {str(p.user.id) for p in people[1:]}
    assert [e["user_id"] for e in withdrawn.json()["items"]] == [str(people[0].user.id)]
    paged = first_page.json()["items"] + second_page.json()["items"]
    assert len(first_page.json()["items"]) == 2 and second_page.json()["next_cursor"] is None
    assert {e["id"] for e in paged} == {e["id"] for e in everyone.json()["items"]}
    assert (await role_client.get(url + "?status=bogus", headers=admin.headers)).status_code == 422
    assert (
        await role_client.get(f"{BASE}/courses/{NIL_ID}/enrolments", headers=admin.headers)
    ).status_code == 404


async def test_one_users_enrolments_are_listed_across_courses(role_client, admin, employee):
    one = await make_course(role_client, admin, "One")
    two = await make_course(role_client, admin, "Two")
    other = await make_course(role_client, admin, "Not theirs")
    await enrol(role_client, admin, one["id"], employee.user.id)
    await enrol(role_client, admin, two["id"], employee.user.id)
    await withdraw(role_client, admin, two["id"], employee.user.id)
    url = f"{BASE}/enrolments?user_id={employee.user.id}"

    everything = await role_client.get(url, headers=admin.headers)
    only_active = await role_client.get(url + "&status=active", headers=admin.headers)

    assert {e["course_id"] for e in everything.json()["items"]} == {one["id"], two["id"]}
    assert other["id"] not in {e["course_id"] for e in everything.json()["items"]}
    assert [e["course_id"] for e in only_active.json()["items"]] == [one["id"]]
    assert (await role_client.get(f"{BASE}/enrolments", headers=admin.headers)).status_code == 422
    unknown = await role_client.get(f"{BASE}/enrolments?user_id={NIL_ID}", headers=admin.headers)
    assert unknown.status_code == 404 and unknown.json()["error"]["message"] == "User not found."


# --- review fix B: a course with enrolment history is archived, not deleted ---


async def test_deleting_a_course_that_only_has_a_withdrawn_enrolment_says_to_archive_it(
    role_client, admin, employee, db_session
):
    course = await make_course(role_client, admin)
    await enrol(role_client, admin, course["id"], employee.user.id)
    await withdraw(role_client, admin, course["id"], employee.user.id)

    refused = await role_client.delete(f"{BASE}/courses/{course['id']}", headers=h(admin))

    assert refused.status_code == 409
    message = refused.json()["error"]["message"]
    assert message == COURSE_DELETE_REFUSED
    assert "Archive it instead" in message and "withdrawn" in message
    # Nothing was deleted, and archiving - the suggested route - works and keeps the history.
    assert len(await enrolment_rows(db_session, course["id"], employee.user.id)) == 1
    archived = await role_client.post(f"{BASE}/courses/{course['id']}/archive", headers=h(admin))
    assert archived.status_code == 200 and archived.json()["status"] == "archived"
    assert len(await enrolment_rows(db_session, course["id"], employee.user.id)) == 1


# --- review fix C: closing the gap a removed module leaves ---


async def _module_ids_by_position(db_session, course_id: str) -> list[tuple[int, str]]:
    rows = await db_session.execute(
        text(
            "SELECT position, id::text FROM training_modules WHERE course_id = :c ORDER BY position"
        ),
        {"c": course_id},
    )
    return [(row[0], row[1]) for row in rows.all()]


async def test_the_gap_helper_refuses_a_position_that_is_still_occupied(
    role_client, admin, source, track, db_session, storage
):
    course = await make_course(role_client, admin)
    modules = await publish_modules(
        role_client, admin, source, track, db_session, storage, course["id"], 3
    )
    before = await _module_ids_by_position(db_session, course["id"])

    with pytest.raises(PositionGapError, match="still occupied"):
        await training_repo.close_position_gap(
            db_session,
            course_id=_uuid(course["id"]),
            after=2,  # module 2 has not been removed
        )

    assert await _module_ids_by_position(db_session, course["id"]) == before
    # The last position is "still occupied" too, though nothing lies beyond it to be moved.
    with pytest.raises(PositionGapError, match="still occupied"):
        await training_repo.close_position_gap(db_session, course_id=_uuid(course["id"]), after=3)
    assert await _module_ids_by_position(db_session, course["id"]) == before
    assert [pos for pos, _ in before] == [1, 2, 3] and len(modules) == 3


async def test_the_gap_helper_closes_exactly_the_gap_left_by_a_removed_module(
    role_client, admin, source, track, db_session, storage
):
    course = await make_course(role_client, admin)
    modules = await publish_modules(
        role_client, admin, source, track, db_session, storage, course["id"], 4
    )
    await training_repo.delete_module(db_session, _uuid(modules[1]["id"]))  # remove position 2

    await training_repo.close_position_gap(db_session, course_id=_uuid(course["id"]), after=2)

    assert await _module_ids_by_position(db_session, course["id"]) == [
        (1, modules[0]["id"]),
        (2, modules[2]["id"]),
        (3, modules[3]["id"]),
    ]
    # Removing the last module leaves nothing to move.
    await training_repo.delete_module(db_session, _uuid(modules[3]["id"]))
    await training_repo.close_position_gap(db_session, course_id=_uuid(course["id"]), after=3)
    assert await _module_ids_by_position(db_session, course["id"]) == [
        (1, modules[0]["id"]),
        (2, modules[2]["id"]),
    ]
    assert await positions(role_client, admin, course["id"]) == [
        (modules[0]["id"], 1, "published"),
        (modules[2]["id"], 2, "published"),
    ]


def _uuid(value: str) -> UUID:
    return UUID(value)


# --- the whole thing as the administrator sees it ---


async def test_a_published_course_with_an_enrolled_employee_lists_in_the_admin_views(
    role_client, admin, employee, source, track, db_session, storage
):
    course, _ = await published_course(role_client, admin, source, track, db_session, storage)
    await enrol(role_client, admin, course["id"], employee.user.id)

    listing = await role_client.get(
        f"{BASE}/enrolments?user_id={employee.user.id}", headers=admin.headers
    )

    [item] = listing.json()["items"]
    assert item["course_id"] == course["id"] and item["course_status"] == "published"
