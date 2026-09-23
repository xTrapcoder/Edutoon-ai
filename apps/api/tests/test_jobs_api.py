"""``/v1/projects/{project_id}/jobs`` - read-only job progress (Phase G).

Same no-real-Clerk pattern as ``test_projects_api.py`` and
``test_scripts_api.py``: tokens are signed locally and
``providers.clerk.get_signing_key`` is monkeypatched. ``get_session`` is
overridden to the transactional ``db_session`` fixture, so nothing here
persists past its own rolled-back transaction. The router itself is
read-only and has no Redis dependency, but ``client_factory`` still wires
up ``app.state.redis`` because the shared ``_create_project`` helper below
calls the Redis-backed idempotency path on ``POST /v1/projects``.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

from edutoon.core.config import get_settings
from edutoon.db.session import get_session
from edutoon.main import create_app
from edutoon.providers import clerk
from edutoon.providers.cache import get_redis_client
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import projects as projects_repo

TEST_ISSUER = "https://test.clerk.accounts.dev"


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


async def _create_project(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    response = await client.post(
        "/v1/projects",
        json={"title": "P", "source_type": "pdf"},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 201
    return response.json()["id"]  # type: ignore[no-any-return]


async def test_list_jobs_returns_a_projects_jobs_newest_first(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        project = await projects_repo.get_by_id(db_session, UUID(project_id))
        assert project is not None
        first = await jobs_repo.create(
            db_session, project_id=project.id, kind="build_evidence"
        )
        second = await jobs_repo.create(
            db_session, project_id=project.id, kind="generate_script"
        )

        response = await client.get(f"/v1/projects/{project_id}/jobs", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == [str(second.id), str(first.id)]
    assert body["items"][0]["kind"] == "generate_script"
    assert body["items"][0]["status"] == "queued"
    assert body["next_cursor"] is None


async def test_list_jobs_for_another_users_project_returns_404_not_403(
    client_factory, auth_headers, db_session
):
    owner_headers = auth_headers()
    other_headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, owner_headers)
        project = await projects_repo.get_by_id(db_session, UUID(project_id))
        assert project is not None
        await jobs_repo.create(db_session, project_id=project.id, kind="build_evidence")

        response = await client.get(f"/v1/projects/{project_id}/jobs", headers=other_headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_list_jobs_for_nonexistent_project_returns_404(client_factory, auth_headers):
    async with client_factory() as client:
        response = await client.get(
            f"/v1/projects/{uuid4()}/jobs", headers=auth_headers()
        )

    assert response.status_code == 404


async def test_list_jobs_without_authorization_is_unauthorized(client_factory):
    async with client_factory() as client:
        response = await client.get(f"/v1/projects/{uuid4()}/jobs")

    assert response.status_code == 401
