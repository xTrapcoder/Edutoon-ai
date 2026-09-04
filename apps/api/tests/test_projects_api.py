"""``/v1/projects`` - authenticated, ownership-scoped CRUD.

Same no-real-Clerk pattern as ``test_auth.py``: tokens are signed locally
and ``providers.clerk.get_signing_key`` is monkeypatched. ``get_session``
is overridden to the transactional ``db_session`` fixture so nothing here
touches the real database outside its own rolled-back transaction.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

from edutoon.api.dependencies import get_current_user
from edutoon.core.config import get_settings
from edutoon.db.session import get_session
from edutoon.main import create_app
from edutoon.providers import clerk
from edutoon.providers.cache import get_redis_client
from edutoon.repositories import users as users_repo

TEST_ISSUER = "https://test.clerk.accounts.dev"
IDEMPOTENCY_HEADERS = {"Idempotency-Key": "test-key-1"}


def _generate_rsa_keypair() -> tuple[bytes, RSAPublicKey]:
    private_key: RSAPrivateKey = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return private_pem, private_key.public_key()


def _make_token(private_pem: bytes, *, sub: str, email: str) -> str:
    now = int(time.time())
    payload = {"sub": sub, "email": email, "iat": now, "exp": now + 3600, "iss": TEST_ISSUER}
    return jwt.encode(payload, private_pem, algorithm="RS256")


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[bytes, RSAPublicKey]:
    return _generate_rsa_keypair()


@pytest.fixture
def auth_headers(rsa_keypair, monkeypatch) -> Callable[[], dict[str, str]]:
    """Each call mints a fresh, distinct authenticated user's headers."""
    private_pem, public_key = rsa_keypair
    monkeypatch.setattr(clerk, "get_signing_key", lambda token: public_key)

    def _make() -> dict[str, str]:
        token = _make_token(private_pem, sub=f"clerk_{uuid4()}", email=f"{uuid4()}@example.com")
        return {"Authorization": f"Bearer {token}"}

    return _make


@pytest.fixture
async def client_factory(db_session) -> AsyncIterator[Callable[[], httpx.AsyncClient]]:
    app = create_app()

    async def _session_override():
        yield db_session

    app.dependency_overrides[get_session] = _session_override
    # ASGITransport never runs the app's lifespan, so app.state.redis - which
    # the idempotency dependency reads - is set here exactly as lifespan()
    # would have set it.
    app.state.redis = get_redis_client(get_settings().REDIS_URL)

    def _make() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    yield _make

    await app.state.redis.aclose()


def _create_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {"title": "My video", "source_type": "topic"}
    payload.update(overrides)
    return payload


# --- authenticated CRUD ----------------------------------------------------------


async def test_create_get_list_update_delete_round_trip(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        create_resp = await client.post(
            "/v1/projects",
            json=_create_payload(description="A test project"),
            headers={**headers, **IDEMPOTENCY_HEADERS},
        )
        assert create_resp.status_code == 201
        created = create_resp.json()
        assert created["title"] == "My video"
        assert created["source_type"] == "topic"
        assert created["status"] == "draft"
        assert created["language"] == "en-GB"
        project_id = created["id"]

        get_resp = await client.get(f"/v1/projects/{project_id}", headers=headers)
        assert get_resp.status_code == 200
        assert get_resp.json() == created

        list_resp = await client.get("/v1/projects", headers=headers)
        assert list_resp.status_code == 200
        list_body = list_resp.json()
        assert [item["id"] for item in list_body["items"]] == [project_id]
        assert list_body["next_cursor"] is None

        update_resp = await client.patch(
            f"/v1/projects/{project_id}",
            json={"title": "Renamed"},
            headers={**headers, **IDEMPOTENCY_HEADERS},
        )
        assert update_resp.status_code == 200
        updated = update_resp.json()
        assert updated["title"] == "Renamed"
        assert updated["description"] == "A test project"  # untouched by the partial update

        delete_resp = await client.delete(
            f"/v1/projects/{project_id}", headers={**headers, **IDEMPOTENCY_HEADERS}
        )
        assert delete_resp.status_code == 204

        after_delete = await client.get(f"/v1/projects/{project_id}", headers=headers)
        assert after_delete.status_code == 404


async def test_update_with_empty_body_is_a_no_op(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        create_resp = await client.post(
            "/v1/projects", json=_create_payload(), headers={**headers, **IDEMPOTENCY_HEADERS}
        )
        project_id = create_resp.json()["id"]

        update_resp = await client.patch(
            f"/v1/projects/{project_id}", json={}, headers={**headers, **IDEMPOTENCY_HEADERS}
        )

    assert update_resp.status_code == 200
    assert update_resp.json() == create_resp.json()


async def test_clearing_nullable_description_with_explicit_null(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        create_resp = await client.post(
            "/v1/projects",
            json=_create_payload(description="will be cleared"),
            headers={**headers, **IDEMPOTENCY_HEADERS},
        )
        project_id = create_resp.json()["id"]

        update_resp = await client.patch(
            f"/v1/projects/{project_id}",
            json={"description": None},
            headers={**headers, **IDEMPOTENCY_HEADERS},
        )

    assert update_resp.status_code == 200
    assert update_resp.json()["description"] is None


# --- ownership isolation (rule 9: not-owned -> 404, never 403) -------------------


async def test_other_users_project_returns_404_not_403(client_factory, auth_headers):
    owner_headers = auth_headers()
    other_headers = auth_headers()

    async with client_factory() as client:
        create_resp = await client.post(
            "/v1/projects", json=_create_payload(), headers={**owner_headers, **IDEMPOTENCY_HEADERS}
        )
        project_id = create_resp.json()["id"]

        get_resp = await client.get(f"/v1/projects/{project_id}", headers=other_headers)
        update_resp = await client.patch(
            f"/v1/projects/{project_id}",
            json={"title": "Hijacked"},
            headers={**other_headers, **IDEMPOTENCY_HEADERS},
        )
        delete_resp = await client.delete(
            f"/v1/projects/{project_id}", headers={**other_headers, **IDEMPOTENCY_HEADERS}
        )

    assert get_resp.status_code == 404
    assert update_resp.status_code == 404
    assert delete_resp.status_code == 404
    for response in (get_resp, update_resp, delete_resp):
        assert response.json()["error"]["code"] == "not_found"


async def test_list_projects_is_scoped_to_the_caller(client_factory, auth_headers):
    owner_headers = auth_headers()
    other_headers = auth_headers()

    async with client_factory() as client:
        await client.post(
            "/v1/projects", json=_create_payload(), headers={**owner_headers, **IDEMPOTENCY_HEADERS}
        )
        list_resp = await client.get("/v1/projects", headers=other_headers)

    assert list_resp.status_code == 200
    assert list_resp.json()["items"] == []


# --- not-found cases ---------------------------------------------------------------


async def test_get_nonexistent_project_returns_404(client_factory, auth_headers):
    async with client_factory() as client:
        response = await client.get(f"/v1/projects/{uuid4()}", headers=auth_headers())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_delete_nonexistent_project_returns_404(client_factory, auth_headers):
    async with client_factory() as client:
        response = await client.delete(
            f"/v1/projects/{uuid4()}", headers={**auth_headers(), **IDEMPOTENCY_HEADERS}
        )

    assert response.status_code == 404


# --- validation failures -----------------------------------------------------------


async def test_create_rejects_unknown_fields(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        response = await client.post(
            "/v1/projects",
            json=_create_payload(sneaky="field"),
            headers={**headers, **IDEMPOTENCY_HEADERS},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_create_rejects_invalid_source_type(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        response = await client.post(
            "/v1/projects",
            json=_create_payload(source_type="video"),
            headers={**headers, **IDEMPOTENCY_HEADERS},
        )

    assert response.status_code == 422


async def test_create_rejects_blank_title(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        response = await client.post(
            "/v1/projects",
            json=_create_payload(title="   "),
            headers={**headers, **IDEMPOTENCY_HEADERS},
        )

    assert response.status_code == 422


async def test_update_rejects_explicit_null_title(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        create_resp = await client.post(
            "/v1/projects", json=_create_payload(), headers={**headers, **IDEMPOTENCY_HEADERS}
        )
        project_id = create_resp.json()["id"]

        response = await client.patch(
            f"/v1/projects/{project_id}",
            json={"title": None},
            headers={**headers, **IDEMPOTENCY_HEADERS},
        )

    assert response.status_code == 422


async def test_update_rejects_unknown_fields(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        create_resp = await client.post(
            "/v1/projects", json=_create_payload(), headers={**headers, **IDEMPOTENCY_HEADERS}
        )
        project_id = create_resp.json()["id"]

        response = await client.patch(
            f"/v1/projects/{project_id}",
            json={"status": "ready"},
            headers={**headers, **IDEMPOTENCY_HEADERS},
        )

    assert response.status_code == 422


# --- idempotency-key header enforcement (rule 8) ------------------------------------


async def test_create_without_idempotency_key_is_rejected(client_factory, auth_headers):
    async with client_factory() as client:
        response = await client.post(
            "/v1/projects", json=_create_payload(), headers=auth_headers()
        )

    assert response.status_code == 422


async def test_update_without_idempotency_key_is_rejected(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        create_resp = await client.post(
            "/v1/projects", json=_create_payload(), headers={**headers, **IDEMPOTENCY_HEADERS}
        )
        project_id = create_resp.json()["id"]

        response = await client.patch(
            f"/v1/projects/{project_id}", json={"title": "x"}, headers=headers
        )

    assert response.status_code == 422


async def test_delete_without_idempotency_key_is_rejected(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        create_resp = await client.post(
            "/v1/projects", json=_create_payload(), headers={**headers, **IDEMPOTENCY_HEADERS}
        )
        project_id = create_resp.json()["id"]

        response = await client.delete(f"/v1/projects/{project_id}", headers=headers)

    assert response.status_code == 422


# --- idempotency-key deduplication (rule 8, real store) -----------------------------


async def test_duplicate_post_with_same_key_creates_only_one_project(
    client_factory, auth_headers
):
    headers = {**auth_headers(), "Idempotency-Key": str(uuid4())}

    async with client_factory() as client:
        first = await client.post("/v1/projects", json=_create_payload(), headers=headers)
        second = await client.post("/v1/projects", json=_create_payload(), headers=headers)

        list_resp = await client.get("/v1/projects", headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()  # replayed, not a fresh row
    assert len(list_resp.json()["items"]) == 1


async def test_replay_returns_identical_response_for_update_and_delete(
    client_factory, auth_headers
):
    headers = auth_headers()

    async with client_factory() as client:
        create_resp = await client.post(
            "/v1/projects", json=_create_payload(), headers={**headers, **IDEMPOTENCY_HEADERS}
        )
        project_id = create_resp.json()["id"]

        update_key = {**headers, "Idempotency-Key": str(uuid4())}
        update_first = await client.patch(
            f"/v1/projects/{project_id}", json={"title": "Renamed"}, headers=update_key
        )
        update_second = await client.patch(
            f"/v1/projects/{project_id}", json={"title": "Renamed"}, headers=update_key
        )

        delete_key = {**headers, "Idempotency-Key": str(uuid4())}
        delete_first = await client.delete(f"/v1/projects/{project_id}", headers=delete_key)
        # Replays the cached 204 without re-running delete_project (which
        # would otherwise 404 on an already-deleted row).
        delete_second = await client.delete(f"/v1/projects/{project_id}", headers=delete_key)

    assert update_first.json() == update_second.json()
    assert delete_first.status_code == delete_second.status_code == 204


async def test_different_users_can_reuse_the_same_idempotency_key(client_factory, auth_headers):
    shared_key = str(uuid4())
    headers_a = {**auth_headers(), "Idempotency-Key": shared_key}
    headers_b = {**auth_headers(), "Idempotency-Key": shared_key}

    async with client_factory() as client:
        response_a = await client.post(
            "/v1/projects", json=_create_payload(title="A"), headers=headers_a
        )
        response_b = await client.post(
            "/v1/projects", json=_create_payload(title="B"), headers=headers_b
        )

    assert response_a.status_code == response_b.status_code == 201
    assert response_a.json()["id"] != response_b.json()["id"]
    assert response_a.json()["title"] == "A"
    assert response_b.json()["title"] == "B"


async def test_duplicate_post_with_same_key_but_different_payload_replays_first_response(
    client_factory, auth_headers
):
    """The idempotency key is not payload-aware (rule 8 scopes it to owner +
    route + key only) - reusing it with a different body still replays the
    first response rather than creating a second project or erroring.
    """
    headers = {**auth_headers(), "Idempotency-Key": str(uuid4())}

    async with client_factory() as client:
        first = await client.post(
            "/v1/projects", json=_create_payload(title="First"), headers=headers
        )
        second = await client.post(
            "/v1/projects", json=_create_payload(title="Second"), headers=headers
        )

        list_resp = await client.get("/v1/projects", headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert second.json()["title"] == "First"  # the "Second" body never took effect
    assert len(list_resp.json()["items"]) == 1


async def test_concurrent_duplicate_post_returns_409(db_session):
    """A genuine concurrent-duplicate test over the real HTTP route.

    Unlike the other tests here, this does not use `client_factory`/
    `auth_headers`: those route auth through the real Clerk-verification
    dependency, which queries `db_session` - and a single `AsyncSession`
    is not safe for two coroutines to use at once, so two truly concurrent
    requests would collide on the ORM itself before ever reaching the
    idempotency check (a test-fixture limitation, not an idempotency bug).

    Sidestepping that: `get_current_user` is overridden to hand back an
    already-persisted user directly (one sequential DB write, before the
    concurrency starts), so neither concurrent request touches the session
    during auth. Only the request that wins the Redis lock ever touches
    `db_session` afterwards - the loser's whole path (a failed `SET NX`,
    a `GET`, then raising) is Redis-only - so there is still exactly one
    session user at a time, and the real route/service/repository stack
    runs unmodified.
    """
    fixed_user = await users_repo.create(db_session, email=f"{uuid4()}@example.com")

    app = create_app()

    async def _session_override():
        yield db_session

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_current_user] = lambda: fixed_user
    app.state.redis = get_redis_client(get_settings().REDIS_URL)

    idempotency_key = str(uuid4())
    headers = {"Idempotency-Key": idempotency_key}

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response_a, response_b = await asyncio.gather(
                client.post("/v1/projects", json=_create_payload(), headers=headers),
                client.post("/v1/projects", json=_create_payload(), headers=headers),
            )

            list_resp = await client.get("/v1/projects", headers=headers)
    finally:
        await app.state.redis.aclose()

    statuses = sorted([response_a.status_code, response_b.status_code])
    assert statuses == [201, 409]
    conflict = response_a if response_a.status_code == 409 else response_b
    assert conflict.json()["error"]["code"] == "idempotency_in_progress"
    assert len(list_resp.json()["items"]) == 1
