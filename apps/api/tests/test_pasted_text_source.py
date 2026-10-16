"""Pasted text as a project's source, through the real API (``POST .../sources/text``).

Runs on the scratch database (``committed_database_url``, migrated to head by Alembic) rather than
the shared development database, because it needs migration 0023's ``text`` vocabulary; each test
rolls its work back. Storage is the real local MinIO on scratch buckets, and every object a test
writes is deleted again.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import AsyncIterator, Callable
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from edutoon.core.config import get_settings
from edutoon.db.session import get_session
from edutoon.domain import pasted_text
from edutoon.domain.source_storage import source_storage_key
from edutoon.main import create_app
from edutoon.providers import clerk
from edutoon.providers.cache import get_redis_client
from edutoon.providers.storage import get_storage_client
from edutoon.repositories import jobs as jobs_repo

TEST_ISSUER = "https://test.clerk.accounts.dev"

TEXT = (
    "Photosynthesis is the process by which green plants make food. "
    "It takes place in the chloroplasts, which contain chlorophyll. "
    "Light energy is absorbed and used to turn water and carbon dioxide into glucose and "
    "oxygen.\n\n"
    "The oxygen is released through tiny pores in the leaf called stomata. "
    "The glucose is used for energy or stored as starch for later use.\n\n"
) * 3


@pytest.fixture(scope="module")
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem, private_key.public_key()


@pytest.fixture
def auth_headers(rsa_keypair, monkeypatch) -> Callable[[], dict[str, str]]:
    private_pem, public_key = rsa_keypair
    monkeypatch.setattr(clerk, "get_signing_key", lambda token: public_key)

    def _make() -> dict[str, str]:
        now = int(time.time())
        token = jwt.encode(
            {
                "sub": f"clerk_{uuid4()}",
                "email": f"{uuid4()}@example.com",
                "iat": now,
                "exp": now + 3600,
                "iss": TEST_ISSUER,
            },
            private_pem,
            algorithm="RS256",
        )
        return {"Authorization": f"Bearer {token}"}

    return _make


@pytest.fixture
async def scratch_session(committed_database_url):
    engine = create_async_engine(committed_database_url)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        sessionmaker = async_sessionmaker(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        async with sessionmaker() as session:
            yield session
        await transaction.rollback()
    await engine.dispose()


@pytest.fixture
async def client_factory(scratch_session) -> AsyncIterator[Callable[[], httpx.AsyncClient]]:
    app = create_app()

    async def _session_override():
        try:
            yield scratch_session
            await scratch_session.commit()
        except Exception:
            await scratch_session.rollback()
            raise

    app.dependency_overrides[get_session] = _session_override
    settings = get_settings()
    app.state.redis = get_redis_client(settings.REDIS_URL)
    storage = get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )
    app.state.storage = storage
    written: list[str] = []
    real_put = storage.put_object

    async def _tracking_put(*, bucket: str, key: str, body: bytes, content_type: str) -> None:
        written.append(key)
        await real_put(bucket=bucket, key=key, body=body, content_type=content_type)

    storage.put_object = _tracking_put  # type: ignore[method-assign]
    app.state.written_keys = written

    def _make() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    yield _make

    for key in written:
        await storage.delete_object(bucket=settings.BUCKET_UPLOADS, key=key)
    await app.state.redis.aclose()


def _key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4())}


async def _project(client, headers, source_type: str = "text") -> str:
    response = await client.post(
        "/v1/projects",
        json={"title": "Pasted", "source_type": source_type},
        headers={**headers, **_key()},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]  # type: ignore[no-any-return]


async def _paste(client, headers, project_id, **body):
    body.setdefault("text", TEXT)
    return await client.post(
        f"/v1/projects/{project_id}/sources/text", json=body, headers={**headers, **_key()}
    )


# --- the happy path --------------------------------------------------------------------------


async def test_pasted_text_becomes_a_parsed_text_source_with_page_free_chunks(
    client_factory, auth_headers, scratch_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _project(client, headers)
        response = await _paste(client, headers, project_id, title="Plants")

        assert response.status_code == 201, response.text
        source = response.json()
        assert source["kind"] == "text"
        assert source["status"] == "parsed"
        assert source["original_filename"] == "Plants"
        assert source["page_count"] is None
        assert source["failure_reason"] is None
        assert source["content_type"].startswith("text/plain")

        chunks = (
            await client.get(
                f"/v1/projects/{project_id}/sources/{source['id']}/chunks", headers=headers
            )
        ).json()["items"]
        assert chunks
        assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))
        assert all(c["page_from"] is None and c["page_to"] is None for c in chunks)
        assert all(0 < len(c["content"]) <= pasted_text.CHUNK_CHARS for c in chunks)
        assert " ".join(" ".join(c["content"] for c in chunks).split()) == " ".join(TEXT.split())

        # indexing is queued, exactly as for a PDF: the same job kind the worker already runs
        jobs = (await jobs_repo.list_by_project(scratch_session, UUID(project_id))).items
        assert [j.kind for j in jobs] == ["build_evidence"]


async def test_the_text_is_stored_as_a_txt_object_in_the_projects_own_prefix(
    client_factory, auth_headers
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _project(client, headers)
        response = await _paste(client, headers, project_id)

    cleaned = pasted_text.clean(TEXT).encode()
    expected = source_storage_key(UUID(project_id), hashlib.sha256(cleaned).hexdigest(), "txt")
    assert response.json()["checksum_sha256"] == hashlib.sha256(cleaned).hexdigest()
    assert response.json()["byte_size"] == len(cleaned)
    assert expected.startswith(f"projects/{project_id}/") and expected.endswith(".txt")


async def test_a_blank_title_is_named_by_the_default(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _project(client, headers)
        response = await _paste(client, headers, project_id, title="   ")
    assert response.json()["original_filename"] == pasted_text.DEFAULT_TITLE


# --- refusals --------------------------------------------------------------------------------


async def test_too_short_text_is_refused_and_nothing_is_stored_or_queued(
    client_factory, auth_headers, scratch_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _project(client, headers)
        response = await _paste(client, headers, project_id, text="A short note about plants.")
        listing = (await client.get(f"/v1/projects/{project_id}/sources", headers=headers)).json()
        written = client._transport.app.state.written_keys  # type: ignore[attr-defined]

    assert response.status_code == 400
    assert "too short" in response.json()["error"]["message"]
    assert listing["items"] == []
    assert written == []
    assert (await jobs_repo.list_by_project(scratch_session, UUID(project_id))).items == []


async def test_text_without_words_is_refused(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _project(client, headers)
        response = await _paste(client, headers, project_id, text="12345 67890 ... " * 60)
    assert response.status_code == 400
    assert "no words" in response.json()["error"]["message"]


async def test_text_over_the_maximum_is_refused(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _project(client, headers)
        response = await _paste(
            client, headers, project_id, text="word " * ((pasted_text.MAX_CHARS // 5) + 100)
        )
    assert response.status_code == 400
    assert "too long" in response.json()["error"]["message"]


async def test_an_unknown_field_is_a_422(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _project(client, headers)
        response = await _paste(client, headers, project_id, language="en-GB")
    assert response.status_code == 422


async def test_a_missing_or_empty_text_is_a_422(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _project(client, headers)
        missing = await client.post(
            f"/v1/projects/{project_id}/sources/text", json={}, headers={**headers, **_key()}
        )
        empty = await _paste(client, headers, project_id, text="")
    assert missing.status_code == 422 and empty.status_code == 422


async def test_an_idempotency_key_is_required(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _project(client, headers)
        response = await client.post(
            f"/v1/projects/{project_id}/sources/text", json={"text": TEXT}, headers=headers
        )
    assert response.status_code == 422


async def test_only_a_text_project_takes_pasted_text(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _project(client, headers, source_type="pdf")
        response = await _paste(client, headers, project_id)
    assert response.status_code == 409


async def test_someone_elses_project_is_a_404_not_a_403(client_factory, auth_headers):
    owner, stranger = auth_headers(), auth_headers()
    async with client_factory() as client:
        project_id = await _project(client, owner)
        response = await _paste(client, stranger, project_id)
        missing = await _paste(client, stranger, uuid4())
    assert response.status_code == missing.status_code == 404
    assert response.json()["error"]["code"] == missing.json()["error"]["code"]


async def test_identical_text_twice_is_a_409_even_with_different_line_endings(
    client_factory, auth_headers
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _project(client, headers)
        first = await _paste(client, headers, project_id)
        again = await _paste(client, headers, project_id, text=TEXT.replace("\n", "\r\n"))
        listing = (await client.get(f"/v1/projects/{project_id}/sources", headers=headers)).json()
    assert first.status_code == 201
    assert again.status_code == 409
    assert len(listing["items"]) == 1


async def test_the_same_idempotency_key_replays_the_first_result(client_factory, auth_headers):
    headers = {**auth_headers(), "Idempotency-Key": str(uuid4())}
    async with client_factory() as client:
        mine_id = await _project(client, headers)
        key = {"Idempotency-Key": str(uuid4())}
        first = await client.post(
            f"/v1/projects/{mine_id}/sources/text", json={"text": TEXT}, headers={**headers, **key}
        )
        replay = await client.post(
            f"/v1/projects/{mine_id}/sources/text", json={"text": TEXT}, headers={**headers, **key}
        )
        listing = (await client.get(f"/v1/projects/{mine_id}/sources", headers=headers)).json()
    assert first.status_code == replay.status_code == 201
    assert first.json()["id"] == replay.json()["id"]
    assert len(listing["items"]) == 1
