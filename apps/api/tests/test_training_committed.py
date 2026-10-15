"""Training modules against a database that really commits.

The other API tests run inside a rolled-back transaction, which cannot show three things this
piece depends on: that a **deferred** unique constraint holds at a real ``COMMIT``, that a file is
removed only **after** the rows are visible to another connection, and that a failed commit leaves
the file in place. So these tests use their own engine, create their own QA user and project
(``qa-training-*@example.test``), and :func:`purge_qa` removes exactly what they made.

**They run on a scratch database, never the development one** (``conftest.committed_database_url``:
created and migrated for the test session, dropped at its end). What they commit includes
``audit_logs`` rows, which are append-only and cannot be removed afterwards; on the development
database that grew the table from 301 to 1,336 rows. Nothing in the development database is read,
changed or deleted, and the stored videos they create are removed by :func:`purge_qa`.

Source videos are small stand-in objects, except where the test is about a *re-render*
(``test_a_rerender_of_the_source_does_not_touch_the_published_module``): that one drives the real
Pillow + FFmpeg renderer and changes ``RENDER_FADE_IN_MS`` for one job, restoring it afterwards.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from edutoon.core.config import get_settings
from edutoon.core.errors import ConflictError
from edutoon.db.session import get_session
from edutoon.domain.training_storage import module_storage_prefix
from edutoon.main import create_app
from edutoon.providers.cache import get_redis_client
from edutoon.providers.renderer import PillowFfmpegRenderer
from edutoon.providers.storage import Storage, get_storage_client
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import render as render_repo
from edutoon.repositories import training as training_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.projects import ProjectRecord
from edutoon.repositories.render import RenderOutputRecord
from edutoon.repositories.scripts import ScriptRecord
from edutoon.repositories.training import VideoSnapshot
from edutoon.services import render as render_service
from edutoon.services import training as training_service
from tests.render_helpers import build_project, requires_ffmpeg
from tests.training_helpers import (
    QaRegistry,
    RecordingLog,
    add_fake_render,
    add_script,
    list_keys,
    object_bytes,
    object_exists,
    purge_qa,
    render_body,
    sha256_of,
)

pytestmark = pytest.mark.real_auth

BASE = "/v1/admin/training"
BUCKET = get_settings().BUCKET_OUTPUTS


class SpyStorage:
    """The real storage, watching ``delete_object``: it records what the database looked like to a
    *different* connection at the moment of each deletion, and can be told to fail for a key."""

    def __init__(self, real: Storage, sessionmaker, probe: Callable[[AsyncSession], Any]) -> None:
        self._real = real
        self._sessionmaker = sessionmaker
        self._probe = probe
        self.deletions: list[tuple[str, Any]] = []
        self.fail_on: set[str] = set()
        self.copies: list[str] = []
        self.truncate_copies = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def copy_object(self, **kwargs: Any) -> None:
        self.copies.append(kwargs["dest_key"])
        await self._real.copy_object(**kwargs)
        if self.truncate_copies:  # simulate a copy that did not complete
            await self._real.put_object(
                bucket=kwargs["dest_bucket"],
                key=kwargs["dest_key"],
                body=b"short",
                content_type=kwargs["content_type"],
            )

    async def delete_object(self, *, bucket: str, key: str) -> None:
        async with self._sessionmaker() as other:  # a separate connection: sees only commits
            seen = await self._probe(other)
        self.deletions.append((key, seen))
        if key in self.fail_on:
            raise RuntimeError("simulated storage outage")
        await self._real.delete_object(bucket=bucket, key=key)


@dataclass
class World:
    sessionmaker: async_sessionmaker[AsyncSession]
    storage: Storage
    spy: SpyStorage
    registry: QaRegistry
    admin: Any
    employee: Any
    project: ProjectRecord
    script: ScriptRecord
    app: Any
    fail_commit: bool = False
    statements: list[str] = field(default_factory=list)

    def client(self, **kwargs: Any) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app, **kwargs)
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    def h(self, person: Any = None, key: str | None = None) -> dict[str, str]:
        return {**(person or self.admin).headers, "Idempotency-Key": key or f"k-{uuid4()}"}

    async def fake_render(
        self, orientation: str = "horizontal", quality: str = "low", *, project=None, script=None
    ) -> tuple[RenderOutputRecord, bytes]:
        async with self.sessionmaker() as session:
            output, body = await add_fake_render(
                session,
                self.storage,
                project=project or self.project,
                script=script or self.script,
                orientation=orientation,
                quality=quality,
            )
            await session.commit()
        return output, body

    async def scalar(self, sql: str, **params: Any) -> Any:
        async with self.sessionmaker() as session:
            return (await session.execute(text(sql), params)).scalar_one_or_none()

    async def rows(self, sql: str, **params: Any) -> list[Any]:
        async with self.sessionmaker() as session:
            return list((await session.execute(text(sql), params)).all())

    async def module_key(self, module_id: str) -> str | None:
        return await self.scalar(
            "SELECT video_key FROM training_modules WHERE id = :i", i=module_id
        )

    async def make_course(self, client: httpx.AsyncClient, title: str = "QA course") -> str:
        response = await client.post(f"{BASE}/courses", json={"title": title}, headers=self.h())
        assert response.status_code == 201, response.text
        return response.json()["id"]

    async def publish(
        self, client: httpx.AsyncClient, course_id: str, **extra: Any
    ) -> httpx.Response:
        return await client.post(
            f"{BASE}/courses/{course_id}/modules",
            json=render_body(self.project, self.script, **extra),
            headers=self.h(),
        )

    async def positions(self, course_id: str) -> list[tuple[str, int]]:
        rows = await self.rows(
            "SELECT id::text, position FROM training_modules "
            "WHERE course_id = :c ORDER BY position",
            c=course_id,
        )
        return [(row[0], row[1]) for row in rows]


async def _make_person(
    sessionmaker, registry: QaRegistry, mint_token, *, role: str, label: str
) -> Any:
    from tests.role_fixtures import Person

    sub, email = f"clerk_{uuid4()}", f"qa-training-{label}-{uuid4().hex[:10]}@example.test"
    async with sessionmaker() as session:
        user = await users_repo.create(
            session, email=email, role=role, clerk_user_id=sub, display_name="QA"
        )
        await session.commit()
    registry.user_ids.append(user.id)
    return Person(user, {"Authorization": f"Bearer {mint_token(sub=sub, email=email)}"}, email, sub)


@pytest.fixture
async def world(mint_token, committed_database_url) -> AsyncIterator[World]:
    settings = get_settings()
    # These tests commit, and what they commit includes append-only audit rows, so they run on a
    # database of their own (conftest.committed_database_url) - never on the development one.
    assert (
        make_url(committed_database_url).database != make_url(settings.DATABASE_DIRECT_URL).database
    ), "committed tests must not run against the development database"
    engine = create_async_engine(committed_database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    registry = QaRegistry()
    real_storage = get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )
    redis = get_redis_client(settings.REDIS_URL)
    holder: dict[str, Any] = {}
    try:
        admin = await _make_person(sessionmaker, registry, mint_token, role="admin", label="admin")
        employee = await _make_person(
            sessionmaker, registry, mint_token, role="employee", label="emp"
        )
        async with sessionmaker() as session:
            project = await projects_repo.create(
                session, owner_id=admin.user.id, title="QA training source", source_type="topic"
            )
            script = await add_script(session, project)
            await session.commit()
        registry.project_ids.append(project.id)

        app = create_app()

        async def _session() -> AsyncIterator[AsyncSession]:
            async with sessionmaker() as session:
                if holder["world"].fail_commit:

                    async def refuse() -> None:
                        raise RuntimeError("simulated commit failure")

                    session.commit = refuse  # type: ignore[method-assign]
                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise

        async def probe_nothing(session: AsyncSession) -> Any:
            return None

        spy = SpyStorage(real_storage, sessionmaker, probe_nothing)
        app.dependency_overrides[get_session] = _session
        app.state.redis = redis
        app.state.storage = spy

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            holder["world"].statements.append(statement)

        w = World(sessionmaker, real_storage, spy, registry, admin, employee, project, script, app)
        holder["world"] = w
        yield w
    finally:
        # Keep only the QA modules' own files in the registry's care; purge_qa adds the rest.
        await purge_qa(sessionmaker, real_storage, registry)
        await redis.aclose()
        await engine.dispose()


# --- D: delete, roll back, and storage clean-up ---


async def test_deleting_a_module_removes_its_file_only_after_the_delete_is_committed(world):
    async with world.client() as client:
        await world.fake_render()
        course = await world.make_course(client)
        module = (await world.publish(client, course)).json()
        key = await world.module_key(module["id"])
        assert await object_exists(world.storage, BUCKET, key)

        async def row_still_visible(other: AsyncSession) -> bool:
            row = await other.execute(
                text("SELECT 1 FROM training_modules WHERE id = :i"), {"i": module["id"]}
            )
            return row.first() is not None

        world.spy._probe = row_still_visible  # noqa: SLF001
        response = await client.delete(f"{BASE}/modules/{module['id']}", headers=world.h())

    assert response.status_code == 204
    assert world.spy.deletions == [(key, False)]  # when the file went, the row was already gone
    assert not await object_exists(world.storage, BUCKET, key)
    assert await world.module_key(module["id"]) is None


async def test_a_delete_whose_commit_fails_leaves_the_row_and_the_file_in_place(world):
    async with world.client(raise_app_exceptions=False) as client:
        await world.fake_render()
        course = await world.make_course(client)
        module = (await world.publish(client, course)).json()
        key = await world.module_key(module["id"])

        world.fail_commit = True
        response = await client.delete(f"{BASE}/modules/{module['id']}", headers=world.h())
        world.fail_commit = False

    assert response.status_code == 500
    assert world.spy.deletions == []  # not a single deletion was even attempted
    assert await world.module_key(module["id"]) == key  # the row survived the rollback
    assert await object_exists(world.storage, BUCKET, key)
    assert await world.positions(course) == [(module["id"], 1)]


async def test_an_unpublish_whose_commit_fails_keeps_the_module_published_with_its_file(world):
    async with world.client(raise_app_exceptions=False) as client:
        await world.fake_render()
        course = await world.make_course(client)
        module = (await world.publish(client, course)).json()
        key = await world.module_key(module["id"])

        world.fail_commit = True
        response = await client.post(f"{BASE}/modules/{module['id']}/unpublish", headers=world.h())
        world.fail_commit = False

    assert response.status_code == 500
    assert (
        await world.scalar("SELECT status FROM training_modules WHERE id = :i", i=module["id"])
        == "published"
    )
    assert await object_exists(world.storage, BUCKET, key)
    assert world.spy.deletions == []


async def test_a_publish_whose_commit_fails_removes_the_copy_it_made(world):
    async with world.client(raise_app_exceptions=False) as client:
        await world.fake_render()
        course = await world.make_course(client)

        world.fail_commit = True
        response = await world.publish(client, course)
        world.fail_commit = False

    assert response.status_code == 500
    # Scoped to what this request made: the one copy it wrote is gone (a listing of the whole
    # ``training/modules/`` prefix would also see anything else running against the bucket).
    [copied_key] = world.spy.copies
    assert not await object_exists(world.storage, BUCKET, copied_key)
    assert await world.positions(course) == []
    assert [key for key, _ in world.spy.deletions] == [copied_key]  # exactly the one new copy


async def test_a_republish_whose_commit_fails_keeps_the_old_file_and_removes_the_new_copy(world):
    async with world.client(raise_app_exceptions=False) as client:
        await world.fake_render()
        course = await world.make_course(client)
        module = (await world.publish(client, course)).json()
        old_key = await world.module_key(module["id"])
        keys_before = await list_keys(
            world.storage, BUCKET, module_storage_prefix(UUID(module["id"]))
        )

        world.fail_commit = True
        response = await client.post(
            f"{BASE}/modules/{module['id']}/republish",
            json=render_body(world.project, world.script),
            headers=world.h(),
        )
        world.fail_commit = False

    assert response.status_code == 500
    assert await world.module_key(module["id"]) == old_key
    assert (
        await list_keys(world.storage, BUCKET, module_storage_prefix(UUID(module["id"])))
        == keys_before
    )
    assert [key for key, _ in world.spy.deletions] != [old_key]  # the old one was never touched
    assert await object_exists(world.storage, BUCKET, old_key)


async def test_a_storage_failure_after_the_commit_does_not_undo_it_and_the_orphan_is_logged(
    world, monkeypatch
):
    logs = RecordingLog()
    monkeypatch.setattr(training_service, "log", logs)
    async with world.client() as client:
        await world.fake_render()
        course = await world.make_course(client)
        module = (await world.publish(client, course)).json()
        key = await world.module_key(module["id"])
        world.spy.fail_on.add(key)
        world.registry.storage_keys.append((BUCKET, key))  # it will be left behind; purge it

        response = await client.delete(f"{BASE}/modules/{module['id']}", headers=world.h())

    assert response.status_code == 204  # the request still succeeded
    assert await world.module_key(module["id"]) is None  # the database change stands
    [entry] = [e for e in logs.entries if e["event"] == "training.object_cleanup_failed"]
    assert entry["orphaned"] is True and entry["key"] == key and entry["bucket"] == BUCKET
    assert await object_exists(world.storage, BUCKET, key)  # the orphan is really still there


# --- C: republish ---


async def test_republishing_uses_a_new_key_and_removes_the_old_object_only_after_the_commit(world):
    async with world.client() as client:
        output, first_bytes = await world.fake_render()
        course = await world.make_course(client)
        module = (await world.publish(client, course)).json()
        old_key = await world.module_key(module["id"])
        _, second_bytes = await world.fake_render()  # the source is re-rendered: new bytes
        assert second_bytes != first_bytes

        async def current_key(other: AsyncSession) -> str | None:
            row = await other.execute(
                text("SELECT video_key FROM training_modules WHERE id = :i"), {"i": module["id"]}
            )
            return row.scalar_one_or_none()

        world.spy._probe = current_key  # noqa: SLF001
        response = await client.post(
            f"{BASE}/modules/{module['id']}/republish",
            json=render_body(world.project, world.script),
            headers=world.h(),
        )
        new_key = await world.module_key(module["id"])
        streamed = await client.get(
            f"{BASE}/modules/{module['id']}/video", headers=world.admin.headers
        )

    assert response.status_code == 200, response.text
    assert new_key != old_key
    # At the moment the old object was deleted, another connection already saw the NEW key.
    assert world.spy.deletions == [(old_key, new_key)]
    assert not await object_exists(world.storage, BUCKET, old_key)
    assert await object_bytes(world.storage, BUCKET, new_key) == second_bytes
    assert streamed.status_code == 200 and streamed.content == second_bytes
    assert response.json()["video_output_hash"] == sha256_of(second_bytes)
    assert output.id == UUID(response.json()["source_render_output_id"])


# --- B: deleting the source ---


async def test_deleting_the_source_project_leaves_the_published_module_playing(world):
    async with world.client() as client:
        output, body = await world.fake_render()
        course = await world.make_course(client)
        module = (await world.publish(client, course)).json()
        key = await world.module_key(module["id"])
        assert module["source_render_output_id"] == str(output.id)

        deleted = await client.delete(f"/v1/projects/{world.project.id}", headers=world.h())
        streamed = await client.get(
            f"{BASE}/modules/{module['id']}/video", headers=world.admin.headers
        )
        got = await client.get(f"{BASE}/modules/{module['id']}", headers=world.admin.headers)

    assert deleted.status_code == 204
    # the project, its render record and its render object are gone ...
    assert await world.scalar("SELECT count(*) FROM render_outputs WHERE id = :i", i=output.id) == 0
    assert not await object_exists(world.storage, output.storage_bucket, output.storage_key)
    # ... the module's provenance link was cleared by the foreign key (and nothing else) ...
    assert got.status_code == 200 and got.json()["source_render_output_id"] is None
    assert (
        await world.scalar(
            "SELECT source_render_output_id FROM training_modules WHERE id = :i", i=module["id"]
        )
        is None
    )
    # ... and it plays its own copy, which is a different object from the source's.
    assert streamed.status_code == 200 and streamed.content == body
    assert await world.module_key(module["id"]) == key and key != output.storage_key
    assert await object_exists(world.storage, BUCKET, key)


# --- A: a re-render of the source must not change the published module ---


@requires_ffmpeg
async def test_a_rerender_of_the_source_does_not_touch_the_published_module(world, monkeypatch):
    settings_before = os.environ.get("RENDER_FADE_IN_MS")
    original_fade = get_settings().RENDER_FADE_IN_MS
    storage = world.storage
    renderer = PillowFfmpegRenderer()

    async def render_now(session: AsyncSession) -> RenderOutputRecord:
        enqueued = await render_service.enqueue_render(
            session,
            project_id=world.project.id,
            owner_id=world.admin.user.id,
            script_id=world.script.id,
            orientation="horizontal",
            quality="low",
            idempotency_key=f"qa-{uuid4()}",
        )
        await session.commit()
        job = await render_service.run_render_video_job(
            session, storage, renderer, job_id=enqueued.job.id, sessionmaker=world.sessionmaker
        )
        await session.commit()  # the job's final state is written through this session
        assert job.status == "succeeded", job.error
        output = await render_repo.get_output(
            session,
            project_id=world.project.id,
            script_id=world.script.id,
            orientation="horizontal",
            quality="low",
        )
        assert output is not None
        return output

    # A real project whose script is the QA script: give it narration, scenes and a storyboard.
    async with world.sessionmaker() as session:
        # The fixture's bare script is replaced by one with narration, scenes and a storyboard
        # (a project may have only one active script).
        await session.execute(text("DELETE FROM scripts WHERE id = :s"), {"s": world.script.id})
        built = await build_project(
            session,
            storage,
            layout=[["title_card"]],
            durations_ms=[2_000],
            project=world.project,
        )
        await session.commit()
    world.registry.storage_keys.extend(built.storage_keys)
    world.script = built.script

    async with world.sessionmaker() as session:
        first = await render_now(session)
    source_before = await object_bytes(storage, first.storage_bucket, first.storage_key)

    async with world.client() as client:
        course = await world.make_course(client)
        module = (await world.publish(client, course)).json()
        key = await world.module_key(module["id"])
        snapshot_bytes = await object_bytes(storage, BUCKET, key)
        assert sha256_of(snapshot_bytes) == sha256_of(source_before)  # a faithful copy
        recorded = {
            k: module[k] for k in ("video_byte_size", "video_duration_ms", "video_output_hash")
        }
        other_jobs_before = await world.rows(
            "SELECT id, status FROM jobs WHERE project_id <> :p ORDER BY id", p=world.project.id
        )

        os.environ["RENDER_FADE_IN_MS"] = str(original_fade + 600)
        get_settings.cache_clear()
        try:
            assert original_fade + 600 == get_settings().RENDER_FADE_IN_MS
            async with world.sessionmaker() as session:
                second = await render_now(session)  # only this one job is run by hand
        finally:
            if settings_before is None:
                os.environ.pop("RENDER_FADE_IN_MS", None)
            else:
                os.environ["RENDER_FADE_IN_MS"] = settings_before
            get_settings.cache_clear()
        assert original_fade == get_settings().RENDER_FADE_IN_MS  # restored

        source_after = await object_bytes(storage, second.storage_bucket, second.storage_key)
        # The re-render is real: the source object's bytes (and so its hash) changed.
        assert sha256_of(source_after) != sha256_of(source_before)
        assert second.output_hash != first.output_hash
        assert second.id == first.id and second.storage_key == first.storage_key

        # The module did not move: same key, same bytes, same size, duration and fingerprint.
        assert await world.module_key(module["id"]) == key
        assert sha256_of(await object_bytes(storage, BUCKET, key)) == sha256_of(snapshot_bytes)
        again = (await client.get(f"{BASE}/modules/{module['id']}", headers=world.h())).json()
        assert {k: again[k] for k in recorded} == recorded
        assert again["video_output_hash"] == first.output_hash != second.output_hash
        streamed = await client.get(
            f"{BASE}/modules/{module['id']}/video", headers=world.admin.headers
        )
        assert streamed.content == snapshot_bytes != source_after

    assert (
        await world.rows(
            "SELECT id, status FROM jobs WHERE project_id <> :p ORDER BY id", p=world.project.id
        )
        == other_jobs_before
    )  # no unrelated job was touched while the setting was changed


# --- E: reordering across real commits ---


async def test_reordering_three_modules_is_one_statement_and_contiguous_at_every_commit(world):
    async with world.client() as client:
        for orientation, quality in (("horizontal", "high"), ("vertical", "low")):
            await world.fake_render(orientation, quality)
        await world.fake_render()
        course = await world.make_course(client)
        a = (await world.publish(client, course)).json()["id"]
        b = (await world.publish(client, course, quality="high")).json()["id"]
        c = (await world.publish(client, course, orientation="vertical")).json()["id"]
        assert await world.positions(course) == [(a, 1), (b, 2), (c, 3)]

        for order in ([c, a, b], [b, c, a], [a, b, c], [c, b, a]):
            world.statements.clear()
            response = await client.put(
                f"{BASE}/courses/{course}/modules/order",
                json={"module_ids": order},
                headers=world.h(),
            )
            assert response.status_code == 200, response.text
            # what a different connection sees after the commit: 1..3, unique, in the new order
            assert await world.positions(course) == [(order[0], 1), (order[1], 2), (order[2], 3)]
            updates = [
                s
                for s in world.statements
                if s.lstrip().upper().startswith("UPDATE TRAINING_MODULES")
            ]
            assert len(updates) == 1, updates  # one atomic statement ...
            assert all(
                "-" not in s.split("SET", 1)[1].split("WHERE")[0] for s in updates
            )  # ... no negatives
            assert not any(
                s.lstrip().upper().startswith(("DELETE", "INSERT"))
                for s in world.statements
                if "training_modules" in s
            )  # ... and no delete / re-insert


async def test_the_position_constraint_is_deferrable_and_still_rejects_duplicates_at_commit(world):
    async with world.client() as client:
        await world.fake_render()
        await world.fake_render("horizontal", "high")
        course = await world.make_course(client)
        a = (await world.publish(client, course)).json()["id"]
        b = (await world.publish(client, course, quality="high")).json()["id"]

    [(deferrable, deferred)] = await world.rows(
        "SELECT condeferrable, condeferred FROM pg_constraint "
        "WHERE conname = 'uq_training_modules_course_id_position'"
    )
    assert (deferrable, deferred) == (True, True)

    # Two plain single-row UPDATEs swap the positions inside one transaction: allowed only because
    # the check waits for COMMIT.
    async with world.sessionmaker() as session:
        await session.execute(
            text("UPDATE training_modules SET position = 2 WHERE id = :a"), {"a": a}
        )
        await session.execute(
            text("UPDATE training_modules SET position = 1 WHERE id = :b"), {"b": b}
        )
        await session.commit()
    assert await world.positions(course) == [(b, 1), (a, 2)]

    # ... but a transaction that ends with two modules in one place is refused at COMMIT.
    async with world.sessionmaker() as session:
        await session.execute(
            text("UPDATE training_modules SET position = 1 WHERE id = :a"), {"a": a}
        )
        with pytest.raises(IntegrityError) as raised:
            await session.commit()
    assert "uq_training_modules_course_id_position" in str(raised.value)
    assert await world.positions(course) == [(b, 1), (a, 2)]  # nothing changed


async def test_concurrent_publishes_into_one_course_get_distinct_contiguous_positions(world):
    import asyncio

    async with world.client() as client:
        for orientation, quality in (
            ("horizontal", "high"),
            ("vertical", "low"),
            ("vertical", "high"),
        ):
            await world.fake_render(orientation, quality)
        await world.fake_render()
        course = await world.make_course(client)
        responses = await asyncio.gather(
            world.publish(client, course),
            world.publish(client, course, quality="high"),
            world.publish(client, course, orientation="vertical"),
            world.publish(client, course, orientation="vertical", quality="high"),
        )

    assert [r.status_code for r in responses] == [201, 201, 201, 201], [r.text for r in responses]
    assert sorted(p for _, p in await world.positions(course)) == [1, 2, 3, 4]


# --- G: the database, not just the service, refuses a duplicate publication ---


async def test_the_database_rejects_the_same_render_twice_in_one_course(world):
    output, _ = await world.fake_render()
    async with world.client() as client:
        course = await world.make_course(client)

    def snapshot(key: str) -> VideoSnapshot:
        return VideoSnapshot(BUCKET, key, 10, 1000, 640, 360, 30, "h", output.id)

    async with world.sessionmaker() as session:
        first_id = await training_repo.new_module_id(session)
        await training_repo.insert_module(
            session,
            module_id=first_id,
            course_id=UUID(course),
            position=1,
            title="One",
            description=None,
            snapshot=snapshot(f"training/modules/{first_id}/{uuid4()}.mp4"),
            published_by=world.admin.user.id,
        )
        second_id = await training_repo.new_module_id(session)
        # The repository maps the database's unique-violation to a ConflictError.
        with pytest.raises(ConflictError, match="already published in this course"):
            await training_repo.insert_module(
                session,
                module_id=second_id,
                course_id=UUID(course),
                position=2,
                title="Two",
                description=None,
                snapshot=snapshot(f"training/modules/{second_id}/{uuid4()}.mp4"),
                published_by=world.admin.user.id,
            )
        await session.commit()

    # And the raw statement is refused by the named unique index itself.
    async with world.sessionmaker() as session:
        third_id = await training_repo.new_module_id(session)
        with pytest.raises(IntegrityError) as raised:
            await session.execute(
                text(
                    "INSERT INTO training_modules (id, course_id, position, title, status, "
                    "source_render_output_id, video_bucket, video_key, video_byte_size, "
                    "video_duration_ms, video_width, video_height, video_fps, video_output_hash, "
                    "published_by) VALUES "
                    "(:id, :c, 3, 'Three', 'published', :o, :b, :k, 1, 1, 1, 1, "
                    "1, 'h', :u)"
                ),
                {
                    "id": third_id,
                    "c": course,
                    "o": output.id,
                    "b": BUCKET,
                    "k": f"training/modules/{third_id}/x.mp4",
                    "u": world.admin.user.id,
                },
            )
        assert "uq_training_modules_course_source_render" in str(raised.value)
    assert len(await world.positions(course)) == 1


async def test_a_duplicate_publish_that_gets_past_the_service_check_is_still_a_409_and_cleans_up(
    world, monkeypatch
):
    """Remove the service-level pre-check: the request reaches the database, which refuses it,
    and the copy that was already made is removed."""
    async with world.client() as client:
        await world.fake_render()
        course = await world.make_course(client)
        first = (await world.publish(client, course)).json()
        copies_before = len(world.spy.copies)

        async def no_precheck(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(training_repo, "find_published_by_source", no_precheck)
        second = await world.publish(client, course)

    assert second.status_code == 409
    assert "already published in this course" in second.json()["error"]["message"]
    # The database caught it after the copy was made, and the extra copy was removed again.
    [extra_copy] = world.spy.copies[copies_before:]
    assert not await object_exists(world.storage, BUCKET, extra_copy)
    assert [m for m, _ in await world.positions(course)] == [first["id"]]


async def test_a_duplicate_publish_is_refused_before_anything_is_copied(world):
    async with world.client() as client:
        await world.fake_render()
        course = await world.make_course(client)
        assert (await world.publish(client, course)).status_code == 201
        assert len(world.spy.copies) == 1

        second = await world.publish(client, course)

    assert second.status_code == 409
    assert len(world.spy.copies) == 1  # the service check ran first: no second copy was attempted
    assert world.spy.deletions == []


async def test_a_copy_that_comes_out_the_wrong_size_is_refused_and_removed(world):
    async with world.client() as client:
        await world.fake_render()
        course = await world.make_course(client)

        world.spy.truncate_copies = True
        response = await world.publish(client, course)
        world.spy.truncate_copies = False

    assert response.status_code == 409
    assert "could not be copied completely" in response.json()["error"]["message"]
    [short_copy] = world.spy.copies  # this request's own copy, removed because it came out short
    assert not await object_exists(world.storage, BUCKET, short_copy)
    assert await world.positions(course) == []
