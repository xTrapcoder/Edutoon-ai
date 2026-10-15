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
from edutoon.providers.storage import get_storage_client
from edutoon.repositories import users as users_repo
from tests.idempotency_helpers import INVALID_KEYS, idempotency_status

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
    # ASGITransport never runs the app's lifespan, so app.state.redis and app.state.storage
    # - which the idempotency dependency and the delete route (it removes the project's
    # uploaded files) read - are set here exactly as lifespan() would have set them.
    settings = get_settings()
    app.state.redis = get_redis_client(settings.REDIS_URL)
    app.state.storage = get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )

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
        assert created["language"] == "en-IN"
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
        response = await client.post("/v1/projects", json=_create_payload(), headers=auth_headers())

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


async def test_duplicate_post_with_same_key_creates_only_one_project(client_factory, auth_headers):
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


# --- the Idempotency-Key header, and durability before the response --------------------

# One request per mutating route, built from an existing project's id.
_MUTATIONS: dict[str, Callable[[str], tuple[str, str, dict[str, object] | None]]] = {
    "create": lambda project_id: ("POST", "/v1/projects", _create_payload(title="A second one")),
    "update": lambda project_id: ("PATCH", f"/v1/projects/{project_id}", {"title": "Renamed"}),
    "delete": lambda project_id: ("DELETE", f"/v1/projects/{project_id}", None),
}


async def _a_project(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    response = await client.post(
        "/v1/projects", json=_create_payload(), headers={**headers, "Idempotency-Key": str(uuid4())}
    )
    assert response.status_code == 201
    return response.json()["id"]  # type: ignore[no-any-return]


@pytest.mark.parametrize("route", _MUTATIONS)
async def test_a_mutation_is_committed_before_its_result_is_cached_or_returned(
    client_factory, auth_headers, db_session, redis_client, monkeypatch, route
):
    """The request-scoped session only commits once FastAPI has sent the response, so
    the router commits for itself: at the moment it does, the dedupe record must still
    say "in_progress" - a cached "completed" would promise work a failed commit could
    still lose.
    """
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _a_project(client, headers)
        method, path, body = _MUTATIONS[route](project_id)
        key = str(uuid4())
        seen_at_commit: list[str | None] = []
        real_commit = db_session.commit

        async def _spying_commit() -> None:
            seen_at_commit.append(await idempotency_status(redis_client, path, key))
            await real_commit()

        monkeypatch.setattr(db_session, "commit", _spying_commit)
        response = await client.request(
            method, path, json=body, headers={**headers, "Idempotency-Key": key}
        )

    assert response.status_code in (200, 201, 204), response.text
    assert seen_at_commit == ["in_progress"]
    assert await idempotency_status(redis_client, path, key) == "completed"


@pytest.mark.parametrize("route", _MUTATIONS)
async def test_a_commit_that_fails_leaves_no_cached_success_behind(
    client_factory, auth_headers, db_session, redis_client, monkeypatch, route
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _a_project(client, headers)
        method, path, body = _MUTATIONS[route](project_id)
        key = str(uuid4())

        async def _failing_commit() -> None:
            raise RuntimeError("the database went away")

        monkeypatch.setattr(db_session, "commit", _failing_commit)
        with pytest.raises(RuntimeError, match="went away"):
            await client.request(
                method, path, json=body, headers={**headers, "Idempotency-Key": key}
            )

    # Nothing says the work was done, and the key is free for the client's retry.
    assert await idempotency_status(redis_client, path, key) is None


@pytest.mark.parametrize("route", _MUTATIONS)
@pytest.mark.parametrize("key", INVALID_KEYS)
async def test_an_empty_or_oversized_idempotency_key_is_rejected_and_nothing_is_written(
    client_factory, auth_headers, route, key
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _a_project(client, headers)
        before = (await client.get("/v1/projects", headers=headers)).json()
        method, path, body = _MUTATIONS[route](project_id)

        response = await client.request(
            method, path, json=body, headers={**headers, "Idempotency-Key": key}
        )

        assert response.status_code == 422
        [error] = response.json()["error"]["details"]["field_errors"]
        assert error["path"] == "header.idempotency-key"
        assert (await client.get("/v1/projects", headers=headers)).json() == before


async def test_an_idempotency_key_of_the_maximum_length_is_accepted(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        response = await client.post(
            "/v1/projects",
            json=_create_payload(),
            headers={**headers, "Idempotency-Key": "k" * 255},
        )

    assert response.status_code == 201, response.text


# --- generation settings (orientation / target_duration_ms / language) -----------------

_MIN_MS = 60_000
_MAX_MS = 3_600_000

_INVALID_DURATIONS = [
    pytest.param(0, id="zero"),
    pytest.param(-1, id="negative-one"),
    pytest.param(-_MIN_MS, id="negative-minimum"),
    pytest.param(_MIN_MS - 1, id="one-millisecond-under-the-minimum"),
    pytest.param(_MAX_MS + 1, id="one-millisecond-over-the-maximum"),
    pytest.param(61 * _MIN_MS, id="sixty-one-minutes"),
    pytest.param(10**15, id="absurdly-large"),
    pytest.param("180000", id="numeric-string"),
    pytest.param("three minutes", id="text"),
    pytest.param(180_000.5, id="fractional"),
    pytest.param(180_000.0, id="integral-float"),
    pytest.param(True, id="boolean-true"),
    pytest.param(False, id="boolean-false"),
    pytest.param(None, id="null"),
    pytest.param([180_000], id="list"),
    pytest.param({"minutes": 3}, id="object"),
]
_INVALID_ORIENTATIONS = [
    pytest.param("square", id="unsupported"),
    pytest.param("landscape", id="synonym"),
    pytest.param("Horizontal", id="wrong-case"),
    pytest.param("", id="empty"),
    pytest.param(None, id="null"),
    pytest.param(1, id="number"),
    pytest.param(["horizontal"], id="list"),
]
_INVALID_LANGUAGES = [
    pytest.param("fr-FR", id="unsupported"),
    pytest.param("en", id="bare-language"),
    pytest.param("en-US", id="unsupported-english"),
    pytest.param("en_IN", id="underscore"),
    pytest.param("en-in", id="lowercase-region"),
    pytest.param("EN-IN", id="uppercase"),
    pytest.param("", id="empty"),
    pytest.param(None, id="null"),
    pytest.param(1, id="number"),
]


def _without_null(params):
    """PATCH refuses an explicit ``null`` in the model-level validator (error path
    ``body``, not ``body.<field>``), so it is tested on its own below.
    """
    return [param for param in params if param.values[0] is not None]


def _field_error_paths(response: httpx.Response) -> list[str]:
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    return [entry["path"] for entry in error["details"]["field_errors"]]


async def _create(client, headers, **overrides):
    return await client.post(
        "/v1/projects",
        json=_create_payload(**overrides),
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )


async def _patch(client, headers, project_id, body):
    return await client.patch(
        f"/v1/projects/{project_id}",
        json=body,
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )


async def test_a_project_created_without_settings_gets_the_defaults(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        created = (await _create(client, headers)).json()
        fetched = (await client.get(f"/v1/projects/{created['id']}", headers=headers)).json()

    for project in (created, fetched):
        assert project["orientation"] == "horizontal"
        assert project["target_duration_ms"] == 300_000
        assert project["language"] == "en-IN"


async def test_the_exact_payload_existing_callers_send_still_works(client_factory, auth_headers):
    """Backwards compatibility: the request body from before generation settings
    existed is still a valid request, and everything it used to return is still there.
    """
    headers = auth_headers()

    async with client_factory() as client:
        response = await client.post(
            "/v1/projects",
            json={"title": "My video", "source_type": "pdf", "description": "As before"},
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "My video"
    assert body["source_type"] == "pdf"
    assert body["description"] == "As before"
    assert body["status"] == "draft"
    assert body["language"] == "en-IN"
    assert {"id", "owner_id", "created_at", "updated_at"} <= body.keys()


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
async def test_each_orientation_is_accepted_and_persisted(
    client_factory, auth_headers, orientation
):
    headers = auth_headers()

    async with client_factory() as client:
        response = await _create(client, headers, orientation=orientation)
        fetched = await client.get(f"/v1/projects/{response.json()['id']}", headers=headers)

    assert response.status_code == 201
    assert response.json()["orientation"] == orientation
    assert fetched.json()["orientation"] == orientation


@pytest.mark.parametrize("language", ["en-IN", "en-GB"])
async def test_each_supported_language_is_accepted_and_persisted(
    client_factory, auth_headers, language
):
    headers = auth_headers()

    async with client_factory() as client:
        response = await _create(client, headers, language=language)
        fetched = await client.get(f"/v1/projects/{response.json()['id']}", headers=headers)

    assert response.status_code == 201
    assert response.json()["language"] == language
    assert fetched.json()["language"] == language


@pytest.mark.parametrize(
    "duration",
    [
        pytest.param(_MIN_MS, id="minimum-one-minute"),
        pytest.param(_MIN_MS + 1, id="just-over-the-minimum"),
        pytest.param(90_000, id="ninety-seconds"),
        pytest.param(300_000, id="five-minutes"),
        pytest.param(1_800_000, id="thirty-minutes"),
        pytest.param(_MAX_MS - 1, id="just-under-the-maximum"),
        pytest.param(_MAX_MS, id="maximum-sixty-minutes"),
    ],
)
async def test_a_target_duration_within_one_to_sixty_minutes_is_accepted_and_persisted(
    client_factory, auth_headers, duration
):
    headers = auth_headers()

    async with client_factory() as client:
        response = await _create(client, headers, target_duration_ms=duration)
        fetched = await client.get(f"/v1/projects/{response.json()['id']}", headers=headers)

    assert response.status_code == 201
    assert response.json()["target_duration_ms"] == duration
    assert fetched.json()["target_duration_ms"] == duration


async def test_all_three_settings_can_be_given_together(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        response = await _create(
            client, headers, orientation="vertical", language="en-GB", target_duration_ms=900_000
        )

    assert response.status_code == 201
    body = response.json()
    assert (body["orientation"], body["language"], body["target_duration_ms"]) == (
        "vertical",
        "en-GB",
        900_000,
    )


@pytest.mark.parametrize("duration", _INVALID_DURATIONS)
async def test_create_rejects_an_invalid_target_duration(client_factory, auth_headers, duration):
    headers = auth_headers()

    async with client_factory() as client:
        response = await _create(client, headers, target_duration_ms=duration)
        listed = await client.get("/v1/projects", headers=headers)

    assert response.status_code == 422
    assert _field_error_paths(response) == ["body.target_duration_ms"]
    assert listed.json()["items"] == []  # nothing was written


@pytest.mark.parametrize("orientation", _INVALID_ORIENTATIONS)
async def test_create_rejects_an_unsupported_orientation(client_factory, auth_headers, orientation):
    headers = auth_headers()

    async with client_factory() as client:
        response = await _create(client, headers, orientation=orientation)
        listed = await client.get("/v1/projects", headers=headers)

    assert response.status_code == 422
    assert _field_error_paths(response) == ["body.orientation"]
    assert listed.json()["items"] == []


@pytest.mark.parametrize("language", _INVALID_LANGUAGES)
async def test_create_rejects_an_unsupported_language(client_factory, auth_headers, language):
    headers = auth_headers()

    async with client_factory() as client:
        response = await _create(client, headers, language=language)
        listed = await client.get("/v1/projects", headers=headers)

    assert response.status_code == 422
    assert _field_error_paths(response) == ["body.language"]
    assert listed.json()["items"] == []


async def test_a_replayed_create_returns_the_same_settings(client_factory, auth_headers):
    headers = {**auth_headers(), "Idempotency-Key": str(uuid4())}
    payload = _create_payload(orientation="vertical", language="en-GB", target_duration_ms=120_000)

    async with client_factory() as client:
        first = await client.post("/v1/projects", json=payload, headers=headers)
        second = await client.post("/v1/projects", json=payload, headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert second.json()["orientation"] == "vertical"
    assert second.json()["target_duration_ms"] == 120_000


async def test_list_returns_each_projects_settings(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        await _create(client, headers, orientation="vertical", target_duration_ms=_MIN_MS)
        listed = await client.get("/v1/projects", headers=headers)

    [item] = listed.json()["items"]
    assert (item["orientation"], item["target_duration_ms"], item["language"]) == (
        "vertical",
        _MIN_MS,
        "en-IN",
    )


# --- updating the settings ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("orientation", "vertical"),
        ("language", "en-GB"),
        ("target_duration_ms", 1_200_000),
        ("target_duration_ms", _MIN_MS),
        ("target_duration_ms", _MAX_MS),
    ],
)
async def test_update_changes_one_setting_and_leaves_the_others(
    client_factory, auth_headers, field, value
):
    headers = auth_headers()

    async with client_factory() as client:
        created = (await _create(client, headers)).json()

        response = await _patch(client, headers, created["id"], {field: value})
        fetched = await client.get(f"/v1/projects/{created['id']}", headers=headers)

    assert response.status_code == 200
    expected = {key: value_ for key, value_ in created.items() if key != "updated_at"}
    expected[field] = value
    for project in (response.json(), fetched.json()):
        assert {key: v for key, v in project.items() if key != "updated_at"} == expected


async def test_update_changes_all_three_settings_at_once(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        created = (await _create(client, headers)).json()

        response = await _patch(
            client,
            headers,
            created["id"],
            {"orientation": "vertical", "language": "en-GB", "target_duration_ms": 2_700_000},
        )
        fetched = await client.get(f"/v1/projects/{created['id']}", headers=headers)

    assert response.status_code == 200
    for project in (response.json(), fetched.json()):
        assert project["orientation"] == "vertical"
        assert project["language"] == "en-GB"
        assert project["target_duration_ms"] == 2_700_000
        assert project["title"] == created["title"]


async def test_a_title_only_update_leaves_the_settings_alone(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        created = (
            await _create(
                client,
                headers,
                orientation="vertical",
                language="en-GB",
                target_duration_ms=600_000,
            )
        ).json()

        response = await _patch(client, headers, created["id"], {"title": "Renamed"})

    body = response.json()
    assert body["title"] == "Renamed"
    assert (body["orientation"], body["language"], body["target_duration_ms"]) == (
        "vertical",
        "en-GB",
        600_000,
    )


@pytest.mark.parametrize("duration", _without_null(_INVALID_DURATIONS))
async def test_update_rejects_an_invalid_target_duration_and_changes_nothing(
    client_factory, auth_headers, duration
):
    headers = auth_headers()

    async with client_factory() as client:
        created = (await _create(client, headers, target_duration_ms=600_000)).json()

        response = await _patch(client, headers, created["id"], {"target_duration_ms": duration})
        fetched = await client.get(f"/v1/projects/{created['id']}", headers=headers)

    assert response.status_code == 422
    assert _field_error_paths(response) == ["body.target_duration_ms"]
    assert fetched.json() == created


@pytest.mark.parametrize("orientation", _without_null(_INVALID_ORIENTATIONS))
async def test_update_rejects_an_unsupported_orientation_and_changes_nothing(
    client_factory, auth_headers, orientation
):
    headers = auth_headers()

    async with client_factory() as client:
        created = (await _create(client, headers)).json()

        response = await _patch(client, headers, created["id"], {"orientation": orientation})
        fetched = await client.get(f"/v1/projects/{created['id']}", headers=headers)

    assert response.status_code == 422
    assert _field_error_paths(response) == ["body.orientation"]
    assert fetched.json() == created


@pytest.mark.parametrize("language", _without_null(_INVALID_LANGUAGES))
async def test_update_rejects_an_unsupported_language_and_changes_nothing(
    client_factory, auth_headers, language
):
    headers = auth_headers()

    async with client_factory() as client:
        created = (await _create(client, headers)).json()

        response = await _patch(client, headers, created["id"], {"language": language})
        fetched = await client.get(f"/v1/projects/{created['id']}", headers=headers)

    assert response.status_code == 422
    assert _field_error_paths(response) == ["body.language"]
    assert fetched.json() == created


@pytest.mark.parametrize("field", ["orientation", "language", "target_duration_ms"])
async def test_update_rejects_an_explicit_null_for_a_setting_and_changes_nothing(
    client_factory, auth_headers, field
):
    """The settings are ``NOT NULL``: omit one to leave it unchanged, ``null`` is refused."""
    headers = auth_headers()

    async with client_factory() as client:
        created = (await _create(client, headers)).json()

        response = await _patch(client, headers, created["id"], {field: None})
        fetched = await client.get(f"/v1/projects/{created['id']}", headers=headers)

    assert response.status_code == 422
    assert f"{field} cannot be null" in response.text
    assert fetched.json() == created


async def test_one_invalid_setting_stops_a_valid_one_in_the_same_update(
    client_factory, auth_headers
):
    headers = auth_headers()

    async with client_factory() as client:
        created = (await _create(client, headers)).json()

        response = await _patch(
            client,
            headers,
            created["id"],
            {"orientation": "vertical", "target_duration_ms": _MAX_MS + 1},
        )
        fetched = await client.get(f"/v1/projects/{created['id']}", headers=headers)

    assert response.status_code == 422
    assert fetched.json()["orientation"] == "horizontal"  # the valid half was not applied


async def test_a_project_owned_by_someone_else_cannot_have_its_settings_changed(
    client_factory, auth_headers
):
    owner_headers = auth_headers()

    async with client_factory() as client:
        created = (await _create(client, owner_headers)).json()

        response = await _patch(client, auth_headers(), created["id"], {"orientation": "vertical"})
        fetched = await client.get(f"/v1/projects/{created['id']}", headers=owner_headers)

    assert response.status_code == 404
    assert fetched.json()["orientation"] == "horizontal"
