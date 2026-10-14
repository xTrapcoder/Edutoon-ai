"""``/v1/admin/training`` - courses and modules over HTTP, against the real application.

Same pattern as ``test_route_policies.py``: tokens signed locally, the real routes and
dependencies, the database the rolled-back ``db_session`` transaction (so nothing persists), the
real local Redis (idempotency) and the real local MinIO. The source video is a small stand-in
object (``training_helpers.add_fake_render``): publishing copies and measures it and never decodes
it. What needs a *committed* database - the deferred position constraint across commits, a
re-render, deleting the source project, a rolled-back transaction - is in
``test_training_committed.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from edutoon.core.config import get_settings
from edutoon.domain.training_storage import belongs_to_module, module_storage_prefix
from edutoon.providers.storage import Storage, get_storage_client
from edutoon.repositories import projects as projects_repo
from tests.idempotency_helpers import INVALID_KEYS
from tests.training_helpers import (
    add_fake_render,
    add_script,
    list_keys,
    object_bytes,
    object_exists,
    render_body,
)

pytestmark = pytest.mark.real_auth

BASE = "/v1/admin/training"
NIL_ID = "00000000-0000-4000-8000-000000000000"


@pytest.fixture
def storage() -> Storage:
    settings = get_settings()
    return get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )


@dataclass
class Tracker:
    storage: Storage
    keys: list[tuple[str, str]] = field(default_factory=list)
    module_ids: list[str] = field(default_factory=list)

    def module(self, response: httpx.Response) -> dict:
        body = response.json()
        self.module_ids.append(body["id"])
        return body


@pytest.fixture
async def track(storage):
    tracker = Tracker(storage)
    yield tracker
    bucket = get_settings().BUCKET_OUTPUTS
    for bucket_, key in tracker.keys:
        await storage.delete_object(bucket=bucket_, key=key)
    for module_id in tracker.module_ids:
        for key in await list_keys(storage, bucket, module_storage_prefix(UUID(module_id))):
            await storage.delete_object(bucket=bucket, key=key)


@dataclass
class Source:
    admin: object
    project: object
    script: object
    output: object
    body: bytes


@pytest.fixture
async def admin(make_person):
    return await make_person(role="admin")


@pytest.fixture
async def source(db_session, storage, admin, track) -> Source:
    project = await projects_repo.create(
        db_session, owner_id=admin.user.id, title="Source project", source_type="topic"
    )
    script = await add_script(db_session, project)
    output, body = await add_fake_render(db_session, storage, project=project, script=script)
    track.keys.append((output.storage_bucket, output.storage_key))
    return Source(admin, project, script, output, body)


def h(person, key: str | None = None) -> dict[str, str]:
    return {**person.headers, "Idempotency-Key": key or f"k-{uuid4()}"}


async def make_course(client, admin, title: str = "Safety induction", **extra) -> dict:
    response = await client.post(
        f"{BASE}/courses", json={"title": title, **extra}, headers=h(admin)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def publish(client, track, admin, course_id, source: Source, **extra) -> httpx.Response:
    body = render_body(source.project, source.script, **extra)
    response = await client.post(f"{BASE}/courses/{course_id}/modules", json=body, headers=h(admin))
    if response.status_code == 201:
        track.module(response)
    return response


async def key_of(db_session, module_id: str) -> str | None:
    row = (
        await db_session.execute(
            text("SELECT video_key FROM training_modules WHERE id = :i"), {"i": module_id}
        )
    ).first()
    return row[0] if row else None


async def positions(client, admin, course_id) -> list[tuple[str, int, str]]:
    response = await client.get(f"{BASE}/courses/{course_id}/modules", headers=admin.headers)
    assert response.status_code == 200
    return [(m["id"], m["position"], m["status"]) for m in response.json()["items"]]


async def extra_output(db_session, storage, source: Source, track, orientation, quality):
    output, body = await add_fake_render(
        db_session,
        storage,
        project=source.project,
        script=source.script,
        orientation=orientation,
        quality=quality,
    )
    track.keys.append((output.storage_bucket, output.storage_key))
    return output, body


# --- courses ---


async def test_create_get_list_and_update_a_course(role_client, admin):
    created = await make_course(role_client, admin, "  Fire safety  ", description="Basics")

    assert created["title"] == "Fire safety" and created["status"] == "draft"
    assert created["module_count"] == 0 and created["published_module_count"] == 0
    assert created["created_by"] == str(admin.user.id)

    got = await role_client.get(f"{BASE}/courses/{created['id']}", headers=admin.headers)
    assert got.status_code == 200 and got.json() == created

    listed = await role_client.get(f"{BASE}/courses?status=draft&limit=100", headers=admin.headers)
    assert listed.status_code == 200
    assert created["id"] in [c["id"] for c in listed.json()["items"]]
    published = await role_client.get(f"{BASE}/courses?status=published", headers=admin.headers)
    assert created["id"] not in [c["id"] for c in published.json()["items"]]

    patched = await role_client.patch(
        f"{BASE}/courses/{created['id']}",
        json={"title": "Fire safety 2", "description": None},
        headers=h(admin),
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "Fire safety 2" and patched.json()["description"] is None


async def test_a_null_title_in_a_patch_leaves_the_title_alone(role_client, admin):
    created = await make_course(role_client, admin, "Keep")
    patched = await role_client.patch(
        f"{BASE}/courses/{created['id']}", json={"title": None}, headers=h(admin)
    )
    assert patched.status_code == 200 and patched.json()["title"] == "Keep"


@pytest.mark.parametrize(
    "body",
    [
        {"title": "x", "status": "published"},
        {"title": "x", "created_by": NIL_ID},
        {"title": "   "},
        {"title": ""},
        {},
        {"title": "x" * 201},
    ],
)
async def test_a_bad_course_body_is_a_422(role_client, admin, body):
    response = await role_client.post(f"{BASE}/courses", json=body, headers=h(admin))
    assert response.status_code == 422


async def test_a_course_patch_rejects_unknown_fields_such_as_status(role_client, admin):
    created = await make_course(role_client, admin)
    for body in ({"status": "published"}, {"title": "ok", "id": NIL_ID}):
        response = await role_client.patch(
            f"{BASE}/courses/{created['id']}", json=body, headers=h(admin)
        )
        assert response.status_code == 422


@pytest.mark.parametrize("key", INVALID_KEYS)
async def test_every_mutating_training_route_validates_the_idempotency_key(role_client, admin, key):
    created = await make_course(role_client, admin)
    cid = created["id"]
    routes = [
        ("POST", f"{BASE}/courses", {"title": "x"}),
        ("PATCH", f"{BASE}/courses/{cid}", {"title": "x"}),
        ("POST", f"{BASE}/courses/{cid}/publish", None),
        ("POST", f"{BASE}/courses/{cid}/archive", None),
        ("DELETE", f"{BASE}/courses/{cid}", None),
        (
            "POST",
            f"{BASE}/courses/{cid}/modules",
            {
                "project_id": NIL_ID,
                "script_id": NIL_ID,
                "orientation": "horizontal",
                "quality": "low",
            },
        ),
        ("PUT", f"{BASE}/courses/{cid}/modules/order", {"module_ids": [NIL_ID]}),
        ("DELETE", f"{BASE}/modules/{NIL_ID}", None),
        ("POST", f"{BASE}/modules/{NIL_ID}/unpublish", None),
        (
            "POST",
            f"{BASE}/modules/{NIL_ID}/republish",
            {
                "project_id": NIL_ID,
                "script_id": NIL_ID,
                "orientation": "horizontal",
                "quality": "low",
            },
        ),
    ]
    headers = dict(admin.headers) if key is None else {**admin.headers, "Idempotency-Key": key}
    for method, url, body in routes:
        response = await role_client.request(method, url, json=body, headers=headers)
        assert response.status_code == 422, (method, url, response.status_code)


async def test_creating_a_course_twice_with_one_key_replays_the_first(role_client, admin):
    headers = h(admin, "same-key-1")
    first = await role_client.post(f"{BASE}/courses", json={"title": "Once"}, headers=headers)
    second = await role_client.post(f"{BASE}/courses", json={"title": "Once"}, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    listed = await role_client.get(f"{BASE}/courses?limit=100", headers=admin.headers)
    assert [c["title"] for c in listed.json()["items"]].count("Once") == 1


async def test_a_course_cannot_be_published_empty_and_archived_courses_are_frozen(
    role_client, admin
):
    course = await make_course(role_client, admin)
    cid = course["id"]

    empty = await role_client.post(f"{BASE}/courses/{cid}/publish", headers=h(admin))
    assert empty.status_code == 409 and "at least one module" in empty.json()["error"]["message"]

    archived = await role_client.post(f"{BASE}/courses/{cid}/archive", headers=h(admin))
    assert archived.status_code == 200 and archived.json()["status"] == "archived"
    again = await role_client.post(f"{BASE}/courses/{cid}/archive", headers=h(admin))
    assert again.status_code == 409
    edit = await role_client.patch(f"{BASE}/courses/{cid}", json={"title": "n"}, headers=h(admin))
    assert edit.status_code == 409
    publish_again = await role_client.post(f"{BASE}/courses/{cid}/publish", headers=h(admin))
    assert publish_again.status_code == 409


async def test_publishing_a_course_needs_a_published_module_and_only_works_from_draft(
    role_client, admin, source, track
):
    course = await make_course(role_client, admin)
    assert (await publish(role_client, track, admin, course["id"], source)).status_code == 201

    published = await role_client.post(f"{BASE}/courses/{course['id']}/publish", headers=h(admin))
    assert published.status_code == 200
    assert published.json()["status"] == "published"
    assert published.json()["published_module_count"] == 1
    twice = await role_client.post(f"{BASE}/courses/{course['id']}/publish", headers=h(admin))
    assert twice.status_code == 409


async def test_an_unpublished_only_course_cannot_be_published(role_client, admin, source, track):
    course = await make_course(role_client, admin)
    module = track.module(await publish(role_client, track, admin, course["id"], source))
    await role_client.post(f"{BASE}/modules/{module['id']}/unpublish", headers=h(admin))

    response = await role_client.post(f"{BASE}/courses/{course['id']}/publish", headers=h(admin))
    assert response.status_code == 409


async def test_a_course_with_enrolments_cannot_be_deleted_but_an_empty_one_can(
    role_client, admin, make_person, db_session
):
    employee = await make_person(role="employee")
    kept = await make_course(role_client, admin, "Has people")
    await db_session.execute(
        text("INSERT INTO course_enrolments (course_id, user_id, assigned_by) VALUES (:c, :u, :a)"),
        {"c": kept["id"], "u": employee.user.id, "a": admin.user.id},
    )

    refused = await role_client.delete(f"{BASE}/courses/{kept['id']}", headers=h(admin))
    assert refused.status_code == 409 and "Archive it instead" in refused.json()["error"]["message"]
    assert (
        await role_client.get(f"{BASE}/courses/{kept['id']}", headers=admin.headers)
    ).status_code == 200

    empty = await make_course(role_client, admin, "Nobody")
    deleted = await role_client.delete(f"{BASE}/courses/{empty['id']}", headers=h(admin))
    assert deleted.status_code == 204
    gone = await role_client.get(f"{BASE}/courses/{empty['id']}", headers=admin.headers)
    assert gone.status_code == 404 and gone.json()["error"]["message"] == "Course not found."


async def test_a_course_with_a_withdrawn_enrolment_still_cannot_be_deleted(
    role_client, admin, make_person, db_session
):
    employee = await make_person(role="employee")
    course = await make_course(role_client, admin)
    await db_session.execute(
        text(
            "INSERT INTO course_enrolments (course_id, user_id, assigned_by, status) "
            "VALUES (:c, :u, :a, 'withdrawn')"
        ),
        {"c": course["id"], "u": employee.user.id, "a": admin.user.id},
    )
    response = await role_client.delete(f"{BASE}/courses/{course['id']}", headers=h(admin))
    assert response.status_code == 409


async def test_deleting_a_course_removes_its_modules_and_their_videos(
    role_client, admin, source, track, db_session, storage
):
    course = await make_course(role_client, admin)
    module = track.module(await publish(role_client, track, admin, course["id"], source))
    key = await key_of(db_session, module["id"])
    bucket = get_settings().BUCKET_OUTPUTS
    assert await object_exists(storage, bucket, key)

    deleted = await role_client.delete(f"{BASE}/courses/{course['id']}", headers=h(admin))

    assert deleted.status_code == 204
    assert not await object_exists(storage, bucket, key)
    assert await key_of(db_session, module["id"]) is None
    # The source render is untouched.
    assert await object_exists(storage, source.output.storage_bucket, source.output.storage_key)


# --- publishing a module (the snapshot) -----------------------------------------------------------


async def test_publishing_copies_the_video_to_the_modules_own_key_and_records_the_snapshot(
    role_client, admin, source, track, db_session, storage
):
    course = await make_course(role_client, admin)

    response = await publish(role_client, track, admin, course["id"], source, description="d")

    assert response.status_code == 201, response.text
    module = response.json()
    assert module["position"] == 1 and module["status"] == "published"
    assert module["title"] == "Source project"  # defaults to the project's title
    assert module["video_byte_size"] == len(source.body)
    assert module["video_duration_ms"] == source.output.duration_ms
    assert (module["video_width"], module["video_height"], module["video_fps"]) == (640, 360, 30)
    assert module["video_output_hash"] == source.output.output_hash
    assert module["source_render_output_id"] == str(source.output.id)
    assert "video_key" not in module and "video_bucket" not in module

    key = await key_of(db_session, module["id"])
    assert key is not None and belongs_to_module(key, UUID(module["id"]))
    assert key != source.output.storage_key
    assert key.startswith(f"training/modules/{module['id']}/") and key.endswith(".mp4")
    bucket = get_settings().BUCKET_OUTPUTS
    assert await object_bytes(storage, bucket, key) == source.body
    assert await object_bytes(storage, bucket, source.output.storage_key) == source.body


async def test_each_publication_uses_a_fresh_key_and_modules_are_appended_in_order(
    role_client, admin, source, track, db_session, storage
):
    course = await make_course(role_client, admin)
    second_output, _ = await extra_output(db_session, storage, source, track, "horizontal", "high")
    third_output, _ = await extra_output(db_session, storage, source, track, "vertical", "low")

    first = track.module(await publish(role_client, track, admin, course["id"], source))
    second = track.module(
        await publish(role_client, track, admin, course["id"], source, quality="high", title="Two")
    )
    third = track.module(
        await publish(
            role_client, track, admin, course["id"], source, orientation="vertical", title="Three"
        )
    )

    assert [first["position"], second["position"], third["position"]] == [1, 2, 3]
    assert second["title"] == "Two"
    keys = {await key_of(db_session, m["id"]) for m in (first, second, third)}
    assert len(keys) == 3
    assert second["source_render_output_id"] == str(second_output.id)
    assert third["source_render_output_id"] == str(third_output.id)


async def test_publishing_the_same_render_twice_into_a_course_is_a_409_and_copies_nothing(
    role_client, admin, source, track, storage
):
    course = await make_course(role_client, admin)
    first = track.module(await publish(role_client, track, admin, course["id"], source))
    bucket = get_settings().BUCKET_OUTPUTS
    before = await list_keys(storage, bucket, "training/modules/")

    second = await publish(role_client, track, admin, course["id"], source)

    assert second.status_code == 409
    assert (
        second.json()["error"]["message"]
        == "This rendered video is already published in this course."
    )
    assert await list_keys(storage, bucket, "training/modules/") == before
    listing = await positions(role_client, admin, course["id"])
    assert [m[0] for m in listing] == [first["id"]]


async def test_the_same_render_may_be_published_into_two_different_courses(
    role_client, admin, source, track
):
    one = await make_course(role_client, admin, "One")
    two = await make_course(role_client, admin, "Two")
    assert (await publish(role_client, track, admin, one["id"], source)).status_code == 201
    assert (await publish(role_client, track, admin, two["id"], source)).status_code == 201


async def test_an_idempotent_replay_of_a_publish_makes_one_module_and_one_object(
    role_client, admin, source, track, storage
):
    course = await make_course(role_client, admin)
    headers = h(admin, "publish-once")
    body = render_body(source.project, source.script)
    url = f"{BASE}/courses/{course['id']}/modules"

    first = await role_client.post(url, json=body, headers=headers)
    track.module(first)
    second = await role_client.post(url, json=body, headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    bucket = get_settings().BUCKET_OUTPUTS
    assert (
        len(await list_keys(storage, bucket, module_storage_prefix(UUID(first.json()["id"])))) == 1
    )


async def test_publishing_needs_an_approved_script(role_client, admin, db_session, storage, track):
    project = await projects_repo.create(
        db_session, owner_id=admin.user.id, title="Draft", source_type="topic"
    )
    script = await add_script(db_session, project, approved=False)
    output, body = await add_fake_render(db_session, storage, project=project, script=script)
    track.keys.append((output.storage_bucket, output.storage_key))
    course = await make_course(role_client, admin)

    response = await role_client.post(
        f"{BASE}/courses/{course['id']}/modules",
        json=render_body(project, script),
        headers=h(admin),
    )

    assert response.status_code == 409
    assert "Approve the script" in response.json()["error"]["message"]


async def test_publishing_somebody_elses_render_is_a_404(
    role_client, admin, source, make_person, track
):
    intruder = await make_person(role="admin")
    course = await make_course(role_client, intruder)

    response = await role_client.post(
        f"{BASE}/courses/{course['id']}/modules",
        json=render_body(source.project, source.script),
        headers=h(intruder),
    )

    assert response.status_code == 404


async def test_publishing_a_render_that_does_not_exist_is_a_404_and_copies_nothing(
    role_client, admin, source, track, storage
):
    course = await make_course(role_client, admin)
    bucket = get_settings().BUCKET_OUTPUTS
    before = await list_keys(storage, bucket, "training/modules/")

    response = await publish(role_client, track, admin, course["id"], source, quality="high")

    assert response.status_code == 404
    assert await list_keys(storage, bucket, "training/modules/") == before


async def test_publishing_when_the_source_file_is_missing_or_the_wrong_size_is_refused(
    role_client, admin, source, track, storage, db_session
):
    course = await make_course(role_client, admin)
    bucket = get_settings().BUCKET_OUTPUTS
    before = await list_keys(storage, bucket, "training/modules/")

    await db_session.execute(
        text("UPDATE render_outputs SET byte_size = byte_size + 1 WHERE id = :i"),
        {"i": source.output.id},
    )
    wrong_size = await publish(role_client, track, admin, course["id"], source)
    assert wrong_size.status_code == 409
    # refused on the source's own size, before anything was copied
    assert "does not match its record" in wrong_size.json()["error"]["message"]

    await storage.delete_object(bucket=bucket, key=source.output.storage_key)
    missing = await publish(role_client, track, admin, course["id"], source)
    assert missing.status_code == 404
    assert await list_keys(storage, bucket, "training/modules/") == before


async def test_a_publish_body_rejects_unknown_fields_and_bad_values(role_client, admin, source):
    course = await make_course(role_client, admin)
    url = f"{BASE}/courses/{course['id']}/modules"
    for extra in (
        {"status": "published"},
        {"video_key": "projects/x/y.mp4"},
        {"orientation": "square"},
        {"quality": "ultra"},
        {"title": "   "},
    ):
        response = await role_client.post(
            url, json=render_body(source.project, source.script, **extra), headers=h(admin)
        )
        assert response.status_code == 422, extra


async def test_nothing_can_be_published_into_an_archived_course(
    role_client, admin, source, track, storage
):
    course = await make_course(role_client, admin)
    await role_client.post(f"{BASE}/courses/{course['id']}/archive", headers=h(admin))
    bucket = get_settings().BUCKET_OUTPUTS
    before = await list_keys(storage, bucket, "training/modules/")

    response = await publish(role_client, track, admin, course["id"], source)

    assert response.status_code == 409
    assert await list_keys(storage, bucket, "training/modules/") == before


# --- listing, reading, previewing ---


async def test_modules_list_includes_unpublished_ones_and_get_returns_one(
    role_client, admin, source, track
):
    course = await make_course(role_client, admin)
    module = track.module(await publish(role_client, track, admin, course["id"], source))
    await role_client.post(f"{BASE}/modules/{module['id']}/unpublish", headers=h(admin))

    listed = await role_client.get(f"{BASE}/courses/{course['id']}/modules", headers=admin.headers)
    got = await role_client.get(f"{BASE}/modules/{module['id']}", headers=admin.headers)

    assert [(m["id"], m["status"]) for m in listed.json()["items"]] == [
        (module["id"], "unpublished")
    ]
    assert got.status_code == 200 and got.json()["status"] == "unpublished"
    assert got.json()["unpublished_at"] is not None
    course_after = await role_client.get(f"{BASE}/courses/{course['id']}", headers=admin.headers)
    assert course_after.json()["module_count"] == 1
    assert course_after.json()["published_module_count"] == 0


async def test_the_preview_streams_the_modules_own_copy_with_range_support(
    role_client, admin, source, track
):
    course = await make_course(role_client, admin)
    module = track.module(await publish(role_client, track, admin, course["id"], source))
    url = f"{BASE}/modules/{module['id']}/video"

    full = await role_client.get(url, headers=admin.headers)
    assert full.status_code == 200 and full.headers["content-type"] == "video/mp4"
    assert full.headers["accept-ranges"] == "bytes" and full.content == source.body

    part = await role_client.get(url, headers={**admin.headers, "Range": "bytes=2-11"})
    assert part.status_code == 206 and part.content == source.body[2:12]
    assert part.headers["content-range"] == f"bytes 2-11/{len(source.body)}"

    beyond = await role_client.get(url, headers={**admin.headers, "Range": "bytes=999999-"})
    assert beyond.status_code == 416
    assert beyond.headers["content-range"] == f"bytes */{len(source.body)}"

    download = await role_client.get(url + "?download=true", headers=admin.headers)
    assert "attachment" in download.headers["content-disposition"]


async def test_the_preview_serves_the_snapshot_even_if_the_source_render_object_is_gone(
    role_client, admin, source, track, storage
):
    course = await make_course(role_client, admin)
    module = track.module(await publish(role_client, track, admin, course["id"], source))
    await storage.delete_object(bucket=source.output.storage_bucket, key=source.output.storage_key)

    response = await role_client.get(f"{BASE}/modules/{module['id']}/video", headers=admin.headers)

    assert response.status_code == 200 and response.content == source.body


async def test_the_preview_of_an_unknown_module_and_of_an_unpublished_one_are_404s(
    role_client, admin, source, track
):
    course = await make_course(role_client, admin)
    module = track.module(await publish(role_client, track, admin, course["id"], source))
    await role_client.post(f"{BASE}/modules/{module['id']}/unpublish", headers=h(admin))

    missing = await role_client.get(f"{BASE}/modules/{NIL_ID}/video", headers=admin.headers)
    unpublished = await role_client.get(
        f"{BASE}/modules/{module['id']}/video", headers=admin.headers
    )

    assert missing.status_code == 404 and missing.json()["error"]["message"] == "Module not found."
    assert unpublished.status_code == 404


async def test_the_database_refuses_a_module_key_that_is_not_the_modules_own(
    role_client, admin, source, track, db_session
):
    """A module cannot be pointed at a key outside its own prefix (e.g. the source render's):
    ``ck_training_modules_video_key_is_module_owned`` refuses it."""
    course = await make_course(role_client, admin)
    module = track.module(await publish(role_client, track, admin, course["id"], source))
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE training_modules SET video_key = :k WHERE id = :i"),
                {"k": source.output.storage_key, "i": module["id"]},
            )


# --- unpublish, republish, delete ---


async def test_unpublishing_removes_the_file_and_keeps_the_module_and_its_place(
    role_client, admin, source, track, db_session, storage
):
    course = await make_course(role_client, admin)
    module = track.module(await publish(role_client, track, admin, course["id"], source))
    key = await key_of(db_session, module["id"])
    bucket = get_settings().BUCKET_OUTPUTS

    response = await role_client.post(f"{BASE}/modules/{module['id']}/unpublish", headers=h(admin))

    assert response.status_code == 200 and response.json()["status"] == "unpublished"
    assert response.json()["position"] == 1 and response.json()["title"] == module["title"]
    assert response.json()["video_byte_size"] == len(source.body)  # descriptive snapshot kept
    assert not await object_exists(storage, bucket, key)
    assert await key_of(db_session, module["id"]) is None
    again = await role_client.post(f"{BASE}/modules/{module['id']}/unpublish", headers=h(admin))
    assert again.status_code == 409


async def test_republishing_writes_a_new_key_and_removes_the_old_object_afterwards(
    role_client, admin, source, track, db_session, storage
):
    course = await make_course(role_client, admin)
    module = track.module(await publish(role_client, track, admin, course["id"], source))
    old_key = await key_of(db_session, module["id"])
    bucket = get_settings().BUCKET_OUTPUTS
    # A re-render of the source: new bytes at the same render key, same render row.
    output2, body2 = await add_fake_render(
        db_session, storage, project=source.project, script=source.script
    )
    assert output2.id == source.output.id and body2 != source.body

    response = await role_client.post(
        f"{BASE}/modules/{module['id']}/republish",
        json=render_body(source.project, source.script),
        headers=h(admin),
    )

    assert response.status_code == 200, response.text
    new_key = await key_of(db_session, module["id"])
    assert new_key != old_key and belongs_to_module(new_key, UUID(module["id"]))
    assert response.json()["video_byte_size"] == len(body2)
    assert response.json()["position"] == 1 and response.json()["title"] == module["title"]
    assert not await object_exists(storage, bucket, old_key)
    assert await object_bytes(storage, bucket, new_key) == body2
    stream = await role_client.get(f"{BASE}/modules/{module['id']}/video", headers=admin.headers)
    assert stream.content == body2


async def test_republishing_an_unpublished_module_publishes_it_again(
    role_client, admin, source, track, db_session, storage
):
    course = await make_course(role_client, admin)
    module = track.module(await publish(role_client, track, admin, course["id"], source))
    await role_client.post(f"{BASE}/modules/{module['id']}/unpublish", headers=h(admin))

    response = await role_client.post(
        f"{BASE}/modules/{module['id']}/republish",
        json=render_body(source.project, source.script),
        headers=h(admin),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "published" and response.json()["unpublished_at"] is None
    assert await key_of(db_session, module["id"]) is not None


async def test_republishing_a_render_that_is_another_modules_source_is_a_409(
    role_client, admin, source, track, db_session, storage
):
    course = await make_course(role_client, admin)
    await extra_output(db_session, storage, source, track, "horizontal", "high")
    first = track.module(await publish(role_client, track, admin, course["id"], source))
    second = track.module(
        await publish(role_client, track, admin, course["id"], source, quality="high")
    )

    response = await role_client.post(
        f"{BASE}/modules/{second['id']}/republish",
        json=render_body(source.project, source.script),  # low: already module `first`'s
        headers=h(admin),
    )

    assert response.status_code == 409
    assert first["id"] != second["id"]


async def test_deleting_a_module_removes_its_file_and_closes_the_gap(
    role_client, admin, source, track, db_session, storage
):
    course = await make_course(role_client, admin)
    await extra_output(db_session, storage, source, track, "horizontal", "high")
    await extra_output(db_session, storage, source, track, "vertical", "low")
    m1 = track.module(await publish(role_client, track, admin, course["id"], source))
    m2 = track.module(
        await publish(role_client, track, admin, course["id"], source, quality="high")
    )
    m3 = track.module(
        await publish(role_client, track, admin, course["id"], source, orientation="vertical")
    )
    key = await key_of(db_session, m2["id"])
    bucket = get_settings().BUCKET_OUTPUTS

    deleted = await role_client.delete(f"{BASE}/modules/{m2['id']}", headers=h(admin))

    assert deleted.status_code == 204
    assert not await object_exists(storage, bucket, key)
    assert await positions(role_client, admin, course["id"]) == [
        (m1["id"], 1, "published"),
        (m3["id"], 2, "published"),
    ]
    gone = await role_client.get(f"{BASE}/modules/{m2['id']}", headers=admin.headers)
    assert gone.status_code == 404


# --- reordering ---


async def _three_modules(role_client, admin, source, track, db_session, storage):
    course = await make_course(role_client, admin)
    await extra_output(db_session, storage, source, track, "horizontal", "high")
    await extra_output(db_session, storage, source, track, "vertical", "low")
    ids = [
        track.module(await publish(role_client, track, admin, course["id"], source))["id"],
        track.module(
            await publish(role_client, track, admin, course["id"], source, quality="high")
        )["id"],
        track.module(
            await publish(role_client, track, admin, course["id"], source, orientation="vertical")
        )["id"],
    ]
    return course, ids


async def test_reordering_three_modules_gives_contiguous_unique_positions_each_time(
    role_client, admin, source, track, db_session, storage
):
    course, [a, b, c] = await _three_modules(role_client, admin, source, track, db_session, storage)
    url = f"{BASE}/courses/{course['id']}/modules/order"

    for order in ([c, a, b], [b, c, a], [a, b, c], [c, b, a], [c, b, a]):
        response = await role_client.put(url, json={"module_ids": order}, headers=h(admin))
        assert response.status_code == 200, response.text
        assert [(m["id"], m["position"]) for m in response.json()["items"]] == [
            (order[0], 1),
            (order[1], 2),
            (order[2], 3),
        ]
        stored = await db_session.execute(
            text("SELECT position FROM training_modules WHERE course_id = :c ORDER BY position"),
            {"c": course["id"]},
        )
        assert [row[0] for row in stored] == [1, 2, 3]


async def test_a_reorder_must_name_every_module_of_the_course_exactly_once(
    role_client, admin, source, track, db_session, storage
):
    course, [a, b, c] = await _three_modules(role_client, admin, source, track, db_session, storage)
    other = await make_course(role_client, admin, "Other")
    url = f"{BASE}/courses/{course['id']}/modules/order"

    for bad in ([a, b], [a, b, c, NIL_ID], [a, b, NIL_ID]):
        response = await role_client.put(url, json={"module_ids": bad}, headers=h(admin))
        assert response.status_code == 409, bad  # not this course's modules: it changed
    repeated = await role_client.put(url, json={"module_ids": [a, b, c, a]}, headers=h(admin))
    assert repeated.status_code == 422  # a malformed body, not a stale list
    assert repeated.json()["error"]["details"]["field_errors"][0]["path"] == "body.module_ids"
    empty = await role_client.put(url, json={"module_ids": []}, headers=h(admin))
    assert empty.status_code == 422
    assert [m[:2] for m in await positions(role_client, admin, course["id"])] == [
        (a, 1),
        (b, 2),
        (c, 3),
    ]
    assert other["id"] != course["id"]


async def test_unpublished_modules_keep_their_place_when_reordering(
    role_client, admin, source, track, db_session, storage
):
    course, [a, b, c] = await _three_modules(role_client, admin, source, track, db_session, storage)
    await role_client.post(f"{BASE}/modules/{b}/unpublish", headers=h(admin))

    response = await role_client.put(
        f"{BASE}/courses/{course['id']}/modules/order",
        json={"module_ids": [b, c, a]},
        headers=h(admin),
    )

    assert response.status_code == 200
    assert [(m["id"], m["position"], m["status"]) for m in response.json()["items"]] == [
        (b, 1, "unpublished"),
        (c, 2, "published"),
        (a, 3, "published"),
    ]


async def test_an_archived_course_cannot_be_reordered(
    role_client, admin, source, track, db_session, storage
):
    course, [a, b, c] = await _three_modules(role_client, admin, source, track, db_session, storage)
    await role_client.post(f"{BASE}/courses/{course['id']}/archive", headers=h(admin))

    response = await role_client.put(
        f"{BASE}/courses/{course['id']}/modules/order",
        json={"module_ids": [c, b, a]},
        headers=h(admin),
    )

    assert response.status_code == 409
    assert [m[:2] for m in await positions(role_client, admin, course["id"])] == [
        (a, 1),
        (b, 2),
        (c, 3),
    ]
