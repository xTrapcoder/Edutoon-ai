# ruff: noqa: F811 - pytest fixtures imported from test_admin_training_api
"""``/v1/training`` - what an enrolled employee sees and may stream (Step 2, Piece 3).

Real application, real routes and dependencies, rolled-back ``db_session``, real local Redis and
MinIO (the published snapshot is a real object). The fixtures come from ``test_admin_training_api``.

The central guarantees, each tested against the HTTP surface:

* **five causes, one 404** - no such module, not enrolled, enrolment withdrawn, course not
  published, module not published all answer byte-identically (status, body, headers), and none of
  them is listed;
* **locked is not "not found"** - an enrolled employee's later module is listed with ``locked`` and
  streams a 403 ``module_locked`` naming the blocker, for as long as an earlier module *that has an
  assessment* has not been passed (Step 3; a module with no assessment blocks nothing);
* **one serving function** - the employee stream and the administrator's preview are served by the
  same ``serve_module_video`` (Range behaviour itself is tested once, on the preview, in
  ``test_admin_training_api.py``).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from uuid import UUID

import pytest

from edutoon.api import video_response
from edutoon.api.routers import admin_training as admin_router
from edutoon.api.routers import training as employee_router
from edutoon.core.config import get_settings
from edutoon.services import training_access
from tests.test_admin_training_api import (  # noqa: F401 - fixtures used by name
    BASE,
    NIL_ID,
    admin,
    h,
    key_of,
    make_course,
    publish,
    source,
    storage,
    track,
)
from tests.training_api_helpers import (
    EMPLOYEE_BASE,
    answers_for,
    comparable,
    enrol,
    published_course,
    submit_attempt,
    withdraw,
)

pytestmark = pytest.mark.real_auth

STREAM = EMPLOYEE_BASE + "/modules/{}/video"
COURSES = EMPLOYEE_BASE + "/courses"


@pytest.fixture
async def employee(make_person):
    return await make_person(role="employee")


async def listing(client, person) -> list[dict]:
    response = await client.get(COURSES, headers=person.headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


# --- the course listing and the lock ---------------------------------------------------------


async def test_an_enrolled_employee_sees_their_course_and_only_the_first_module_is_open(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await published_course(
        role_client, admin, source, track, db_session, storage, modules=3, assessed=(0,)
    )
    await enrol(role_client, admin, course["id"], employee.user.id)

    [mine] = await listing(role_client, employee)

    assert mine["id"] == course["id"] and mine["title"] == course["title"]
    first, second, third = mine["modules"]
    assert [m["id"] for m in mine["modules"]] == [m["id"] for m in modules]
    assert first["locked"] is False and first["lock"] is None
    for locked, blocking in ((second, modules[0]), (third, modules[0])):
        assert locked["locked"] is True
        assert locked["lock"] == {
            "reason": "earlier_module_not_passed",
            "blocking_module_id": blocking["id"],
            "blocking_module_position": blocking["position"],
            "blocking_module_title": blocking["title"],
        }
    assert first["duration_ms"] == modules[0]["video_duration_ms"]
    # No storage location, hash or publisher leaks into an employee's view.
    leaked = {
        "video_key",
        "video_bucket",
        "video_output_hash",
        "published_by",
        "source_render_output_id",
    }
    assert not leaked & set(first)


async def test_the_first_module_streams_and_the_second_is_locked_until_the_first_is_passed(
    role_client, admin, employee, source, track, db_session, storage
):
    """Pinned in Piece 3 as "nothing is passed, so only module 1 is open"; updated in Step 3 now
    that ``passed_module_ids`` reads real attempts.

    Module 1 has an assessment, so it blocks module 2 until it is passed. The assertion on
    ``passed_module_ids`` is the visible link: empty before a pass, exactly module 1 after a passing
    attempt - and module 2 then streams with no other change.
    """
    course, modules = await published_course(
        role_client, admin, source, track, db_session, storage, modules=2, assessed=(0,)
    )
    await enrol(role_client, admin, course["id"], employee.user.id)
    ids = [UUID(m["id"]) for m in modules]

    passed = await training_access.passed_module_ids(
        db_session, user_id=employee.user.id, module_ids=ids
    )
    assert passed == frozenset()  # nothing attempted yet

    first = await role_client.get(STREAM.format(modules[0]["id"]), headers=employee.headers)
    assert first.status_code == 200 and first.content == source.body
    assert first.headers["content-type"] == "video/mp4"
    [mine] = await listing(role_client, employee)
    assert [m["locked"] for m in mine["modules"]] == [False, True]

    second = await role_client.get(STREAM.format(modules[1]["id"]), headers=employee.headers)
    assert second.status_code == 403
    assert second.json() == {
        "error": {
            "code": "module_locked",
            "message": "This module is locked until the earlier module has been completed.",
            "details": {
                "blocking_module_id": modules[0]["id"],
                "blocking_module_position": 1,
                "blocking_module_title": modules[0]["title"],
            },
        }
    }

    attempt = await submit_attempt(
        role_client,
        employee,
        modules[0]["id"],
        await answers_for(role_client, admin, modules[0]["id"]),
    )
    assert attempt.status_code == 201 and attempt.json()["passed"] is True
    assert await training_access.passed_module_ids(
        db_session, user_id=employee.user.id, module_ids=ids
    ) == frozenset({ids[0]})
    [mine] = await listing(role_client, employee)
    assert [m["locked"] for m in mine["modules"]] == [False, False]
    unlocked = await role_client.get(STREAM.format(modules[1]["id"]), headers=employee.headers)
    assert unlocked.status_code == 200


async def test_a_module_unlocks_only_through_the_passed_module_seam(
    role_client, admin, employee, source, track, db_session, storage, monkeypatch
):
    """Proves the lock really is *because* of ``passed_module_ids`` (not something else): when the
    seam says module 1 is passed, module 2 lists as open and streams - with no other change."""
    course, modules = await published_course(
        role_client, admin, source, track, db_session, storage, modules=2, assessed=(0,)
    )
    await enrol(role_client, admin, course["id"], employee.user.id)
    assert (
        await role_client.get(STREAM.format(modules[1]["id"]), headers=employee.headers)
    ).status_code == 403

    async def pretend_module_one_is_passed(session, *, user_id, module_ids):
        return frozenset({UUID(modules[0]["id"])}) & frozenset(module_ids)

    monkeypatch.setattr(training_access, "passed_module_ids", pretend_module_one_is_passed)

    [mine] = await listing(role_client, employee)
    unlocked = await role_client.get(STREAM.format(modules[1]["id"]), headers=employee.headers)
    assert [m["locked"] for m in mine["modules"]] == [False, False]
    assert unlocked.status_code == 200


async def test_an_unpublished_module_is_not_listed_and_does_not_block_the_next(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await published_course(
        role_client, admin, source, track, db_session, storage, modules=3, assessed=(0,)
    )
    await enrol(role_client, admin, course["id"], employee.user.id)
    await role_client.post(f"{BASE}/modules/{modules[1]['id']}/unpublish", headers=h(admin))

    [mine] = await listing(role_client, employee)

    assert [m["id"] for m in mine["modules"]] == [modules[0]["id"], modules[2]["id"]]
    assert mine["modules"][1]["lock"]["blocking_module_id"] == modules[0]["id"]


# --- the five causes, one 404 ------------------------------------------------------------------


async def test_five_different_reasons_for_no_access_give_one_identical_404(
    role_client, admin, make_person, source, track, db_session, storage
):
    course, modules = await published_course(
        role_client, admin, source, track, db_session, storage, modules=2
    )
    target = modules[0]["id"]
    employee = await make_person(role="employee")
    await enrol(role_client, admin, course["id"], employee.user.id)
    assert (
        await role_client.get(STREAM.format(target), headers=employee.headers)
    ).status_code == 200  # the baseline: this employee really can stream it

    stranger = await make_person(role="employee")  # (2) enrolled in nothing
    former = await make_person(role="employee")  # (3) enrolled, then withdrawn
    await enrol(role_client, admin, course["id"], former.user.id)
    await withdraw(role_client, admin, course["id"], former.user.id)

    draft = await make_course(role_client, admin, "Not yet published")  # (4) course not published
    [draft_module] = [
        (await publish(role_client, track, admin, draft["id"], source, title="Draft one")).json()
    ]
    await enrol(role_client, admin, draft["id"], employee.user.id)

    await role_client.post(f"{BASE}/modules/{modules[1]['id']}/unpublish", headers=h(admin))  # (5)

    answers = {
        "missing module": await role_client.get(STREAM.format(NIL_ID), headers=employee.headers),
        "not enrolled": await role_client.get(STREAM.format(target), headers=stranger.headers),
        "withdrawn": await role_client.get(STREAM.format(target), headers=former.headers),
        "course not published": await role_client.get(
            STREAM.format(draft_module["id"]), headers=employee.headers
        ),
        "module not published": await role_client.get(
            STREAM.format(modules[1]["id"]), headers=employee.headers
        ),
        # Not one of the five, but it must not be told apart either: an administrator on the
        # employee route.
        "administrator": await role_client.get(STREAM.format(target), headers=admin.headers),
    }

    reference = comparable(answers["missing module"])
    assert reference[0] == 404
    assert reference[1] == b'{"error":{"code":"not_found","message":"Module not found."}}'
    for cause, response in answers.items():
        assert comparable(response) == reference, (
            f"{cause} is distinguishable from a missing module"
        )


async def test_modules_the_employee_cannot_open_are_not_in_their_listing(
    role_client, admin, make_person, source, track, db_session, storage
):
    course, modules = await published_course(
        role_client, admin, source, track, db_session, storage, modules=2
    )
    employee = await make_person(role="employee")
    stranger = await make_person(role="employee")
    former = await make_person(role="employee")
    await enrol(role_client, admin, course["id"], employee.user.id)
    await enrol(role_client, admin, course["id"], former.user.id)
    await withdraw(role_client, admin, course["id"], former.user.id)
    draft = await make_course(role_client, admin, "Draft")
    await publish(role_client, track, admin, draft["id"], source, title="Draft one")
    await enrol(role_client, admin, draft["id"], employee.user.id)
    await role_client.post(f"{BASE}/modules/{modules[1]['id']}/unpublish", headers=h(admin))

    mine = await listing(role_client, employee)

    assert [c["id"] for c in mine] == [course["id"]]  # the draft course is absent
    assert [m["id"] for m in mine[0]["modules"]] == [modules[0]["id"]]  # unpublished one absent
    assert await listing(role_client, stranger) == []
    assert await listing(role_client, former) == []


async def test_withdrawal_takes_effect_for_the_next_request(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await published_course(
        role_client, admin, source, track, db_session, storage, modules=1
    )
    await enrol(role_client, admin, course["id"], employee.user.id)
    url = STREAM.format(modules[0]["id"])
    missing = await role_client.get(STREAM.format(NIL_ID), headers=employee.headers)
    assert (await role_client.get(url, headers=employee.headers)).status_code == 200

    withdrawn = await withdraw(role_client, admin, course["id"], employee.user.id)
    after = await role_client.get(url, headers=employee.headers)

    assert withdrawn.status_code == 200
    assert comparable(after) == comparable(missing)
    assert await listing(role_client, employee) == []


async def test_archiving_the_course_makes_every_module_unavailable_with_the_same_404(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await published_course(
        role_client, admin, source, track, db_session, storage, modules=2
    )
    await enrol(role_client, admin, course["id"], employee.user.id)
    missing = await role_client.get(STREAM.format(NIL_ID), headers=employee.headers)
    assert (
        await role_client.get(STREAM.format(modules[0]["id"]), headers=employee.headers)
    ).status_code == 200

    archived = await role_client.post(f"{BASE}/courses/{course['id']}/archive", headers=h(admin))

    assert archived.status_code == 200
    for module in modules:  # the open module and the (previously locked) one alike
        after = await role_client.get(STREAM.format(module["id"]), headers=employee.headers)
        assert comparable(after) == comparable(missing)
    assert await listing(role_client, employee) == []


async def test_re_enrolling_restores_access_under_the_published_rules(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await published_course(
        role_client, admin, source, track, db_session, storage, modules=2, assessed=(0,)
    )
    await enrol(role_client, admin, course["id"], employee.user.id)
    await withdraw(role_client, admin, course["id"], employee.user.id)
    assert (
        await role_client.get(STREAM.format(modules[0]["id"]), headers=employee.headers)
    ).status_code == 404

    again = await enrol(role_client, admin, course["id"], employee.user.id)

    assert again.status_code == 200
    assert (
        await role_client.get(STREAM.format(modules[0]["id"]), headers=employee.headers)
    ).status_code == 200
    assert (
        await role_client.get(STREAM.format(modules[1]["id"]), headers=employee.headers)
    ).status_code == 403  # still locked: access is restored, not widened


# --- the stream is the module's own snapshot -------------------------------------------------


async def test_the_stream_serves_the_snapshot_even_if_the_source_render_object_is_gone(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await published_course(
        role_client, admin, source, track, db_session, storage, modules=1
    )
    await enrol(role_client, admin, course["id"], employee.user.id)
    await storage.delete_object(bucket=source.output.storage_bucket, key=source.output.storage_key)

    response = await role_client.get(STREAM.format(modules[0]["id"]), headers=employee.headers)

    assert response.status_code == 200 and response.content == source.body


async def test_a_snapshot_file_gone_from_storage_gets_the_standard_404(
    role_client, admin, employee, source, track, db_session, storage
):
    """If the stored file has vanished the employee is told what any other unavailable module
    tells them - not the administrator's more specific 'no video to play' wording."""
    course, modules = await published_course(
        role_client, admin, source, track, db_session, storage, modules=1
    )
    await enrol(role_client, admin, course["id"], employee.user.id)
    missing = await role_client.get(STREAM.format(NIL_ID), headers=employee.headers)
    key = await key_of(db_session, modules[0]["id"])
    await storage.delete_object(bucket=get_settings().BUCKET_OUTPUTS, key=key)

    gone = await role_client.get(STREAM.format(modules[0]["id"]), headers=employee.headers)

    assert gone.status_code == 404 and comparable(gone) == comparable(missing)
    # ... while the administrator's preview of the same module keeps its own wording.
    preview = await role_client.get(
        f"{BASE}/modules/{modules[0]['id']}/video", headers=admin.headers
    )
    assert preview.status_code == 404
    assert preview.json()["error"]["message"] == "This module has no video to play."


async def test_the_stream_supports_a_range_request_like_the_preview(
    role_client, admin, employee, source, track, db_session, storage
):
    """A smoke test that the shared function's partial-content path is reachable from this route;
    the Range behaviour itself (206 / 416 / headers) is covered once, on the preview."""
    course, modules = await published_course(
        role_client, admin, source, track, db_session, storage, modules=1
    )
    await enrol(role_client, admin, course["id"], employee.user.id)

    part = await role_client.get(
        STREAM.format(modules[0]["id"]), headers={**employee.headers, "Range": "bytes=2-11"}
    )

    assert part.status_code == 206 and part.content == source.body[2:12]


# --- one serving function ---------------------------------------------------------------------


def _function_source(module, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(Path(inspect.getsourcefile(module)).read_text(encoding="utf-8"))
    [node] = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name]
    return node


def _names_used(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
    }


def test_both_video_routes_are_served_by_the_one_function_and_neither_serves_by_itself():
    preview = _function_source(admin_router, "stream_module_video")
    employee = _function_source(employee_router, "stream_module_video")

    assert "serve_module_video" in _names_used(preview)
    assert "serve_module_video" in _names_used(employee)
    # Neither route body opens the object, parses a Range, or builds a streaming response itself.
    for route in (preview, employee):
        used = _names_used(route)
        assert not used & {
            "StreamingResponse",
            "open_module_video",
            "iter_chunks",
            "open_object",
            "content_range",
            "content_length",
        }
    # And both modules import the very same function object.
    assert admin_router.serve_module_video is employee_router.serve_module_video
    assert admin_router.serve_module_video is video_response.serve_module_video


async def test_each_route_reaches_the_shared_function_exactly_once_per_request(
    role_client, admin, employee, source, track, db_session, storage, monkeypatch
):
    course, modules = await published_course(
        role_client, admin, source, track, db_session, storage, modules=1
    )
    await enrol(role_client, admin, course["id"], employee.user.id)
    calls: list[UUID] = []
    real = video_response.serve_module_video

    async def spy(*args, **kwargs):
        calls.append(kwargs["module_id"])
        return await real(*args, **kwargs)

    monkeypatch.setattr(admin_router, "serve_module_video", spy)
    monkeypatch.setattr(employee_router, "serve_module_video", spy)
    module_id = UUID(modules[0]["id"])

    as_admin = await role_client.get(f"{BASE}/modules/{module_id}/video", headers=admin.headers)
    as_employee = await role_client.get(STREAM.format(module_id), headers=employee.headers)

    assert as_admin.status_code == as_employee.status_code == 200
    assert as_admin.content == as_employee.content == source.body
    assert calls == [module_id, module_id]


async def test_a_locked_or_missing_module_never_reaches_the_shared_function(
    role_client, admin, employee, source, track, db_session, storage, monkeypatch
):
    course, modules = await published_course(
        role_client, admin, source, track, db_session, storage, modules=2, assessed=(0,)
    )
    await enrol(role_client, admin, course["id"], employee.user.id)
    calls: list[object] = []

    async def spy(*args, **kwargs):
        calls.append(kwargs)
        raise AssertionError("access must be decided before anything is served")

    monkeypatch.setattr(employee_router, "serve_module_video", spy)

    locked = await role_client.get(STREAM.format(modules[1]["id"]), headers=employee.headers)
    missing = await role_client.get(STREAM.format(NIL_ID), headers=employee.headers)

    assert (locked.status_code, missing.status_code) == (403, 404)
    assert calls == []
