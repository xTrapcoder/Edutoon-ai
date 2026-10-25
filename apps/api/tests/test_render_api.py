"""``/v1/projects/{project_id}/scripts/{script_id}/render`` and friends - Phase 10's HTTP surface.

Same no-real-Clerk pattern as ``test_script_audio_api.py`` (tokens signed locally,
``providers.clerk.get_signing_key`` monkeypatched) and the same ``get_session`` override so
nothing persists past a test's rolled-back transaction. Storage is the real local MinIO; where
a test needs a finished video it drives ``services.render.run_render_video_job`` directly with
the real renderer, the way the other API tests drive their job runners.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from edutoon.core.config import get_settings
from edutoon.db.session import get_session
from edutoon.main import create_app
from edutoon.providers import clerk
from edutoon.providers.cache import get_redis_client
from edutoon.providers.storage import Storage, get_storage_client
from edutoon.repositories import projects as projects_repo
from edutoon.services import render as render_service
from tests.idempotency_helpers import INVALID_KEYS
from tests.render_helpers import (
    Built,
    CountingRenderer,
    build_project,
    remove_objects,
    requires_ffmpeg,
)

pytestmark = requires_ffmpeg

TEST_ISSUER = "https://test.clerk.accounts.dev"


def _generate_rsa_keypair() -> tuple[bytes, RSAPublicKey]:
    private_key: RSAPrivateKey = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem, private_key.public_key()


def _make_token(private_pem: bytes, *, sub: str, email: str) -> str:
    now = int(time.time())
    payload = {"sub": sub, "email": email, "iat": now, "exp": now + 3600, "iss": TEST_ISSUER}
    return jwt.encode(payload, private_pem, algorithm="RS256")


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[bytes, RSAPublicKey]:
    return _generate_rsa_keypair()


@pytest.fixture
def auth_headers(rsa_keypair, monkeypatch) -> Callable[[], dict[str, str]]:
    private_pem, public_key = rsa_keypair
    monkeypatch.setattr(clerk, "get_signing_key", lambda token: public_key)

    def _make() -> dict[str, str]:
        token = _make_token(private_pem, sub=f"clerk_{uuid4()}", email=f"{uuid4()}@example.com")
        return {"Authorization": f"Bearer {token}"}

    return _make


@pytest.fixture(autouse=True)
def _one_chunk_at_a_time(monkeypatch: Any) -> None:
    settings = get_settings().model_copy(update={"RENDER_CONCURRENCY": 1})
    monkeypatch.setattr(render_service, "get_settings", lambda: settings)


@pytest.fixture
def storage() -> Storage:
    settings = get_settings()
    return get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )


@pytest.fixture
async def client_factory(db_session, storage) -> AsyncIterator[Callable[[], httpx.AsyncClient]]:
    app = create_app()

    async def _session_override():
        yield db_session

    app.dependency_overrides[get_session] = _session_override
    app.state.redis = get_redis_client(get_settings().REDIS_URL)
    app.state.storage = storage

    def _make() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    yield _make
    await app.state.redis.aclose()


@pytest.fixture
async def worker_sessionmaker(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    connection = await db_session.connection()
    return async_sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


def _headers(base: dict[str, str], key: str | None = None) -> dict[str, str]:
    return {**base, "Idempotency-Key": key or str(uuid4())}


BODY = {"orientation": "horizontal", "quality": "low"}


async def _project_with_storyboard(
    client: httpx.AsyncClient, headers: dict[str, str], db_session, storage
) -> tuple[Built, str, str]:
    response = await client.post(
        "/v1/projects",
        json={"title": "How photosynthesis works", "source_type": "pdf"},
        headers=_headers(headers),
    )
    assert response.status_code == 201, response.text
    project = await projects_repo.get_by_id(db_session, UUID(response.json()["id"]))
    assert project is not None
    built = await build_project(db_session, storage, project=project)
    await db_session.commit()
    return built, str(project.id), str(built.script.id)


@pytest.fixture
async def cleanup(db_session, storage) -> AsyncIterator[list[Built]]:
    made: list[Built] = []
    yield made
    for built in made:
        await remove_objects(db_session, storage, built)


def _render_url(project_id: str, script_id: str) -> str:
    return f"/v1/projects/{project_id}/scripts/{script_id}/render"


# --- enqueue ---


async def test_enqueueing_a_render_returns_202_with_the_job_and_an_estimate(
    client_factory, auth_headers, db_session, storage, cleanup
):
    headers = auth_headers()
    async with client_factory() as client:
        built, project_id, script_id = await _project_with_storyboard(
            client, headers, db_session, storage
        )
        cleanup.append(built)

        response = await client.post(
            _render_url(project_id, script_id), json=BODY, headers=_headers(headers)
        )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["job"]["kind"] == "render_video" and body["job"]["status"] == "queued"
    assert body["estimate"]["chunk_count"] == 6
    assert body["estimate"]["chunks_to_render"] == 6
    assert (body["estimate"]["width"], body["estimate"]["height"]) == (640, 360)


async def test_a_second_render_while_one_is_queued_is_a_409(
    client_factory, auth_headers, db_session, storage, cleanup
):
    headers = auth_headers()
    async with client_factory() as client:
        built, project_id, script_id = await _project_with_storyboard(
            client, headers, db_session, storage
        )
        cleanup.append(built)
        first = await client.post(
            _render_url(project_id, script_id), json=BODY, headers=_headers(headers)
        )
        second = await client.post(
            _render_url(project_id, script_id), json=BODY, headers=_headers(headers)
        )

    assert first.status_code == 202 and second.status_code == 409, second.text
    assert "already queued or running" in second.json()["error"]["message"]


async def test_a_repeat_idempotency_key_replays_the_original_job(
    client_factory, auth_headers, db_session, storage, cleanup
):
    headers = auth_headers()
    async with client_factory() as client:
        built, project_id, script_id = await _project_with_storyboard(
            client, headers, db_session, storage
        )
        cleanup.append(built)
        request_headers = _headers(headers)
        first = await client.post(
            _render_url(project_id, script_id), json=BODY, headers=request_headers
        )
        second = await client.post(
            _render_url(project_id, script_id), json=BODY, headers=request_headers
        )

    assert first.status_code == second.status_code == 202
    assert first.json()["job"]["id"] == second.json()["job"]["id"]


async def test_enqueueing_without_an_idempotency_key_is_a_422(
    client_factory, auth_headers, db_session, storage, cleanup
):
    headers = auth_headers()
    async with client_factory() as client:
        built, project_id, script_id = await _project_with_storyboard(
            client, headers, db_session, storage
        )
        cleanup.append(built)
        response = await client.post(_render_url(project_id, script_id), json=BODY, headers=headers)
    assert response.status_code == 422


@pytest.mark.parametrize("key", INVALID_KEYS)
async def test_an_invalid_idempotency_key_is_refused(
    client_factory, auth_headers, db_session, storage, cleanup, key
):
    headers = auth_headers()
    async with client_factory() as client:
        built, project_id, script_id = await _project_with_storyboard(
            client, headers, db_session, storage
        )
        cleanup.append(built)
        response = await client.post(
            _render_url(project_id, script_id),
            json=BODY,
            headers={**headers, "Idempotency-Key": key},
        )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {**BODY, "unexpected": "field"},
        {"orientation": "square", "quality": "low"},
        {"orientation": "horizontal", "quality": "ultra"},
        {"orientation": "horizontal"},
        {},
    ],
)
async def test_an_invalid_or_unknown_body_is_a_422(
    client_factory, auth_headers, db_session, storage, cleanup, body
):
    headers = auth_headers()
    async with client_factory() as client:
        built, project_id, script_id = await _project_with_storyboard(
            client, headers, db_session, storage
        )
        cleanup.append(built)
        response = await client.post(
            _render_url(project_id, script_id), json=body, headers=_headers(headers)
        )
    assert response.status_code == 422


async def test_someone_elses_project_is_a_404_on_every_render_route(
    client_factory, auth_headers, db_session, storage, cleanup
):
    owner, intruder = auth_headers(), auth_headers()
    async with client_factory() as client:
        built, project_id, script_id = await _project_with_storyboard(
            client, owner, db_session, storage
        )
        cleanup.append(built)
        url = _render_url(project_id, script_id)
        query = "?orientation=horizontal&quality=low"

        responses = [
            await client.post(url, json=BODY, headers=_headers(intruder)),
            await client.get(url + query, headers=intruder),
            await client.get(url + "/video" + query, headers=intruder),
            await client.get(f"/v1/projects/{project_id}/render/progress", headers=intruder),
            await client.post(
                f"/v1/projects/{project_id}/render/retry", headers=_headers(intruder)
            ),
        ]
    assert [r.status_code for r in responses] == [404] * 5


async def test_a_storyboard_that_is_not_ready_is_refused_with_a_message_that_says_what_to_do(
    client_factory, auth_headers, db_session, storage
):
    headers = auth_headers()
    async with client_factory() as client:
        response = await client.post(
            "/v1/projects",
            json={"title": "T", "source_type": "pdf"},
            headers=_headers(headers),
        )
        project_id = response.json()["id"]
        from edutoon.repositories import scripts as scripts_repo

        script = await scripts_repo.create(db_session, project_id=UUID(project_id), llm_model="m")
        await scripts_repo.set_status(
            db_session, project_id=UUID(project_id), script_id=script.id, status="approved"
        )
        await db_session.commit()
        response = await client.post(
            _render_url(project_id, str(script.id)), json=BODY, headers=_headers(headers)
        )
    assert response.status_code == 400
    assert "narration" in response.json()["error"]["message"]


# --- status ---


async def test_status_lists_every_scene_and_says_nothing_is_rendered_yet(
    client_factory, auth_headers, db_session, storage, cleanup
):
    headers = auth_headers()
    async with client_factory() as client:
        built, project_id, script_id = await _project_with_storyboard(
            client, headers, db_session, storage
        )
        cleanup.append(built)
        response = await client.get(
            _render_url(project_id, script_id) + "?orientation=horizontal&quality=low",
            headers=headers,
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["scenes"]) == 6 and {s["state"] for s in body["scenes"]} == {"needs_render"}
    assert body["is_complete"] is False and body["output"] is None and body["blockers"] == []
    assert body["needs_render_count"] == 6


async def test_status_requires_a_valid_orientation_and_quality(
    client_factory, auth_headers, db_session, storage, cleanup
):
    headers = auth_headers()
    async with client_factory() as client:
        built, project_id, script_id = await _project_with_storyboard(
            client, headers, db_session, storage
        )
        cleanup.append(built)
        url = _render_url(project_id, script_id)
        assert (await client.get(url, headers=headers)).status_code == 422
        bad = await client.get(url + "?orientation=round&quality=low", headers=headers)
    assert bad.status_code == 422


# --- video streaming ---


async def _render_now(client, headers, db_session, storage, worker_sessionmaker, cleanup):
    built, project_id, script_id = await _project_with_storyboard(
        client, headers, db_session, storage
    )
    cleanup.append(built)
    response = await client.post(
        _render_url(project_id, script_id), json=BODY, headers=_headers(headers)
    )
    job = await render_service.run_render_video_job(
        db_session,
        storage,
        CountingRenderer(),
        job_id=UUID(response.json()["job"]["id"]),
        sessionmaker=worker_sessionmaker,
    )
    assert job.status == "succeeded", job.error
    return project_id, script_id


async def test_the_finished_video_streams_supports_ranges_and_downloads(
    client_factory, auth_headers, db_session, storage, worker_sessionmaker, cleanup
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id, script_id = await _render_now(
            client, headers, db_session, storage, worker_sessionmaker, cleanup
        )
        url = _render_url(project_id, script_id) + "/video?orientation=horizontal&quality=low"

        full = await client.get(url, headers=headers)
        assert full.status_code == 200 and full.headers["content-type"] == "video/mp4"
        assert full.headers["accept-ranges"] == "bytes"
        total = len(full.content)
        assert int(full.headers["content-length"]) == total
        assert full.content[4:8] == b"ftyp"  # a real MP4

        part = await client.get(url, headers={**headers, "Range": "bytes=0-1023"})
        assert part.status_code == 206
        assert part.headers["content-range"] == f"bytes 0-1023/{total}"
        assert part.content == full.content[:1024]

        middle = await client.get(url, headers={**headers, "Range": "bytes=2000-2999"})
        assert middle.status_code == 206 and middle.content == full.content[2000:3000]

        tail = await client.get(url, headers={**headers, "Range": "bytes=-500"})
        assert tail.status_code == 206 and tail.content == full.content[-500:]

        beyond = await client.get(url, headers={**headers, "Range": f"bytes={total + 10}-"})
        assert beyond.status_code == 416
        assert beyond.headers["content-range"] == f"bytes */{total}"

        download = await client.get(url + "&download=true", headers=headers)
        assert download.status_code == 200
        assert "attachment" in download.headers["content-disposition"]
        assert ".mp4" in download.headers["content-disposition"]

        other = await client.get(
            _render_url(project_id, script_id) + "/video?orientation=vertical&quality=low",
            headers=headers,
        )
        assert other.status_code == 404  # never rendered in that shape


async def test_no_video_before_a_render_is_a_404(
    client_factory, auth_headers, db_session, storage, cleanup
):
    headers = auth_headers()
    async with client_factory() as client:
        built, project_id, script_id = await _project_with_storyboard(
            client, headers, db_session, storage
        )
        cleanup.append(built)
        response = await client.get(
            _render_url(project_id, script_id) + "/video?orientation=horizontal&quality=low",
            headers=headers,
        )
    assert response.status_code == 404


async def test_the_status_of_a_finished_render_is_complete_and_never_falsely_so(
    client_factory, auth_headers, db_session, storage, worker_sessionmaker, cleanup
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id, script_id = await _render_now(
            client, headers, db_session, storage, worker_sessionmaker, cleanup
        )
        url = _render_url(project_id, script_id) + "?orientation=horizontal&quality=low"
        done = (await client.get(url, headers=headers)).json()
        assert done["is_complete"] is True and done["output"]["is_current"] is True
        assert done["ready_count"] == 6

        # A different configuration was never rendered: not complete, whatever else exists.
        other = (
            await client.get(
                _render_url(project_id, script_id) + "?orientation=vertical&quality=low",
                headers=headers,
            )
        ).json()
        assert other["is_complete"] is False and other["output"] is None

        progress = (
            await client.get(f"/v1/projects/{project_id}/render/progress", headers=headers)
        ).json()
        assert progress["job"]["status"] == "succeeded"
        assert progress["progress"]["percent"] == 100
        assert progress["progress"]["chunks_total"] == 6


async def test_retry_with_nothing_failed_is_a_409(
    client_factory, auth_headers, db_session, storage, cleanup
):
    headers = auth_headers()
    async with client_factory() as client:
        built, project_id, _ = await _project_with_storyboard(client, headers, db_session, storage)
        cleanup.append(built)
        response = await client.post(
            f"/v1/projects/{project_id}/render/retry", headers=_headers(headers)
        )
    assert response.status_code == 409
    assert "no failed render" in response.json()["error"]["message"]
