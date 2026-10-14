# ruff: noqa: F811 - pytest fixtures imported from test_training_committed
"""Enrolments against a database that really commits, with requests that really race.

The rolled-back tests cannot show what two simultaneous transactions do to each other, which is the
whole question for "enrol the same person twice at once". These use ``test_training_committed``'s
``world`` (a scratch database of their own, real commits, QA users removed afterwards) and fire the
requests together with ``asyncio.gather`` - each on its own client and so its own database session
and connection.

Assertions are about the one course and user the test made.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from uuid import uuid4

import pytest

from tests.test_training_committed import BASE, World, _make_person, world  # noqa: F401

pytestmark = pytest.mark.real_auth


async def _enrol(world: World, course_id: str, user_id, *, key: str | None = None):
    async with world.client(raise_app_exceptions=False) as client:
        return await client.post(
            f"{BASE}/courses/{course_id}/enrolments",
            json={"user_id": str(user_id)},
            headers=world.h(key=key or f"race-{uuid4()}"),
        )


async def _withdraw(world: World, course_id: str, user_id):
    async with world.client(raise_app_exceptions=False) as client:
        return await client.post(
            f"{BASE}/courses/{course_id}/enrolments/{user_id}/withdraw", headers=world.h()
        )


async def _rows(world: World, course_id: str, user_id) -> list[tuple[str, str]]:
    rows = await world.rows(
        "SELECT id::text, status FROM course_enrolments WHERE course_id = :c AND user_id = :u",
        c=course_id,
        u=user_id,
    )
    return [(row[0], row[1]) for row in rows]


async def _audit(world: World, enrolment_id: str) -> list[str]:
    rows = await world.rows(
        "SELECT action FROM audit_logs WHERE entity_type = 'course_enrolment' "
        "AND entity_id = :i ORDER BY created_at, id",
        i=enrolment_id,
    )
    return [row[0] for row in rows]


async def test_concurrent_enrolments_of_one_user_leave_exactly_one_row(world):
    async with world.client() as client:
        course_id = await world.make_course(client)
    employee = world.employee.user.id

    answers = await asyncio.gather(*(_enrol(world, course_id, employee) for _ in range(8)))

    assert Counter(a.status_code for a in answers) == {201: 1, 409: 7}, [
        (a.status_code, a.text) for a in answers
    ]
    [(enrolment_id, status)] = await _rows(world, course_id, employee)
    assert status == "active"
    [winner] = [a for a in answers if a.status_code == 201]
    assert winner.json()["id"] == enrolment_id
    assert await _audit(world, enrolment_id) == ["training.enrolment.created"]


async def test_concurrent_re_enrolments_of_a_withdrawn_user_reactivate_one_row(world):
    async with world.client() as client:
        course_id = await world.make_course(client)
    employee = world.employee.user.id
    created = await _enrol(world, course_id, employee)
    assert created.status_code == 201
    assert (await _withdraw(world, course_id, employee)).status_code == 200

    answers = await asyncio.gather(*(_enrol(world, course_id, employee) for _ in range(6)))

    assert Counter(a.status_code for a in answers) == {200: 1, 409: 5}, [
        (a.status_code, a.text) for a in answers
    ]
    assert await _rows(world, course_id, employee) == [(created.json()["id"], "active")]
    assert await _audit(world, created.json()["id"]) == [
        "training.enrolment.created",
        "training.enrolment.withdrawn",
        "training.enrolment.reactivated",
    ]


async def test_concurrent_enrolments_of_different_users_all_succeed_independently(
    world, mint_token
):
    async with world.client() as client:
        course_id = await world.make_course(client)
    people = [
        await _make_person(
            world.sessionmaker, world.registry, mint_token, role="employee", label=f"p{i}"
        )
        for i in range(5)
    ]

    answers = await asyncio.gather(*(_enrol(world, course_id, p.user.id) for p in people))

    assert [a.status_code for a in answers] == [201] * 5
    for person in people:
        assert len(await _rows(world, course_id, person.user.id)) == 1


async def test_an_enrolment_racing_the_deletion_of_its_course_never_leaves_an_orphan(world):
    """Whichever wins, the outcome is coherent: either the enrolment landed first and the course
    then refuses deletion (409), or the course was deleted first and the enrolment finds nothing
    (404). Never an enrolment row for a course that is gone, and never a 500."""
    outcomes = Counter()
    for attempt in range(6):
        async with world.client() as client:
            course_id = await world.make_course(client, title=f"Racing {attempt}")
        employee = world.employee.user.id

        async def delete(course_id=course_id):
            async with world.client(raise_app_exceptions=False) as client:
                return await client.delete(f"{BASE}/courses/{course_id}", headers=world.h())

        enrolled, deleted = await asyncio.gather(_enrol(world, course_id, employee), delete())

        pair = (enrolled.status_code, deleted.status_code)
        assert pair in {(201, 409), (404, 204)}, pair
        outcomes[pair] += 1
        remaining = await _rows(world, course_id, employee)
        course_exists = await world.scalar(
            "SELECT count(*) FROM training_courses WHERE id = :c", c=course_id
        )
        assert (len(remaining), course_exists) == ((1, 1) if pair == (201, 409) else (0, 0))
    assert sum(outcomes.values()) == 6


async def test_a_failed_commit_leaves_no_enrolment_and_the_same_key_can_be_retried(world):
    async with world.client() as client:
        course_id = await world.make_course(client)
    employee = world.employee.user.id
    key = f"retry-{uuid4()}"

    world.fail_commit = True
    failed = await _enrol(world, course_id, employee, key=key)
    world.fail_commit = False
    retried = await _enrol(world, course_id, employee, key=key)

    assert failed.status_code == 500
    assert retried.status_code == 201  # the key was released: a retry runs afresh
    [(enrolment_id, status)] = await _rows(world, course_id, employee)
    assert enrolment_id == retried.json()["id"] and status == "active"
    assert await _audit(world, enrolment_id) == ["training.enrolment.created"]


async def test_a_withdrawal_or_archive_stops_the_very_next_stream_request_after_real_commits(world):
    """Each request here has its own session and its own commit, as in production. Once the
    withdrawal (or the archive) has committed, the next request - on a different connection - is
    the standard 404, byte for byte what a module that does not exist gives."""
    async with world.client() as client:
        await world.fake_render()
        course_id = await world.make_course(client)
        module = (await world.publish(client, course_id)).json()
        published = await client.post(f"{BASE}/courses/{course_id}/publish", headers=world.h())
        assert published.status_code == 200
        employee = world.employee
        stream = f"/v1/training/modules/{module['id']}/video"
        missing = await client.get(
            "/v1/training/modules/00000000-0000-4000-8000-000000000000/video",
            headers=employee.headers,
        )
        assert (await _enrol(world, course_id, employee.user.id)).status_code == 201

        before = await client.get(stream, headers=employee.headers)
        withdrawn = await _withdraw(world, course_id, employee.user.id)
        after_withdrawal = await client.get(stream, headers=employee.headers)
        reenrolled = await _enrol(world, course_id, employee.user.id)
        restored = await client.get(stream, headers=employee.headers)
        archived = await client.post(f"{BASE}/courses/{course_id}/archive", headers=world.h())
        after_archive = await client.get(stream, headers=employee.headers)

    assert before.status_code == 200 and restored.status_code == 200
    assert (withdrawn.status_code, reenrolled.status_code, archived.status_code) == (200, 200, 200)
    for denied in (after_withdrawal, after_archive):
        assert denied.status_code == 404
        assert denied.content == missing.content
        assert denied.headers["content-type"] == missing.headers["content-type"]
